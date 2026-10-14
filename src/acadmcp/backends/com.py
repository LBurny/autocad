"""ActiveX/COM backend: draws straight into a running AutoCAD session.

This is the driver that gives you the live-editor experience — entities appear
in the drawing as the AI issues commands, and you can carry on editing them by
hand. It needs AutoCAD running and a document open; call :func:`connect` (or the
``acad_connect`` MCP tool) first.
"""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable, Sequence
from operator import attrgetter
from pathlib import Path
from typing import Any

from ..geom import Point, left_normal, sub, unit, xy
from .base import BackendError

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


def _require_com() -> None:
    if not HAS_COM:
        raise BackendError(
            "the 'com' backend needs pywin32: pip install pywin32 "
            "(on Windows this ships with the project's dependencies)"
        )


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """Invoke a COM call, retrying for as long as AutoCAD reports itself busy."""
    delay = CALL_BACKOFF
    for attempt in range(CALL_RETRIES):
        try:
            return fn(*args)
        except pythoncom.com_error as exc:  # type: ignore[attr-defined]
            if exc.args[0] != RPC_E_CALL_REJECTED or attempt == CALL_RETRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.6, CALL_BACKOFF_MAX)
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
        app = win32com.client.GetActiveObject("AutoCAD.Application")
    except Exception:  # noqa: BLE001 - not running, or not in the ROT yet
        app = None

    if app is None:
        if not create:
            raise BackendError("no running AutoCAD found; start AutoCAD first or pass create=True")
        last: Exception | None = None
        for progid in PROGIDS:
            try:
                app = win32com.client.Dispatch(progid)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if app is None:
            raise BackendError(f"could not start AutoCAD via COM: {last}")

    # Visible is refused while a modal dialog is up, which is not a failure.
    with contextlib.suppress(Exception):
        _call(setattr, app, "Visible", bool(visible))
    return app


