"""ActiveX/COM backend: draws straight into a running AutoCAD session.

This is the driver that gives you the live-editor experience — entities appear
in the drawing as the AI issues commands, and you can carry on editing them by
hand. It needs AutoCAD running and a document open; call :func:`connect` (or the
``acad_connect`` MCP tool) first.
"""

from __future__ import annotations

import contextlib
import json
import math
import queue
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from operator import attrgetter
from pathlib import Path
from typing import Any

from ..geom import Point, left_normal, sub, unit, xy
from ..journal import Journal
from .base import BackendError
from .recording import RecordingBackend, unflatten_points

#: acSelectionSetAll — every entity in the drawing, for a whole-drawing export.
SELECT_ALL = 5

try:  # pragma: no cover - platform dependent
    import pythoncom
    import win32com.client
    from win32com.client import VARIANT

    HAS_COM = True
except ImportError:  # pragma: no cover
    HAS_COM = False

#: AutoCAD 2026 is release R25.1; older/newer releases fall back to the generic id.
PROGIDS = (
    "AutoCAD.Application.25.1",
    "AutoCAD.Application.25.0",
    "AutoCAD.Application.24.3",
    "AutoCAD.Application",
)

#: AutoCAD's ActiveX alignment enum value for "middle centre".
AC_ALIGN_MIDDLE_CENTER = 4
#: acHatchPatternTypePredefined
AC_HATCH_PREDEFINED = 0
#: acAttachmentPointMiddleCenter
AC_MTEXT_MIDDLE_CENTER = 5

#: Text style this backend creates for its own text. A stock drawing's Standard
#: style is txt.shx with no big font, and SHX fonts carry no CJK glyphs, so every
#: Chinese character comes out as '?'.
#:
#: The gbenor + gbcbig pair is used rather than a CJK TrueType face because it is
#: what renders correctly in AutoCAD: measured here, simhei.ttf drew Chinese as
#: '?' while gbenor.shx + gbcbig.shx drew it fine. It is also the pair the
#: reference drawings in this workspace already use.
CJK_STYLE = "HZ"
CJK_FONT = "gbenor.shx"
CJK_BIG_FONT = "gbcbig.shx"

#: AutoCAD refuses COM calls while it is mid-command or repainting and answers
#: with RPC_E_CALL_REJECTED. A run of a few hundred entities will hit this, so
#: every call is retried rather than letting a long drawing run die on a blip.
RPC_E_CALL_REJECTED = -2147418111
CALL_RETRIES = 10
CALL_BACKOFF = 0.2
CALL_BACKOFF_MAX = 1.5

#: How long a call may sit on the COM thread before its caller gives up. Generous,
#: because a save or a large hatch legitimately takes a while; bounded, because a
#: modal dialog in AutoCAD means no COM call ever returns.
CALL_WAIT = 600.0

#: Raised by `insert` and, at commit time, by `_apply_primitive`. Both have to say
#: the same thing: a step recorded before an insert failed would otherwise replay
#: here as "unknown primitive kind", which hides that the backend simply has no
#: block library to draw from.
INSERT_REFUSED = (
    "the 'com' backend cannot insert blocks yet; the plugin backend can, and it is "
    "the one that keeps the drawing's step journal"
)


# ---------------------------------------------------------------------------
# the sidecar: where a COM session's journal actually lives
# ---------------------------------------------------------------------------

#: The suffix of the file written beside a saved drawing. ActiveX exposes no way to
#: write an XRecord into the DWG — that is the plugin's route and COM has none — so a
#: COM session's steps and catalogue identity go in a file next to the drawing instead.
SIDECAR_SUFFIX = ".acadmcp.json"


def sidecar_path(drawing: str | Path) -> Path:
    """Where the sidecar for a saved drawing lives: `<drawing>.<ext>.acadmcp.json`."""
    target = Path(drawing)
    return target.with_suffix(target.suffix + SIDECAR_SUFFIX)


def write_sidecar(drawing: str | Path, journal: Journal) -> Path | None:
    """Write the journal beside a saved drawing, or None when there is nothing to write.

    A session with no steps and no meta writes nothing, so an ordinary save leaves the
    user's folder exactly as it was: a file appearing next to somebody's drawing is not
    acceptable unless it is announced, and an empty one would be announced for nothing.
    """
    if not journal.steps and not journal.meta:
        return None
    path = sidecar_path(drawing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(journal.to_dict(), ensure_ascii=True, indent=2), encoding="utf-8")
    return path