class ComBackend:
    """Renders into the model space of the active AutoCAD drawing."""

    name = "com"

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
        **_: Any,
    ) -> None:
        _require_com()
        self.text_style = text_style
        self.cjk_font = cjk_font
        self.cjk_big_font = cjk_big_font
        self._style_ready = False
        self.app = app if app is not None else connect(create=create, visible=visible)
        if new_document:
            self.doc = _call(self.app.Documents.Add)
        else:
            try:
                self.doc = doc if doc is not None else _call(lambda: self.app.ActiveDocument)
            except Exception as exc:  # noqa: BLE001
                raise BackendError(
                    "AutoCAD has no open drawing; open one or pass new_document=True"
                ) from exc
        self.msp = _call(lambda: self.doc.ModelSpace)
        self.path: str | None = None
        self._ops = 0
        with contextlib.suppress(Exception):
            _call(self.doc.SetVariable, "INSUNITS", 4)  # millimetres

    # --- document ----------------------------------------------------------
    def ensure_layer(self, name: str, color: int | None = None, linetype: str | None = None) -> Any:
        try:
            layer = _call(self.doc.Layers.Item, name)
        except Exception:  # noqa: BLE001 - a missing layer raises a bare COM error
            layer = _call(self.doc.Layers.Add, name)
        if color is not None:
            with contextlib.suppress(Exception):
                _call(setattr, layer, "color", int(color))
        if linetype and linetype != "CONTINUOUS":
            # Already loaded, or the standard linetype library is not present.
            with contextlib.suppress(Exception):
                _call(self.doc.Linetypes.Load, linetype, "acad.lin")
            with contextlib.suppress(Exception):
                _call(setattr, layer, "Linetype", linetype)
        return layer

    def set_units_mm(self) -> None:
        with contextlib.suppress(Exception):
            _call(self.doc.SetVariable, "INSUNITS", 4)

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
            style = _call(self.doc.TextStyles.Item, name)
        except Exception:  # noqa: BLE001 - absent style raises a bare COM error
            style = _call(self.doc.TextStyles.Add, name)
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
            _call(self.doc.Regen, 1)  # acActiveViewport
        return name

    def _cjk_style(self) -> str:
        """The style to put on text entities, created on first use."""
        if not self._style_ready:
            self.set_text_style()
        return self.text_style

    def save(self, path: str | None = None) -> str:
        if not path:
            raise BackendError(
                "the COM backend saves through AutoCAD; pass an absolute .dwg or .dxf path"
            )
        target = str(Path(path).with_suffix(Path(path).suffix or ".dwg"))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        try:
            _call(self.doc.SaveAs, target)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"AutoCAD could not save {target!r}: {exc}") from exc
        self.path = target
        return target

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
        """
        if window:
            self.zoom_window(
                (float(window[0]), float(window[1])), (float(window[2]), float(window[3]))
            )
        from ..capture import capture_window

        return capture_window(int(_call(lambda: self.app.HWND)), path)

    def zoom_window(self, corner1: Point, corner2: Point) -> None:
        """Zoom the AutoCAD view to the rectangle spanned by two corners."""
        _call(self.app.ZoomWindow, _point3(corner1), _point3(corner2))

    def zoom_extents(self) -> None:
        with contextlib.suppress(Exception):
            _call(self.app.ZoomExtents)

    def info(self) -> dict[str, Any]:
        try:
            count = int(_call(lambda: self.msp.Count))
        except Exception:  # noqa: BLE001
            count = -1
        try:
            name = str(_call(lambda: self.doc.Name))
        except Exception:  # noqa: BLE001
            name = "<unnamed>"
        return {
            "backend": self.name,
            "document": name,
            "units": "mm",
            "entities": count,
            "ops": self._ops,
        }

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        try:
            total = int(_call(lambda: self.msp.Count))
        except Exception:  # noqa: BLE001
            return out
        for i in range(total):
            if len(out) >= limit:
                break
            try:
                e = _call(self.msp.Item, i)
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

    # --- primitives --------------------------------------------------------
    def line(self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None) -> Any:
        self._ops += 1
        e = _call(self.msp.AddLine, _point3(p1), _point3(p2))
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
        self._ops += 1
        pts = [xy(p) for p in points]
        e = _call(self.msp.AddLightWeightPolyline, _flat2(pts))
        _call(setattr, e, "Closed", bool(closed))
        self._style(e, layer, linetype)
        return e

    def circle(self, center: Point, radius: float, *, layer: str = "0") -> Any:
        self._ops += 1
        e = _call(self.msp.AddCircle, _point3(center), float(radius))
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
        self._ops += 1
        e = _call(
            self.msp.AddArc,
            _point3(center),
            float(radius),
            math.radians(float(start_deg)),
            math.radians(float(end_deg)),
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
        self._ops += 1
        e = _call(self.msp.AddText, str(content), _point3(pos), float(height))
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
        self._ops += 1
        e = _call(self.msp.AddMText, _point3(pos), float(width or 6000.0), str(content))
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
        self._ops += 1
        # The ActiveX hatch API wants boundary *objects*, so lay down a temporary
        # polyline, hatch it, then remove the boundary again.
        boundary = _call(self.msp.AddLightWeightPolyline, _flat2([xy(p) for p in points]))
        _call(setattr, boundary, "Closed", True)
        pattern_name = "SOLID" if solid else pattern
        h = _call(self.msp.AddHatch, AC_HATCH_PREDEFINED, pattern_name, False)
        loop = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (boundary,))
        try:
            _call(h.AppendOuterLoop, loop)
            if not solid:
                _call(setattr, h, "PatternScale", float(scale))
                _call(setattr, h, "PatternAngle", math.radians(float(angle)))
            _call(h.Evaluate)
        finally:
            # The temporary boundary has to go even if the hatch call failed.
            with contextlib.suppress(Exception):
                _call(boundary.Delete)
        self._style(h, layer)
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
        self._ops += 1
        a, b = xy(p1), xy(p2)
        # AutoCAD measures along the run, so push the dimension line out sideways.
        n = left_normal(unit(sub(b, a)))
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        text_pos = (mid[0] + n[0] * float(offset), mid[1] + n[1] * float(offset))
        e = _call(self.msp.AddDimAligned, _point3(a), _point3(b), _point3(text_pos))
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
        """Refused: this backend has no block library to draw from."""
        raise BackendError(
            "the 'com' backend cannot insert blocks yet; the plugin backend can, and it is the one that keeps the drawing's step journal"
        )

    # --- helpers -----------------------------------------------------------
    def _style(self, entity: Any, layer: str, linetype: str | None = None) -> None:
        # Styling must never abort a drawing that otherwise succeeded.
        with contextlib.suppress(Exception):
            _call(setattr, entity, "Layer", layer)
            if linetype and linetype != "CONTINUOUS":
                _call(setattr, entity, "Linetype", linetype)