def read_sidecar(drawing: str | Path) -> tuple[Journal | None, str]:
    """(journal, problem) from the sidecar beside a drawing.

    An absent file is not a problem — most drawings have none. An unreadable one is
    reported rather than raised, the rule the journal follows everywhere: losing a
    session's undo history is a line in the report, not the end of the session.
    """
    path = sidecar_path(drawing)
    if not path.is_file():
        return None, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Journal.from_dict(payload), ""
    except (OSError, ValueError) as exc:
        return None, f"sidecar {str(path)!r} ignored: {exc}"


class _Box:
    """One call in flight: its result, its failure, and its completion."""

    def __init__(self) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = threading.Event()


class _ComThread:
    """The one thread that owns the COM objects.

    A COM object belongs to the apartment of the thread that created it. The MCP
    server runs each tool call on whichever worker thread happens to be free, so a
    backend built during one call and used during the next is used from the wrong
    apartment — and win32com reports that as ``AttributeError: <unknown>.AddLine``,
    because the dynamic wrapper can no longer read the object's type. Measured
    before this existed: the first call after a restart worked and every later one
    failed, which made the whole AutoCAD path unusable past one command.

    Every COM call is therefore marshalled here, where COM is initialised once for
    the life of the thread and every object stays in the apartment it was made in.
    """

    def __init__(self) -> None:
        #: True once this thread has initialised its COM apartment.
        self.ready = False
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._serve, name="acadmcp-com", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        pythoncom.CoInitialize()
        self.ready = True
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args, box = item
            try:
                box.result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread
                box.error = exc
            finally:
                box.done.set()

    def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run `fn` on the COM thread and bring its result — or failure — back."""
        box = _Box()
        self._queue.put((fn, args, box))
        if not box.done.wait(CALL_WAIT):
            raise BackendError(
                f"AutoCAD did not answer within {CALL_WAIT:g}s. If a dialog is up in "
                "the AutoCAD window, dismiss it and try again."
            )
        if box.error is not None:
            raise box.error
        return box.result


_owner_lock = threading.Lock()
_owner: _ComThread | None = None


def _com_thread() -> _ComThread:
    """The COM thread, started on first use so that importing this module is free."""
    global _owner
    with _owner_lock:
        if _owner is None:
            _owner = _ComThread()
        return _owner


def _require_com() -> None:
    if not HAS_COM:
        raise BackendError(
            "the 'com' backend needs pywin32: pip install pywin32 "
            "(on Windows this ships with the project's dependencies)"
        )


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """Invoke a COM call on the COM thread, retrying while AutoCAD reports busy.

    Pass a *zero-argument callable* whenever the thing being called is reached
    through an attribute — ``_call(lambda: self.msp.AddLine(a, b))``, not
    ``_call(self.msp.AddLine, a, b)``. The second form resolves ``self.msp.AddLine``
    on the calling thread, which is exactly the off-apartment access this function
    exists to prevent; the callable form resolves it inside the worker.
    ``_call(setattr, entity, "Layer", name)`` is fine, because ``setattr`` does its
    lookup when it runs.
    """
    delay = CALL_BACKOFF
    for attempt in range(CALL_RETRIES):
        try:
            return _com_thread().run(fn, *args)
        except pythoncom.com_error as exc:  # type: ignore[attr-defined]
            if exc.args[0] != RPC_E_CALL_REJECTED or attempt == CALL_RETRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.6, CALL_BACKOFF_MAX)
        except AttributeError as exc:
            # win32com's dynamic dispatch raises exactly this when a cached object is
            # reached for off its apartment: the object is alive but its type is gone.
            # Translating it matters because it is *not* a com_error, so the retry
            # above never sees it and the tool layer reports a bare "failed".
            if not str(exc).startswith("<unknown>."):
                raise
            raise BackendError(
                f"AutoCAD would not accept {exc}. The COM link is most likely stale — "
                "call acad_connect again and retry."
            ) from exc
    raise AssertionError("unreachable")


def _point3(p: Point) -> Any:
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(p[0]), float(p[1]), 0.0))


def _flat2(points: Sequence[Point]) -> Any:
    flat: list[float] = []
    for p in points:
        flat.extend((float(p[0]), float(p[1])))
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(flat))


def connect(create: bool = True, visible: bool = True) -> Any:
    """Attach to a running AutoCAD, optionally launching one if none is open."""
    _require_com()
    app = None
    try:
        # On the COM thread: the application object is used from it for the rest of
        # the session, so it has to be made there.
        app = _call(lambda: win32com.client.GetActiveObject("AutoCAD.Application"))
    except Exception:  # noqa: BLE001 - not running, or not in the ROT yet
        app = None

    if app is None:
        if not create:
            raise BackendError("no running AutoCAD found; start AutoCAD first or pass create=True")
        last: Exception | None = None
        for progid in PROGIDS:
            try:
                app = _call(lambda p=progid: win32com.client.Dispatch(p))
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if app is None:
            raise BackendError(f"could not start AutoCAD via COM: {last}")

    # Visible is refused while a modal dialog is up, which is not a failure.
    with contextlib.suppress(Exception):
        _call(setattr, app, "Visible", bool(visible))
    return app


class ComBackend(RecordingBackend):
    """Renders into the model space of the active AutoCAD drawing.

    Primitives are *hybrid*: outside a step they draw eagerly, exactly as they
    always have; inside one they are recorded, and the commit draws the batch and
    journals the handles AutoCAD handed back. The journal lives in this process
    only — AutoCAD owns the drawing, so unlike the plugin there is no DWG copy of
    it to keep in step.
    """

    name = "com"

    #: Journal state default, on the class so a `__new__`-built instance answers.
    #: tests/test_com_backend.py builds a fake link that way (no `__init__`, so no
    #: AutoCAD), and every primitive reads `open_step` before it decides to draw.
    _op = ""
    #: Same reason as `_op`: `info()` reads these, and the fake link has no `__init__`.
    sidecar_path: Path | None = None
    sidecar_unreadable = ""

    def __init__(
        self,
        app: Any = None,
        doc: Any = None,
        create: bool = True,
        visible: bool = True,
        new_document: bool = False,
        text_style: str = CJK_STYLE,
        cjk_font: str = CJK_FONT,
        cjk_big_font: str = CJK_BIG_FONT,
        path: str | None = None,
        **_: Any,
    ) -> None:
        super().__init__()
        _require_com()
        self.text_style = text_style
        self.cjk_font = cjk_font
        self.cjk_big_font = cjk_big_font
        self._style_ready = False
        self.app = app if app is not None else connect(create=create, visible=visible)
        if new_document:
            self.doc = _call(lambda: self.app.Documents.Add())
        else:
            try:
                self.doc = doc if doc is not None else _call(lambda: self.app.ActiveDocument)
            except Exception as exc:  # noqa: BLE001
                raise BackendError(
                    "AutoCAD has no open drawing; open one or pass new_document=True"
                ) from exc
        self.msp = _call(lambda: self.doc.ModelSpace)
        self.path: str | None = str(path) if path else None
        self.sidecar_path = None
        self.sidecar_unreadable = ""
        self._ops = 0
        with contextlib.suppress(Exception):
            _call(lambda: self.doc.SetVariable("INSUNITS", 4))  # millimetres
        # A path given means "this drawing is on disk"; its sidecar is where the journal
        # from the session that drew it lives, since it is not in the DWG.
        if self.path:
            self.load_sidecar(self.path)

    # --- the sidecar -------------------------------------------------------
    def load_sidecar(self, path: str | None = None) -> bool:
        """Adopt the sidecar journal beside `path`, if one is there.

        Returns whether a journal was read. Called on `__init__` when a path is given and
        public so a reopen can point a live session at a different drawing's history; an
        absent sidecar is the ordinary case and an unreadable one is reported through
        `sidecar_unreadable` rather than raised.
        """
        target = path or self.path
        if not target:
            return False
        self.path = str(target)
        journal, problem = read_sidecar(target)
        self.sidecar_unreadable = problem
        if journal is not None:
            self.journal = journal
        return journal is not None

    # --- document ----------------------------------------------------------
    def ensure_layer(self, name: str, color: int | None = None, linetype: str | None = None) -> Any:
        try:
            layer = _call(lambda: self.doc.Layers.Item(name))
        except Exception:  # noqa: BLE001 - a missing layer raises a bare COM error
            layer = _call(lambda: self.doc.Layers.Add(name))
        if color is not None:
            with contextlib.suppress(Exception):
                _call(setattr, layer, "color", int(color))
        if linetype and linetype != "CONTINUOUS":
            # Already loaded, or the standard linetype library is not present.
            with contextlib.suppress(Exception):
                _call(lambda: self.doc.Linetypes.Load(linetype, "acad.lin"))
            with contextlib.suppress(Exception):
                _call(setattr, layer, "Linetype", linetype)
        return layer

    def set_units_mm(self) -> None:
        with contextlib.suppress(Exception):
            _call(lambda: self.doc.SetVariable("INSUNITS", 4))

    def set_text_style(
        self, name: str | None = None, font: str | None = None, big_font: str | None = None
    ) -> str:
        """Create or repoint the text style used for drawn text.

        Pass `font`/`big_font` to match a client standard. Left alone the style
        uses gbenor.shx with the gbcbig.shx big font, which is what carries the
        Chinese glyphs.
        """
        name = name or self.text_style
        font = font or self.cjk_font
        if big_font is None:
            # An SHX font needs a big font for CJK; a TrueType face carries the
            # glyphs itself and must have none, or the big font wins.
            big_font = "" if font.lower().endswith(".ttf") else self.cjk_big_font
        try:
            style = _call(lambda: self.doc.TextStyles.Item(name))
        except Exception:  # noqa: BLE001 - absent style raises a bare COM error
            style = _call(lambda: self.doc.TextStyles.Add(name))
        with contextlib.suppress(Exception):
            _call(setattr, style, "bigFontFile", big_font)
        with contextlib.suppress(Exception):
            _call(setattr, style, "fontFile", font)
        self.text_style = name
        self.cjk_font = font
        self.cjk_big_font = big_font
        self._style_ready = True
        # Repointing a style does not repaint text already in the drawing; without
        # a regen the change looks like it did nothing.
        with contextlib.suppress(Exception):
            _call(lambda: self.doc.Regen(1))  # acActiveViewport
        return name

    def _cjk_style(self) -> str:
        """The style to put on text entities, created on first use."""
        if not self._style_ready:
            self.set_text_style()
        return self.text_style

    def save(self, path: str | None = None) -> str:
        """Save the drawing, or write a copy of it.

        A `.dxf` target is a **copy** and goes through `Document.Export`, which
        leaves the open drawing alone. `SaveAs` is wrong for it twice over: it
        ignores the extension AutoCAD was not told about — asking for `x.dxf`
        wrote `x.dxf.dwg`, a DWG wearing a DXF's name — and it renames the open
        document to the copy's name either way. Measured 2026-10-15 on 2026.

        The journal and the catalogue identity go into a `<drawing>.acadmcp.json`
        **sidecar** beside the target, because ActiveX offers no way to write an
        XRecord into the DWG. `sidecar_path` says whether one was written, and the
        tool result has to report it: a file appearing next to somebody's drawing
        unannounced is not acceptable.
        """
        if not path:
            raise BackendError(
                "the COM backend saves through AutoCAD; pass an absolute .dwg or .dxf path"
            )
        target = Path(path)
        if target.suffix.lower() == ".dxf":
            with tempfile.TemporaryDirectory() as tmp:
                written = self._export_to(str(Path(tmp) / "copy"), "DXF")
                if written is None:
                    raise BackendError(f"AutoCAD would not export a DXF copy to {str(target)!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(written, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                _call(lambda: self.doc.SaveAs(str(target)))
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"AutoCAD could not save {str(target)!r}: {exc}") from exc
        self.path = str(target)
        # `getattr` because a `__new__`-built instance (the fake link the COM tests use,
        # with no `__init__`) has no journal — and a save with nothing to record should
        # write no sidecar rather than fail.
        journal = getattr(self, "journal", None)
        self.sidecar_path = write_sidecar(target, journal) if journal is not None else None
        return str(target)

    def _export_to(self, stem: str, kind: str) -> Path | None:
        """Export the whole drawing to `stem`.`kind`, or None if AutoCAD refused.

        `Document.Export` writes a *copy* — unlike `SaveAs` it does not rename the
        drawing, and unlike `DXFOUT` it does not rename an unsaved one either (both
        were measured doing exactly that here). It needs a selection set, which is
        the whole drawing.
        """
        name = f"ACADMCP_EXPORT_{kind}"
        with contextlib.suppress(Exception):
            _call(lambda: self.doc.SelectionSets.Item(name).Delete())
        try:
            selection = _call(lambda: self.doc.SelectionSets.Add(name))
            try:
                _call(lambda: selection.Select(SELECT_ALL))
                _call(lambda: self.doc.Export(stem, kind, selection))
            finally:
                with contextlib.suppress(Exception):
                    _call(lambda: selection.Delete())
        except Exception:  # noqa: BLE001 - the caller decides what to fall back to
            return None
        # AutoCAD upper-cases the extension it appends.
        for candidate in (Path(f"{stem}.{kind}"), Path(f"{stem}.{kind.lower()}")):
            if candidate.is_file() and candidate.stat().st_size:
                return candidate
        return None

    def preview(self, path: str, theme: str = "dark") -> str | None:
        return self.screenshot(path, theme=theme)

    def screenshot(
        self,
        path: str,
        *,
        window: Sequence[float] | None = None,
        width: int = 1600,
        height: int = 1200,
        theme: str = "dark",
    ) -> str:
        """Photograph the AutoCAD window, framing a region first if one is given.

        AutoCAD cannot hand back a rendered bitmap over COM, so the window itself
        is captured. That shows the true current view, but it also drags in the
        ribbon; prefer the dxf backend when you want a clean drawing-only image.

        When the window cannot be photographed — AutoCAD minimised, which is the
        normal state for someone working in another application — the drawing is
        exported and rendered instead. The window is never restored to take the
        picture: that would put AutoCAD in front of whatever the user moved on to.
        """
        from ..capture import WindowUnavailable, capture_window

        if window:
            self.zoom_window(
                (float(window[0]), float(window[1])), (float(window[2]), float(window[3]))
            )
        try:
            return capture_window(int(_call(lambda: self.app.HWND)), path)
        except WindowUnavailable:
            return self._exported_screenshot(path, width=width, height=height, theme=theme)

    def _exported_screenshot(self, path: str, *, width: int, height: int, theme: str) -> str:
        """Picture of the drawing from AutoCAD's own export.

        Preferred over re-rendering the DXF because AutoCAD draws it: the layer
        colours and hatch patterns are the real ones, and it reflects the current
        view (so `window=` framing still means something). It carries no ribbon,
        which for reviewing a plan is an improvement.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stem = str(Path(tmp) / "view")
            bitmap = self._export_to(stem, "BMP")
            if bitmap is not None:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bitmap, path)
                return path

            drawing = self._export_to(stem, "DXF")
            if drawing is None:
                raise BackendError(
                    "AutoCAD is minimised and would not export the drawing, so there "
                    "is no picture to take; restore it, or use the dxf backend"
                )
            from .dxf import DxfBackend

            return DxfBackend(path=str(drawing)).screenshot(
                path, width=width, height=height, theme=theme
            )

    def zoom_window(self, corner1: Point, corner2: Point) -> None:
        """Zoom the AutoCAD view to the rectangle spanned by two corners."""
        _call(lambda: self.app.ZoomWindow(_point3(corner1), _point3(corner2)))

    def zoom_extents(self) -> None:
        with contextlib.suppress(Exception):
            _call(lambda: self.app.ZoomExtents())

    def info(self) -> dict[str, Any]:
        """What this backend is attached to, including when it cannot tell.

        A lost link is reported rather than papered over. The old version returned
        ``entities: -1`` and ``document: "<unnamed>"``, which reads as "an unnamed
        drawing with a negative entity count" — a drawing problem — instead of "the
        connection is gone", and that is what sent the last bug hunt the wrong way.
        """
        out: dict[str, Any] = {"backend": self.name, "units": "mm", "ops": self._ops}
        try:
            name = str(_call(lambda: self.doc.Name))
            count = int(_call(lambda: self.msp.Count))
        except Exception as exc:  # noqa: BLE001 - a status call reports, it never raises
            out["document"] = None
            out["entities"] = None
            out["problem"] = f"{type(exc).__name__}: {exc}"
            return out
        out["document"] = name
        out["entities"] = count
        # The journal is this process's own bookkeeping, so it can be reported even though
        # the drawing it describes is AutoCAD's. Kept out of the failure branch above: on a
        # lost link the drawing state is the question, and a step count next to
        # `entities: None` reads as if it described the same thing.
        out["journal"] = {
            "steps": len(self.journal.steps),
            "applied": sum(s.state == "applied" for s in self.journal.steps),
        }
        # The sidecar is bookkeeping too, and it is the only place a COM session's journal
        # is durable — so whether one was written, and whether an old one could not be read,
        # belongs in the same report.
        out["sidecar"] = str(self.sidecar_path) if self.sidecar_path else None
        out["sidecar_unreadable"] = self.sidecar_unreadable
        return out

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        # Deliberately not suppressed: this is the call the tag numbering reads, and an
        # empty list would mean "the drawing has no tags", silently restarting the
        # numbering at 1. A broken link has to say so instead.
        total = int(_call(lambda: self.msp.Count))
        for i in range(total):
            if len(out) >= limit:
                break
            try:
                e = _call(lambda index=i: self.msp.Item(index))
                ent_layer = str(_call(attrgetter("Layer"), e))
            except Exception:  # noqa: BLE001 - AutoCAD skips some proxy entities
                continue
            if layer and ent_layer != layer:
                continue
            out.append(
                {
                    "type": str(_call(attrgetter("ObjectName"), e)),
                    "layer": ent_layer,
                    "handle": str(_call(attrgetter("Handle"), e)),
                }
            )
        return out

    # --- primitives: eager outside a step, recorded inside one ---------------
    def line(self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None) -> Any:
        if self.open_step:
            return self._record("line", [p1, p2], layer=layer, linetype=linetype)
        return self._draw_line(p1, p2, layer=layer, linetype=linetype)

    def _draw_line(
        self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None
    ) -> Any:
        self._ops += 1
        e = _call(lambda: self.msp.AddLine(_point3(p1), _point3(p2)))
        self._style(e, layer, linetype)
        return e

    def polyline(
        self,
        points: Sequence[Point],
        *,
        closed: bool = False,
        layer: str = "0",
        linetype: str | None = None,
    ) -> Any:
        if self.open_step:
            return self._record("polyline", points, layer=layer, closed=closed, linetype=linetype)
        return self._draw_polyline(points, closed=closed, layer=layer, linetype=linetype)

    def _draw_polyline(
        self,
        points: Sequence[Point],
        *,
        closed: bool = False,
        layer: str = "0",
        linetype: str | None = None,
    ) -> Any:
        self._ops += 1
        pts = [xy(p) for p in points]
        e = _call(lambda: self.msp.AddLightWeightPolyline(_flat2(pts)))
        _call(setattr, e, "Closed", bool(closed))
        self._style(e, layer, linetype)
        return e

    def circle(self, center: Point, radius: float, *, layer: str = "0") -> Any:
        if self.open_step:
            return self._record("circle", [center], layer=layer, radius=radius)
        return self._draw_circle(center, radius, layer=layer)

    def _draw_circle(self, center: Point, radius: float, *, layer: str = "0") -> Any:
        self._ops += 1
        e = _call(lambda: self.msp.AddCircle(_point3(center), float(radius)))
        self._style(e, layer)
        return e

    def arc(
        self,
        center: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        *,
        layer: str = "0",
    ) -> Any:
        if self.open_step:
            return self._record(
                "arc",
                [center],
                layer=layer,
                radius=radius,
                start_angle=start_deg,
                end_angle=end_deg,
            )
        return self._draw_arc(center, radius, start_deg, end_deg, layer=layer)

    def _draw_arc(
        self,
        center: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        *,
        layer: str = "0",
    ) -> Any:
        self._ops += 1
        e = _call(
            lambda: self.msp.AddArc(
                _point3(center),
                float(radius),
                math.radians(float(start_deg)),
                math.radians(float(end_deg)),
            )
        )
        self._style(e, layer)
        return e

    def text(
        self,
        pos: Point,
        content: str,
        height: float,
        *,
        layer: str = "0",
        rotation: float = 0.0,
        align: str = "left",
    ) -> Any:
        if self.open_step:
            return self._record(
                "text",
                [pos],
                layer=layer,
                text=content,
                height=height,
                rotation=rotation,
                align=align,
            )
        return self._draw_text(pos, content, height, layer=layer, rotation=rotation, align=align)

    def _draw_text(
        self,
        pos: Point,
        content: str,
        height: float,
        *,
        layer: str = "0",
        rotation: float = 0.0,
        align: str = "left",
    ) -> Any:
        self._ops += 1
        e = _call(lambda: self.msp.AddText(str(content), _point3(pos), float(height)))
        with contextlib.suppress(Exception):
            # Set the style before alignment: repointing a style can reset it.
            _call(setattr, e, "StyleName", self._cjk_style())
            _call(setattr, e, "Rotation", math.radians(float(rotation)))
            if align == "center":
                _call(setattr, e, "Alignment", AC_ALIGN_MIDDLE_CENTER)
                _call(setattr, e, "TextAlignmentPoint", _point3(pos))
        self._style(e, layer)
        return e

    def mtext(
        self,
        pos: Point,
        content: str,
        height: float,
        width: float = 0.0,
        *,
        layer: str = "0",
        align: str = "center",
    ) -> Any:
        if self.open_step:
            return self._record(
                "mtext", [pos], layer=layer, text=content, height=height, width=width, align=align
            )
        return self._draw_mtext(pos, content, height, width, layer=layer, align=align)

    def _draw_mtext(
        self,
        pos: Point,
        content: str,
        height: float,
        width: float = 0.0,
        *,
        layer: str = "0",
        align: str = "center",
    ) -> Any:
        self._ops += 1
        e = _call(lambda: self.msp.AddMText(_point3(pos), float(width or 6000.0), str(content)))
        with contextlib.suppress(Exception):
            _call(setattr, e, "StyleName", self._cjk_style())
            _call(setattr, e, "Height", float(height))
            if align == "center":
                _call(setattr, e, "AttachmentPoint", AC_MTEXT_MIDDLE_CENTER)
                _call(setattr, e, "InsertionPoint", _point3(pos))
        self._style(e, layer)
        return e

    def hatch(
        self,
        points: Sequence[Point],
        pattern: str = "ANSI31",
        scale: float = 1.0,
        *,
        layer: str = "0",
        angle: float = 0.0,
        solid: bool = False,
    ) -> Any:
        if self.open_step:
            return self._record(
                "hatch",
                points,
                layer=layer,
                pattern=pattern,
                scale=scale,
                angle=angle,
                solid=solid,
            )
        return self._draw_hatch(points, pattern, scale, layer=layer, angle=angle, solid=solid)

    def _draw_hatch(
        self,
        points: Sequence[Point],
        pattern: str = "ANSI31",
        scale: float = 1.0,
        *,
        layer: str = "0",
        angle: float = 0.0,
        solid: bool = False,
    ) -> Any:
        self._ops += 1
        # The ActiveX hatch API wants boundary *objects*, so lay down a temporary
        # polyline, hatch it, then remove the boundary again. It goes on the target
        # layer rather than 0, so that if it ever has to be left behind it is not junk
        # on layer 0.
        boundary = _call(lambda: self.msp.AddLightWeightPolyline(_flat2([xy(p) for p in points])))
        _call(setattr, boundary, "Closed", True)
        self._style(boundary, layer)

        h = None
        try:
            pattern_name = "SOLID" if solid else pattern
            h = _call(lambda: self.msp.AddHatch(AC_HATCH_PREDEFINED, pattern_name, False))
            loop = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (boundary,))
            _call(lambda: h.AppendOuterLoop(loop))
            if not solid:
                _call(setattr, h, "PatternScale", float(scale))
                _call(setattr, h, "PatternAngle", math.radians(float(angle)))
            _call(lambda: h.Evaluate())
        except Exception:
            # A half-made hatch is worse than none: it keeps the pattern scale it was
            # made with, so at 1.0 across a 240 mm wall it reads as a solid blob, and it
            # sits on layer 0 because the styling below never ran. Measured on a real
            # envelope: one of four wall segments failed this way and left both the hatch
            # and its temporary boundary in the drawing — invisible, because the caller
            # suppresses a hatch failure so a decorative fill cannot take a wall down.
            for half in (h, boundary):
                if half is not None:
                    with contextlib.suppress(Exception):
                        _call(lambda e=half: e.Delete())
            raise
        self._style(h, layer)
        with contextlib.suppress(Exception):
            _call(lambda: boundary.Delete())
        return h

    def linear_dimension(
        self,
        p1: Point,
        p2: Point,
        offset: float,
        *,
        layer: str = "0",
        text_height: float | None = None,
    ) -> Any:
        if self.open_step:
            return self._record(
                "dimension",
                [p1, p2],
                layer=layer,
                offset=offset,
                text_height=text_height or 0.0,
            )
        return self._draw_linear_dimension(p1, p2, offset, layer=layer, text_height=text_height)

    def _draw_linear_dimension(
        self,
        p1: Point,
        p2: Point,
        offset: float,
        *,
        layer: str = "0",
        text_height: float | None = None,
    ) -> Any:
        self._ops += 1
        a, b = xy(p1), xy(p2)
        # AutoCAD measures along the run, so push the dimension line out sideways.
        n = left_normal(unit(sub(b, a)))
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        text_pos = (mid[0] + n[0] * float(offset), mid[1] + n[1] * float(offset))
        e = _call(lambda: self.msp.AddDimAligned(_point3(a), _point3(b), _point3(text_pos)))
        self._style(e, layer)
        return e

    def insert(
        self,
        block: str,
        at: Point,
        *,
        source: str | None = None,
        rotation: float = 0.0,
        scale: float = 1.0,
        scale_x: float | None = None,
        scale_y: float | None = None,
        properties: dict[str, Any] | None = None,
        layer: str = "0",
    ) -> Any:
        """Refused: this backend has no block library to draw from.

        Inside a step it is recorded like any other primitive and the refusal moves
        to the commit, where `_apply_primitive` raises. That is not a second chance:
        it is so a replayed journal reports this backend's reason instead of an
        unknown primitive kind.
        """
        if self.open_step:
            return self._record(
                "insert",
                [at],
                layer=layer,
                block=block,
                source=source,
                rotation=rotation,
                # One scale for both axes unless they are given separately, matching the
                # plugin's wire format — both always travel, so no reader has to guess
                # whether a missing one means 1.0.
                scale_x=scale if scale_x is None else scale_x,
                scale_y=scale if scale_y is None else scale_y,
                properties=properties,
            )
        raise BackendError(INSERT_REFUSED)

    # --- recording: applying and erasing a step -----------------------------
    def _apply_primitive(self, prim: dict[str, Any]) -> Any:
        """Draw one recorded primitive — the inverse of the recording branches above.

        Keys are the plugin's wire format, so a step drawn on any backend can be
        replayed here, and every missing key falls back to the same default the
        corresponding public method has.
        """
        pts = unflatten_points(prim.get("points", []))
        kind = prim["kind"]
        if kind == "line":
            return self._draw_line(
                pts[0], pts[1], layer=prim.get("layer", "0"), linetype=prim.get("linetype")
            )
        if kind == "polyline":
            return self._draw_polyline(
                pts,
                closed=bool(prim.get("closed", False)),
                layer=prim.get("layer", "0"),
                linetype=prim.get("linetype"),
            )
        if kind == "circle":
            return self._draw_circle(pts[0], float(prim["radius"]), layer=prim.get("layer", "0"))
        if kind == "arc":
            return self._draw_arc(
                pts[0],
                float(prim["radius"]),
                float(prim["start_angle"]),
                float(prim["end_angle"]),
                layer=prim.get("layer", "0"),
            )
        if kind == "text":
            return self._draw_text(
                pts[0],
                str(prim["text"]),
                float(prim["height"]),
                layer=prim.get("layer", "0"),
                rotation=float(prim.get("rotation", 0.0)),
                align=str(prim.get("align", "left")),
            )
        if kind == "mtext":
            return self._draw_mtext(
                pts[0],
                str(prim["text"]),
                float(prim["height"]),
                width=float(prim.get("width", 0.0)),
                layer=prim.get("layer", "0"),
                align=str(prim.get("align", "center")),
            )
        if kind == "hatch":
            return self._draw_hatch(
                pts,
                str(prim.get("pattern", "ANSI31")),
                float(prim.get("scale", 1.0)),
                layer=prim.get("layer", "0"),
                angle=float(prim.get("angle", 0.0)),
                solid=bool(prim.get("solid", False)),
            )
        if kind == "dimension":
            # text_height travels as 0.0 when unset; the drawer treats None as "default".
            height = float(prim.get("text_height") or 0.0) or None
            return self._draw_linear_dimension(
                pts[0],
                pts[1],
                float(prim.get("offset", 0.0)),
                layer=prim.get("layer", "0"),
                text_height=height,
            )
        if kind == "insert":
            raise BackendError(INSERT_REFUSED)
        raise BackendError(
            f"the 'com' backend does not know the primitive kind {kind!r} and cannot draw it"
        )

    def _apply(self, primitives: list[dict[str, Any]]) -> list[str]:
        """Draw a recorded batch and report the handle of each entity."""
        handles: list[str] = []
        for prim in primitives:
            entity = self._apply_primitive(prim)
            # Handle is reached through the COM object, so the read goes through _call
            # like every other attribute access; the loop variable is bound into the
            # lambda, or every closure would read the last entity's handle.
            handles.append(str(_call(lambda e=entity: e.Handle)))
        return handles

    def _erase(self, handles: list[str]) -> tuple[int, int]:
        """Delete entities by handle, counting the ones already gone."""
        erased = missing = 0
        for handle in handles:
            try:
                obj = _call(lambda h=handle: self.doc.HandleToObject(h))
                _call(lambda o=obj: o.Delete())
                erased += 1
            except Exception:  # noqa: BLE001 - a hand-deleted entity is counted, not fatal
                missing += 1
        return erased, missing

    # --- helpers -----------------------------------------------------------
    def _style(self, entity: Any, layer: str, linetype: str | None = None) -> None:
        # Styling must never abort a drawing that otherwise succeeded.
        with contextlib.suppress(Exception):
            _call(setattr, entity, "Layer", layer)
            if linetype and linetype != "CONTINUOUS":
                _call(setattr, entity, "Linetype", linetype)
