"""The MCP server: the tool surface an AI client talks to.

Every drawing tool is a thin wrapper over :mod:`acadmcp.ops` and a backend, so the
same tool works whether the target is a live AutoCAD session (``backend="com"``), a
headless DXF file (``backend="dxf"``), or the in-AutoCAD step plugin
(``backend="plugin"``), which keeps what it draws in the drawing's own journal so a
step can be reviewed and rolled back.

The ``step_*`` and ``plugin_*`` tools at the end of this file drive that journal.

Run it with ``acadmcp`` or ``python -m acadmcp.server``.
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # mcp 2.x renamed FastMCP to MCPServer; the decorator API is unchanged.
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image

from mcp.types import ToolAnnotations

from . import catalog, geom, layers, ops, settings
from .acad_plugin_client import PluginClient, PluginError, PluginUnavailable
from .backends import make_backend
from .backends.base import Backend, BackendError
from .backends.plugin import PluginBackend
from .catalog import Element
from .steps import StepSession, describe_steps

mcp = _Server("autocad_mcp")

# Tool annotations, one set per behaviour class — the client reads these to decide
# what it may auto-approve, so the rules are:
#   _READONLY   a pure query: no state changes, repeats freely.
#   _IDEMPOTENT a side effect that repeats cleanly: render a file, move the view,
#               save, ensure a layer or text style exists.
#   _MUTATING   additive drawing and journaling: two calls draw twice, but nothing
#               is destroyed — a wrong step is one drawing_undo away.
#   _DESTRUCTIVE            erases entities or orphans the session's drawing
#                           (drawing_undo, step_control, target switches).
#   _DESTRUCTIVE_IDEMPOTENT a target switch that repeats cleanly (drawing_open and
#                           the connects) or the re-runnable installer.
# openWorldHint is False throughout: everything here is local machine state.
_READONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_MUTATING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


class _Session:
    """Holds the one drawing the tools are currently acting on."""

    def __init__(self) -> None:
        self.backend: Backend | None = None
        self.kind: str = "dxf"

    def require(self) -> Backend:
        if self.backend is None:
            # Start a scratch DXF document so tools work without an explicit new/open.
            self.backend = make_backend("dxf")
            self.kind = "dxf"
            ops.setup_layers(self.backend)
        return self.backend

    def reset(self, backend: Backend, kind: str) -> None:
        self.backend = backend
        self.kind = kind


_state = _Session()


@contextlib.contextmanager
def _auto_step(op: str, params: dict[str, Any], label: str | None = None) -> Iterator[Backend]:
    """Wrap one tool call in a step, when the backend journals.

    An explicit step (step_begin) takes precedence: while one is open the
    drawing records into it, and no implicit commit happens. Backends without
    a journal are passed through untouched.

    `label` is human decoration on the step, not a parameter — the history
    renders "{op} {label}", so a room's name belongs there rather than buried
    in the params.
    """
    be = _state.require()
    stepper = getattr(be, "step", None)
    if stepper is None or getattr(be, "open_step", ""):
        yield be
        return
    with stepper(op, label=label, params=params):
        yield be


def _pt(p: Any) -> tuple[float, float]:
    """Accept [x, y], (x, y) or {"x": .., "y": ..} from the client."""
    if isinstance(p, dict):
        return (float(p["x"]), float(p["y"]))
    return (float(p[0]), float(p[1]))


def _pts(points: Any) -> list[tuple[float, float]]:
    return [_pt(p) for p in points]


def _window(window: Any) -> tuple[float, float, float, float] | None:
    """Accept [min_x, min_y, max_x, max_y] or [p1, p2] as a capture window."""
    if not window:
        return None
    values = list(window)
    if len(values) == 4 and not isinstance(values[0], (list, tuple, dict)):
        return tuple(float(v) for v in values)  # type: ignore[return-value]
    if len(values) == 2:
        (x1, y1), (x2, y2) = _pt(values[0]), _pt(values[1])
        return (x1, y1, x2, y2)
    raise BackendError("window must be [min_x, min_y, max_x, max_y] or [p1, p2]")


# ---------------------------------------------------------------------------
# session / document
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
def drawing_help() -> dict[str, Any]:
    """List the available tool groups and the workflow to follow."""
    return {
        "units": "millimetres throughout",
        "backends": {
            "dxf": "headless ezdxf document; no AutoCAD needed, previewable to PNG",
            "com": "drives a running AutoCAD over COM; entities appear live in the editor",
            "plugin": "drives the in-AutoCAD plugin in steps you can review and roll back",
        },
        "workflow": [
            "drawing_new(backend='dxf'|'com', path=...) or drawing_open(path)",
            "just draw — every tool call is one undoable step; drawing_undo/"
            "drawing_redo/drawing_history manage the journal",
            "draw_axis_grid(...) to lay out the structural grid",
            "draw_wall / draw_wall_with_openings / draw_room for the envelope and partitions",
            "draw_door, draw_window, draw_column for openings and structure",
            "place_element / place_along / place_grid to repeat a library element — a frame"
            " of columns, a wall of windows — sized through the block's dynamic parameters",
            "draw_stair, draw_lift, draw_equipment(kind='shaft') for the core and risers",
            "draw_room again with walls=False to label a room whose walls exist",
            "add_dimension / add_text / add_hatch for annotation",
            "drawing_replay(backend='plugin') re-draws a headless session into AutoCAD",
            "drawing_screenshot(...) to eyeball it (inline=False for just a PNG file), "
            "drawing_save(...) to keep it",
        ],
        "element_library": {
            "how": "element_types() lists what the catalogue offers; library_list(source) "
            "lists the blocks in a library .dwg and their dynamic parameters",
            "catalogue": "a JSON file naming the library block, size variants, layer and tag "
            "prefix for each element; a project adds its own via acadmcp.json",
            "tags": "numbered from the tags already in the drawing, never from a stored counter",
        },
        "conventions": {
            "layers": sorted(settings.current().layers or layers.LAYERS),
            "room_area": "computed from the wall centreline ring minus half a wall thickness",
            "angles": "degrees, counter-clockwise from +X",
            "door_swing": "'left' or 'right' relative to the wall direction",
        },
    }


@mcp.tool(annotations=_READONLY)
def drawing_status() -> dict[str, Any]:
    """Report the active backend, document, entity count and layer list.

    `entities` is what is in the drawing now; `ops` is a cumulative count of
    draw calls since connect and does not go down on undo, so ops > entities
    just means steps were taken back.
    """
    be = _state.require()
    return _state.backend.info() if _state.backend else be.info()


@mcp.tool(annotations=_DESTRUCTIVE)
def drawing_new(backend: str = "dxf", path: str | None = None) -> dict[str, Any]:
    """Start a new drawing on the chosen backend.

    backend="dxf" builds a headless document (optionally at `path` — the file is
    written on drawing_save, not read now); backend="com" asks the running
    AutoCAD for a new drawing.

    Starting a new drawing replaces the session's current one; when that one
    had content, the reply carries a `note` saying so.
    """
    dropped = _session_drawing()
    kind = backend.strip().lower()
    if kind == "com":
        be = make_backend("com", new_document=True)
    elif kind == "dxf":
        be = make_backend("dxf", path=path, create=True)
    elif kind == "plugin":
        # The plugin draws into the AutoCAD drawing that is open: it cannot be told
        # to open a file the way the dxf backend can, so say that rather than ignore
        # `path`.
        if path:
            raise BackendError(
                "the plugin backend draws into AutoCAD's active drawing; open the file "
                "in AutoCAD, or use backend='dxf'"
            )
        be = _plugin_backend_connect(new_document=True)
    else:
        raise BackendError(f"unknown backend {backend!r}; expected 'dxf', 'com' or 'plugin'")
    _state.reset(be, kind)
    ops.setup_layers(be)
    return {"ok": True, **be.info(), **_switch_note(dropped)}


@mcp.tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def drawing_open(path: str) -> dict[str, Any]:
    """Open an existing DXF file so further tools edit it.

    Opening replaces the session's current drawing; when that one had content,
    the reply carries a `note` saying so.
    """
    dropped = _session_drawing()
    be = make_backend("dxf", path=path)
    _state.reset(be, "dxf")
    return {"ok": True, **be.info(), **_switch_note(dropped)}


@mcp.tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def acad_connect(
    mode: str = "com", new_document: bool = False, visible: bool = True
) -> dict[str, Any]:
    """Attach to a running AutoCAD (launching and showing it if needed).

    mode="com" draws live over ActiveX; mode="plugin" draws through the step
    plugin, so every step is reviewable and can be rolled back from the palette
    (open one explicitly with step_begin, draw, then step_commit). Call this
    before issuing drawing commands against AutoCAD.
    """
    dropped = _session_drawing()
    kind = mode.strip().lower()
    if kind == "com":
        be = make_backend("com", visible=visible, new_document=new_document)
    elif kind == "plugin":
        be = _plugin_backend_connect(new_document=new_document)
    else:
        raise BackendError(f"unknown mode {mode!r}; expected 'com' or 'plugin'")
    _state.reset(be, kind)
    ops.setup_layers(be)
    return {"ok": True, **be.info(), **_switch_note(dropped)}


def _switch_note(dropped: str) -> dict[str, str]:
    """The `note` a backend switch attaches when it discarded a drawing."""
    if not dropped:
        return {}
    return {
        "note": (
            f"this session was drawing {dropped}, which is now no longer the target; "
            "save it with drawing_save(path=...dxf) first if it is still wanted"
        )
    }


def _session_drawing() -> str:
    """What the session is drawing right now, or "" when there is nothing to lose.

    Switching backend drops the current drawing, and that used to happen silently.
    """
    be = _state.backend
    if be is None:
        return ""
    try:
        info = be.info()
    except Exception:  # noqa: BLE001 - a status probe must never block the switch
        return ""
    count = info.get("entities")
    if not count:
        return ""
    return f"{count} entities in {info.get('path') or info.get('document') or 'an unsaved drawing'}"


def _default_screenshot_path() -> str:
    """Where a screenshot goes when the caller did not name a file.

    Deliberately not the working directory: the server's cwd is somebody's project, and
    a relative default dropped ``screenshot.png`` into a repository root during one run
    of this tool — even for a call that then failed.
    """
    return str(Path(tempfile.gettempdir()) / "acadmcp-screenshot.png")


@mcp.tool(annotations=_IDEMPOTENT)
def acad_zoom(corner1: Any = None, corner2: Any = None) -> dict[str, Any]:
    """Zoom the AutoCAD view: to the whole drawing, or to a rectangle.

    With no corners the view is fitted to all of the drawing. With corners it
    frames that rectangle — home in on a detail before drawing_screenshot, so
    the capture is legible rather than a whole plan at thumbnail scale.
    """
    be = _state.require()
    if corner1 is None or corner2 is None:
        zoom = getattr(be, "zoom_extents", None)
        if zoom is None:
            return {"ok": False, "reason": "only available on the com and plugin backends"}
        zoom()
        return {"ok": True}
    zoom_window = getattr(be, "zoom_window", None)
    if zoom_window is None:
        return {"ok": False, "reason": "only available on the com and plugin backends"}
    zoom_window(_pt(corner1), _pt(corner2))
    return {"ok": True}


@mcp.tool(annotations=_IDEMPOTENT)
def drawing_save(path: str | None = None) -> dict[str, Any]:
    """Save the drawing, to .dwg or .dxf as the path's suffix says.

    On the com backend this saves through AutoCAD, and saving a live drawing to
    .dwg renames that document — pass a .dxf path for a non-destructive copy.
    """
    be = _state.require()
    return {"ok": True, "path": be.save(path)}


@mcp.tool(annotations=_DESTRUCTIVE)
def drawing_undo(count: int = 1) -> dict[str, Any]:
    """Take back the last `count` drawing steps, on any backend.

    A wrong coordinate costs this one call plus the redraw of the single step —
    not a redraw of the whole plan. On the plugin backend this is the drawing's
    own journal rolling back, so the palette and this call never disagree.
    """
    be = _state.require()
    undo = getattr(be, "undo", None)
    if undo is None:
        raise BackendError(f"backend {be.name!r} keeps no journal; nothing can be undone")
    return {"ok": True, **undo(int(count))}


@mcp.tool(annotations=_MUTATING)
def drawing_redo(count: int = 1) -> dict[str, Any]:
    """Re-draw steps that drawing_undo took back (newest work first)."""
    be = _state.require()
    redo = getattr(be, "redo", None)
    if redo is None:
        raise BackendError(f"backend {be.name!r} keeps no journal; nothing can be redone")
    return {"ok": True, **redo(int(count))}


@mcp.tool(annotations=_READONLY)
def drawing_history() -> dict[str, Any]:
    """What has been drawn, one line per step — much cheaper than a screenshot.

    Read this to know the drawing's state before deciding what to undo or add;
    reach for drawing_screenshot only when the geometry itself needs eyeballing.
    """
    be = _state.require()
    history = getattr(be, "history", None)
    if history is None:
        raise BackendError(f"backend {be.name!r} keeps no journal")
    return {"ok": True, "backend": be.name, "rendered": history()}


@mcp.tool(annotations=_DESTRUCTIVE)
def drawing_replay(backend: str = "plugin", path: str | None = None) -> dict[str, Any]:
    """Re-draw the current journal on another backend, step by step.

    The journal is backend-neutral, so a plan drawn headless (fast, previewable)
    replays into live AutoCAD in one call — switching backends no longer means
    redrawing. Each replayed step lands as a real step on the target, so it
    stays undoable there. Steps the target cannot apply (a library insert onto
    the dxf backend) are skipped and reported, never silently dropped.
    """
    source = _state.require()
    journal = getattr(source, "journal", None)
    if journal is None:
        raise BackendError(
            f"backend {source.name!r} keeps no local journal to replay; "
            "the plugin backend's steps live in the DWG — drive them with step_control"
        )
    applicable = [s for s in journal.steps if s.state == "applied"]

    kind = backend.strip().lower()
    if kind == "plugin":
        target = _plugin_backend_connect(new_document=True)
    elif kind == "dxf":
        if not path:
            raise BackendError("drawing_replay(backend='dxf') needs a path to draw into")
        target = make_backend("dxf")
        target.path = path
    else:
        raise BackendError(f"cannot replay onto backend {backend!r}; expected 'plugin' or 'dxf'")

    ops.setup_layers(target)
    failed: list[dict[str, Any]] = []
    for record in applicable:
        try:
            target.replay_step(record)
        except BackendError as exc:
            failed.append({"index": record.index, "op": record.op, "reason": str(exc)})
    if kind == "dxf":
        target.save(path)
    dropped = _session_drawing()
    _state.reset(target, kind)
    return {
        "ok": True,
        "backend": kind,
        "replayed": len(applicable) - len(failed),
        "failed": failed,
        **({"path": path} if kind == "dxf" else {}),
        **_switch_note(dropped),
    }


@mcp.tool(annotations=_IDEMPOTENT)
def drawing_screenshot(
    path: str | None = None,
    window: Any = None,
    width: int = 1600,
    height: int = 1200,
    theme: str = "dark",
    inline: bool = True,
) -> Any:
    """Look at the drawing. Returns an image you can see directly.

    This is the tool to call after drawing something, to check the result before
    going further or to verify a fix. Pass `window` to frame a region rather than
    the whole drawing — a full floor plan shrunk to 1600 px leaves the room
    labels unreadable, so zoom in when you need to read text.

    `window` is [min_x, min_y, max_x, max_y] in millimetres, or [p1, p2].
    inline=False returns only the PNG's path, for scripts that need a file on
    disk rather than a picture to look at.

    What you get depends on the backend: with backend="dxf" this re-renders the
    drawing cleanly, white-on-dark or black-on-white. With backend="com" it
    photographs the live AutoCAD window, so you see the true current view and
    zoom, but also AutoCAD's ribbon and panels around it.
    """
    be = _state.require()
    target = path or _default_screenshot_path()
    out = be.screenshot(
        target,
        window=_window(window),
        width=int(width),
        height=int(height),
        theme=theme,
    )
    if not inline:
        return {"ok": True, "path": out}
    return Image(path=out)


@mcp.tool(annotations=_READONLY)
def listing_entities(layer: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List entities, optionally filtered to one layer.

    `has_more` tells you `limit` cut the list short; call again with a larger
    limit for the rest. With a `layer` filter the answer also carries
    `layer_known`: False means the layer name is not in the drawing at all,
    which is how a typo is told apart from an empty layer.
    """
    be = _state.require()
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    probe = be.entities(layer=layer, limit=int(limit) + 1)
    out: dict[str, Any] = {
        "count": min(len(probe), int(limit)),
        "has_more": len(probe) > int(limit),
        "entities": probe[: int(limit)],
    }
    if layer is not None:
        known = be.info().get("layers")
        if known is not None:
            out["layer_known"] = layer in known
    return out


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_IDEMPOTENT)
def layer_create(
    name: str | None = None, color: int | None = None, linetype: str | None = None
) -> dict[str, Any]:
    """Create a layer (no-op if it already exists) with an ACI colour and linetype.

    With no name, creates the full standard architectural layer set
    (A-WALL, A-DOOR, ...) instead.
    """
    be = _state.require()
    if name is None:
        return {"ok": True, "layers": ops.setup_layers(be)}
    be.ensure_layer(name, color=color, linetype=linetype)
    return {"ok": True, "layer": name}


@mcp.tool(annotations=_IDEMPOTENT)
def set_text_style(
    name: str = "HZ", font: str = "simhei.ttf", big_font: str | None = None
) -> dict[str, Any]:
    """Point drawn text at a text style with a font that has CJK glyphs.

    A stock AutoCAD drawing's Standard style is txt.shx with no big font, and SHX
    fonts carry no Chinese characters, so text drawn without this comes out as
    "?". TrueType (the default here) needs no big font; pass both a font and a
    big font (e.g. gbenor.shx with gbcbig.shx) to match a client's own standard.
    """
    be = _state.require()
    setter = getattr(be, "set_text_style", None)
    if setter is None:
        raise BackendError(f"backend {be.name!r} has no text styles")
    return {"ok": True, "style": setter(name, font, big_font)}


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_line(p1: Any, p2: Any, layer: str = "0", linetype: str | None = None) -> dict[str, Any]:
    """Draw a straight line between two points."""
    a, b = _pt(p1), _pt(p2)
    params = {"p1": a, "p2": b, "layer": layer, "linetype": linetype}
    with _auto_step("draw_line", params) as be:
        be.line(a, b, layer=layer, linetype=linetype)
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_polyline(points: Any, closed: bool = False, layer: str = "0") -> dict[str, Any]:
    """Draw a polyline through a list of points."""
    pts = _pts(points)
    params = {"points": pts, "closed": closed, "layer": layer}
    with _auto_step("draw_polyline", params) as be:
        be.polyline(pts, closed=closed, layer=layer)
    return {"ok": True, "vertices": len(pts), "closed": closed}


@mcp.tool(annotations=_MUTATING)
def draw_rect(corner1: Any, corner2: Any, layer: str = "0") -> dict[str, Any]:
    """Draw an axis-aligned rectangle from two opposite corners."""
    a, b = _pt(corner1), _pt(corner2)
    params = {"corner1": a, "corner2": b, "layer": layer}
    with _auto_step("draw_rect", params) as be:
        be.polyline(geom.rect_corners(a, b), closed=True, layer=layer)
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_circle(
    center: Any,
    radius: float,
    start_deg: float | None = None,
    end_deg: float | None = None,
    layer: str = "0",
) -> dict[str, Any]:
    """Draw a circle — or an arc, when start_deg and end_deg are given.

    Angles are degrees, counter-clockwise from +X.
    """
    if radius <= 0:
        raise ValueError(f"a circle needs a positive radius, got {radius}")
    c = _pt(center)
    params = {
        "center": c,
        "radius": float(radius),
        "start_deg": start_deg,
        "end_deg": end_deg,
        "layer": layer,
    }
    with _auto_step("draw_circle", params) as be:
        if start_deg is None or end_deg is None:
            be.circle(c, float(radius), layer=layer)
        else:
            be.arc(c, float(radius), float(start_deg), float(end_deg), layer=layer)
    return {"ok": True}


# ---------------------------------------------------------------------------
# walls, rooms, openings
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_wall(
    points: Any,
    thickness: float = layers.WALL_THICKNESS,
    closed: bool = False,
    hatch: bool = False,
    layer: str = "A-WALL",
) -> dict[str, Any]:
    """Draw a wall as two offset faces along a centreline polyline.

    Set closed=True for a room envelope, hatch=True to poché the wall.
    thickness=120 makes a light internal partition.
    """
    pts = _pts(points)
    params = {
        "points": pts,
        "thickness": float(thickness),
        "closed": closed,
        "hatch": hatch,
        "layer": layer,
    }
    with _auto_step("draw_wall", params) as be:
        ops.draw_wall_run(be, pts, float(thickness), closed=closed, hatch=hatch, layer=layer)
    # A closed ring's closing edge is a segment too; len(pts) - 1 undercounts it,
    # and the ring length is only a perimeter when the repeat leg is included.
    ring = pts[:-1] if closed and geom.is_closed(pts) else pts
    measured = [*ring, ring[0]] if closed else ring
    return {
        "ok": True,
        "segments": len(measured) - 1,
        "thickness": float(thickness),
        "length": round(geom.polyline_length(measured), 3),
    }


@mcp.tool(annotations=_MUTATING)
def draw_wall_with_openings(
    points: Any,
    openings: Any,
    thickness: float = layers.WALL_THICKNESS,
    hatch: bool = False,
    closed: bool = False,
) -> dict[str, Any]:
    """Draw a wall run and cut door/window openings into it in one step.

    `openings` is a list of {"start": mm along the run, "width": mm,
    "kind": "door"|"window"|"opening", "swing": "left"|"right", "tag": "M1522"}.
    This is the tool to reach for when reproducing a real plan: it keeps the
    wall faces and the opening symbols consistent.

    Pass `closed=True` for a building envelope, with `points` as the ring: the corners
    are then mitred into each other. Drawing the envelope as four separate runs instead
    leaves a half-wall-thickness notch at every corner, because a run has two square-cut
    ends and each one lands on a corner.

    The returned `symbols` counts every entity of every symbol, tag texts
    included, so a tagged door counts more than an untagged one.
    """
    pts = _pts(points)
    params = {
        "points": pts,
        "openings": openings,
        "thickness": float(thickness),
        "hatch": hatch,
        "closed": closed,
    }
    with _auto_step("draw_wall_with_openings", params) as be:
        result = ops.draw_wall_with_openings(
            be, pts, openings, float(thickness), hatch=hatch, closed=closed
        )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def draw_room(
    boundary: Any,
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    hatch: bool = False,
    show_area: bool = True,
    bound_layer: str | None = None,
    walls: bool = True,
) -> dict[str, Any]:
    """Wall a room in, then label it with its name and computed area in m².

    `boundary` is the wall centreline ring as a list of points. Pass `bound_layer`
    (e.g. "A-ROOM-BOUND") to also draw the face the area is measured to. With
    walls=False only the label (and optional bound face) is drawn — for a room
    whose walls exist already. `thickness` must match the walls actually drawn:
    the default 200 against a 120 mm partition ring silently shrinks the area.
    """
    pts = _pts(boundary)
    params = {
        "boundary": pts,
        "name": name,
        "thickness": float(thickness),
        "hatch": hatch,
        "show_area": show_area,
        "bound_layer": bound_layer,
        "walls": walls,
    }
    # The name is the step's label as well as a parameter, so the history reads
    # "draw_room 测试室" rather than a wall of coordinates.
    with _auto_step("draw_room", params, label=name) as be:
        if walls:
            result = ops.draw_room(
                be,
                pts,
                name,
                float(thickness),
                hatch=hatch,
                show_area=show_area,
                bound_layer=bound_layer,
            )
        else:
            result = ops.add_room_label(
                be, pts, name, float(thickness), show_area=show_area, bound_layer=bound_layer
            )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def draw_door(
    hinge: Any,
    width: float = layers.DOOR_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    rotation: float = 0.0,
    open_angle: float = 90.0,
    swing: str = "left",
    tag: str | None = None,
) -> dict[str, Any]:
    """Draw a single-leaf door: jambs, leaf and swing arc.

    `hinge` is the hinge-side jamb on the wall centreline; `rotation` is the wall
    direction in degrees; swing is "left" or "right".
    """
    hinge_pt = _pt(hinge)
    params = {
        "hinge": hinge_pt,
        "width": float(width),
        "wall_thickness": float(wall_thickness),
        "rotation": float(rotation),
        "open_angle": float(open_angle),
        "swing": swing,
        "tag": tag,
    }
    with _auto_step("draw_door", params) as be:
        ops.draw_door(
            be,
            hinge_pt,
            float(width),
            float(wall_thickness),
            rotation=float(rotation),
            open_angle=float(open_angle),
            swing=swing,
            tag=tag,
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_window(
    start: Any,
    width: float = layers.WINDOW_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    rotation: float = 0.0,
    tag: str | None = None,
) -> dict[str, Any]:
    """Draw a window symbol spanning an opening, starting at the given jamb."""
    start_pt = _pt(start)
    params = {
        "start": start_pt,
        "width": float(width),
        "wall_thickness": float(wall_thickness),
        "rotation": float(rotation),
        "tag": tag,
    }
    with _auto_step("draw_window", params) as be:
        ops.draw_window(
            be, start_pt, float(width), float(wall_thickness), rotation=float(rotation), tag=tag
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_column(
    center: Any,
    size: float = layers.COLUMN_SIZE,
    depth: float | None = None,
    rotation: float = 0.0,
    hatch: bool = True,
) -> dict[str, Any]:
    """Draw a structural column as a rotated, optionally filled rectangle."""
    c = _pt(center)
    params = {
        "center": c,
        "size": float(size),
        "depth": depth,
        "rotation": float(rotation),
        "hatch": hatch,
    }
    with _auto_step("draw_column", params) as be:
        ops.draw_column(be, c, float(size), rotation=float(rotation), depth=depth, hatch=hatch)
    return {"ok": True}


# ---------------------------------------------------------------------------
# core & equipment
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_stair(
    corner1: Any,
    corner2: Any,
    steps: int = 9,
    axis: str = "x",
    up_label: str | None = "\u4e0a",
    down_label: str | None = "\u4e0b",
) -> dict[str, Any]:
    """Draw a stair flight: outline, treads, direction arrow and 上/下 labels.

    `axis` is the travel direction ("x" or "y").
    """
    a, b = _pt(corner1), _pt(corner2)
    params = {
        "corner1": a,
        "corner2": b,
        "steps": int(steps),
        "axis": axis,
        "up_label": up_label,
        "down_label": down_label,
    }
    with _auto_step("draw_stair", params) as be:
        result = ops.draw_stair(
            be,
            a,
            b,
            steps=int(steps),
            axis=axis,
            up_label=up_label,
            down_label=down_label,
        )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def draw_lift(corner1: Any, corner2: Any, tag: str | None = None) -> dict[str, Any]:
    """Draw a lift/elevator shaft with its car outline and X cross."""
    a, b = _pt(corner1), _pt(corner2)
    params = {"corner1": a, "corner2": b, "tag": tag}
    with _auto_step("draw_lift", params) as be:
        ops.draw_lift(be, a, b, tag=tag)
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_equipment(
    corner1: Any, corner2: Any, name: str | None = None, kind: str = "equipment"
) -> dict[str, Any]:
    """Draw a rectangular piece of fixed equipment with a caption.

    kind="shaft" draws a service riser instead (强电井/弱电井/水井): the same
    captioned rectangle plus a diagonal cross, on A-SHAFT.
    """
    a, b = _pt(corner1), _pt(corner2)
    params = {"corner1": a, "corner2": b, "name": name, "kind": kind}
    with _auto_step("draw_equipment", params) as be:
        if kind == "shaft":
            ops.draw_shaft(be, a, b, name=name)
        else:
            ops.draw_equipment(be, a, b, name=name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_axis_grid(
    x_positions: Any,
    y_positions: Any,
    extent: Any,
    x_labels: Any = None,
    y_labels: Any = None,
    bubble_r: float = layers.AXIS_BUBBLE_R,
) -> dict[str, Any]:
    """Draw the structural grid: centre lines, bubbles and axis labels.

    `extent` is [min_x, min_y, max_x, max_y]. x_labels default to 1,2,3...,
    y_labels to A,B,C...
    """
    xs = [float(v) for v in x_positions]
    ys = [float(v) for v in y_positions]
    ex = [float(v) for v in extent]
    if len(ex) != 4:
        raise ValueError(f"extent needs [min_x, min_y, max_x, max_y], got {len(ex)} numbers")
    params = {
        "x_positions": xs,
        "y_positions": ys,
        "extent": ex,
        "x_labels": x_labels,
        "y_labels": y_labels,
        "bubble_r": float(bubble_r),
    }
    with _auto_step("draw_axis_grid", params) as be:
        result = ops.draw_axis_grid(
            be,
            xs,
            ys,
            x_labels=x_labels,
            y_labels=y_labels,
            extent=(ex[0], ex[1], ex[2], ex[3]),
            bubble_r=float(bubble_r),
        )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def add_text(
    pos: Any,
    text: str,
    height: float = layers.H_TEXT,
    rotation: float = 0.0,
    align: str = "left",
    layer: str = "A-ANNO",
) -> dict[str, Any]:
    """Place single-line text. align is left, center, right or top-center."""
    pos_pt = _pt(pos)
    params = {
        "pos": pos_pt,
        "text": text,
        "height": float(height),
        "rotation": float(rotation),
        "align": align,
        "layer": layer,
    }
    with _auto_step("add_text", params) as be:
        ops.annotate(
            be,
            pos_pt,
            text,
            height=float(height),
            rotation=float(rotation),
            layer=layer,
            align=align,
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def add_leader_note(
    anchor: Any,
    text: str,
    direction: float = 45.0,
    length: float = 1500.0,
    height: float = layers.H_TEXT,
) -> dict[str, Any]:
    """Draw a one-segment leader from `anchor` with the note at its far end.

    Anchor on the boundary of what is being noted: an anchor inside a SOLID
    hatch disappears under the fill.
    """
    anchor_pt = _pt(anchor)
    params = {
        "anchor": anchor_pt,
        "text": text,
        "direction": float(direction),
        "length": float(length),
        "height": float(height),
    }
    with _auto_step("add_leader_note", params) as be:
        ops.leader_note(
            be,
            anchor_pt,
            text,
            direction=float(direction),
            length=float(length),
            height=float(height),
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def add_dimension(points: Any, offset: float = layers.DIM_OFFSET) -> dict[str, Any]:
    """Add an aligned dimension, offset sideways by `offset` mm.

    Two points give a single dimension; three or more give a chain along the
    run, plus the overall dimension. The dimension line lands on the LEFT of
    the travel direction for a positive `offset` — so a south edge dimensioned
    east-to-west needs a negative offset to fall outside the building.
    """
    pts = _pts(points)
    params = {"points": pts, "offset": float(offset)}
    with _auto_step("add_dimension", params) as be:
        if len(pts) == 2:
            ops.dimension_between(be, pts[0], pts[1], float(offset))
            result: dict[str, Any] = {}
        else:
            result = ops.dimension_chain(be, pts, float(offset))
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def add_hatch(
    points: Any,
    pattern: str = "ANSI31",
    scale: float = 40.0,
    angle: float = 0.0,
    solid: bool = False,
    layer: str = "A-HATCH",
) -> dict[str, Any]:
    """Hatch a closed boundary given as points (pattern, or solid fill).

    `scale` multiplies the pattern's own base spacing, which differs per
    pattern: ANSI31's base is ≈5 mm, so scale=40 gives the ≈130 mm pitch of a
    wall poché, while AR-HBONE starts far larger and wants a single-digit
    scale. `angle` is added to the pattern's own angle — ANSI31 is already
    45°, so angle=45 makes it vertical. `pattern` must be one of the standard
    acad.pat names (ANSI31…ANSI38, AR-*, NET, …); a misspelt name raises
    rather than drawing a pattern that does not exist.
    """
    pts = _pts(points)
    if not solid and pattern.upper() not in layers.HATCH_PATTERNS:
        raise ValueError(
            f"unknown hatch pattern {pattern!r}; the standard names are "
            "ANSI31…ANSI38, AR-*, NET, SOLID and the rest of acad.pat"
        )
    params = {
        "points": pts,
        "pattern": pattern,
        "scale": float(scale),
        "angle": float(angle),
        "solid": solid,
        "layer": layer,
    }
    with _auto_step("add_hatch", params) as be:
        be.hatch(pts, pattern, float(scale), layer=layer, angle=float(angle), solid=solid)
    return {"ok": True, "pattern": "SOLID" if solid else pattern}


# --------------------------------------------------------------------------- #
# The in-AutoCAD step plugin
# --------------------------------------------------------------------------- #
#
# Drawing through the plugin is not like drawing through the other backends: a step
# has to be opened and closed around the drawing calls, so that everything in
# between becomes one reviewable step rather than a pile of loose entities. The
# usual drawing tools do the work; these open, close and drive the journal.

_plugin_client: PluginClient | None = None


def _client() -> PluginClient:
    """The one connection to the plugin, created on first use."""
    global _plugin_client
    if _plugin_client is None:
        _plugin_client = PluginClient()
    return _plugin_client


def _plugin_backend_connect(new_document: bool = False) -> PluginBackend:
    """Attach to the plugin, optionally opening a fresh drawing in AutoCAD."""
    try:
        client = _client().connect()
        if new_document:
            client.call("new_document")
    except PluginUnavailable as exc:
        raise BackendError(
            f"{exc}. Install the plugin with plugin_install, start AutoCAD, and then "
            "check plugin_status."
        ) from exc

    backend = PluginBackend(client=client)
    backend.forget_layers()
    backend.set_units_mm()
    return backend


def _require_plugin() -> PluginBackend:
    backend = _state.backend
    if not isinstance(backend, PluginBackend):
        raise BackendError(
            "the step tools draw through the plugin; call acad_connect(mode='plugin') first"
        )
    return backend


def _entry(key: str) -> Element:
    """The catalogue entry for this key, or a refusal that lists what there is."""
    element = catalog.current().get(key)
    if element is None:
        raise BackendError(
            f"no element '{key}' in the catalogue; it has: {', '.join(catalog.current().keys())}"
        )
    return element


def _placed(be: PluginBackend, op: str, label: str, params: dict[str, Any], work: Any) -> dict:
    """Run one placement call as a single step, the way drawing through the plugin works."""
    be.begin_step(op, label=label, params=params)
    try:
        summary = work()
    except BaseException:
        be.abort_step()
        raise
    outcome = _outcome(be.commit_step(), "apply")
    return {**outcome, **summary} if isinstance(summary, dict) else outcome


def _placement(be: PluginBackend, op: str, label: str, params: dict[str, Any], work: Any) -> dict:
    """Like `_placed`, but a bad catalogue argument comes back as a refusal to the caller.

    A variant name that does not exist is a caller's mistake, not a drawing failure, and the
    catalogue raises to say so; this is where that becomes a message rather than a traceback.
    """
    try:
        return _placed(be, op, label, params, work)
    except ValueError as exc:
        raise BackendError(str(exc)) from exc


def _outcome(response: dict[str, Any], action: str) -> dict[str, Any]:
    """Flatten the plugin's reply into something a reader can use."""
    result = response.get("result", {})
    return {
        "ok": bool(result.get("ok")),
        "action": result.get("action", action),
        "message": result.get("message", ""),
        "touched": result.get("touched", []),
        "step": result.get("step"),
        "refusal": result.get("refusal"),
        "journal": response.get("status", {}),
    }


@mcp.tool(annotations=_READONLY)
def plugin_status(with_steps: bool = True) -> dict[str, Any]:
    """Report the in-AutoCAD step plugin: whether it is there, and what it holds.

    This answers even while AutoCAD is busy — the plugin's ping does not touch the
    drawing — so it is also how "AutoCAD is stuck" is told apart from "the plugin is
    not loaded".
    """
    try:
        pong = _client().ping()
    except PluginError as exc:
        return {"ok": False, "available": False, "reason": str(exc)}

    session = StepSession(_client())
    try:
        status = session.status()
    except PluginError as exc:
        return {"ok": False, "available": True, "reason": str(exc), **pong}

    out: dict[str, Any] = {"ok": True, "available": True, **pong, **status}
    if with_steps:
        out["steps"] = session.steps()
        out["rendered"] = describe_steps(out["steps"])
    return out


@mcp.tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def plugin_install(force: bool = False) -> dict[str, Any]:
    """Build and install the AutoCAD plugin into the per-user plug-in folder.

    Windows only, and only from a checkout of this repository: it runs
    plugin/scripts/install.sh, which needs the .NET 8 SDK and wants AutoCAD closed
    (pass force=True to let it close a running AutoCAD). It also adds the folder to
    AutoCAD's TRUSTEDPATHS, without which an unsigned plugin raises a security
    prompt on every load.
    """
    script = Path(__file__).resolve().parents[2] / "plugin" / "scripts" / "install.sh"
    if not script.is_file():
        return {
            "ok": False,
            "reason": (
                f"no installer at {script}: plugin_install needs a checkout of this "
                "repository, not an installed copy of the package"
            ),
        }

    command = ["bash", str(script)] + (["--force"] if force else [])
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": f"could not run {script.name}: {exc}"}
    return {
        "ok": done.returncode == 0,
        "returncode": done.returncode,
        "output": (done.stdout + done.stderr).strip(),
    }


@mcp.tool(annotations=_MUTATING)
def step_begin(
    op: str, label: str | None = None, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Open a step: everything drawn until step_commit becomes that one step.

    `op` names the operation the step came from and `params` records the arguments
    that produced it, so the step can be read back and reproduced later. Open a step,
    call draw_room, then step_commit — and the room is one step the user can roll
    back in a single action.
    """
    backend = _require_plugin()
    backend.begin_step(op, label=label, params=params)
    return {
        "ok": True,
        "op": op,
        "label": label or "",
        "hint": "draw with the ordinary tools, then call step_commit",
    }


@mcp.tool(annotations=_MUTATING)
def step_commit(planned: bool = False) -> dict[str, Any]:
    """Close the step opened by step_begin.

    planned=False (the default) draws it now and journals it as applied.
    planned=True only records it, to be drawn later — by the user pressing Run next
    in the palette, or by step_control("run_next") — which is how a whole plan is
    laid out first and drawn one step at a time.
    """
    backend = _require_plugin()
    if not backend.pending:
        return {
            "ok": False,
            "reason": (
                f"step {backend.open_step or '(none)'} recorded no primitives: nothing "
                "was drawn between step_begin and step_commit"
            ),
        }
    response = backend.plan_step() if planned else backend.commit_step()
    return _outcome(response, "plan" if planned else "apply")


@mcp.tool(annotations=_READONLY)
def step_list() -> dict[str, Any]:
    """List the steps in the drawing's journal, oldest first."""
    try:
        rows = StepSession(_client().connect()).steps()
    except PluginError as exc:
        raise BackendError(str(exc)) from exc
    return {"ok": True, "count": len(rows), "steps": rows, "rendered": describe_steps(rows)}


@mcp.tool(annotations=_DESTRUCTIVE)
def step_control(
    action: str,
    index: int | None = None,
    force: bool = False,
    note: str | None = None,
    patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive the step journal.

    Actions, and what each one needs:
      run_next / run_all   — run the next step, or every remaining one
      run_to               — index: run up to and including that step
      rollback_to          — index: erase everything after it, back to planned
      reject               — index: drop it and everything after it
      accept / unaccept    — index: mark reviewed, so a rollback will not pass it
      note                 — index, note: annotate a step
      update               — index, patch: change a step's params, label or note
      snapshot             — record a reviewed baseline at the end
      clear_plan / reset / reload

    rollback_to is refused when an accepted step stands in the way; force overrides
    that refusal, and the result says so.
    """
    session = StepSession(_client().connect())
    try:
        if action == "rollback_to":
            if index is None:
                raise BackendError("rollback_to needs an index")
            response = session.rollback_to(int(index), force=bool(force))
        else:
            arguments: dict[str, Any] = {}
            if index is not None:
                arguments["index"] = int(index)
            if note is not None:
                arguments["note"] = note
            if patch is not None:
                arguments["patch"] = patch
            response = session.control(action, **arguments)
    except (PluginError, ValueError) as exc:
        raise BackendError(str(exc)) from exc
    return _outcome(response, action)


@mcp.tool(annotations=_READONLY)
def library_list(source: str | None = None, limit: int = 500) -> dict[str, Any]:
    """List the block definitions in the open drawing, or in a library .dwg.

    This is what the element catalogue is written against, so it is also how a catalogue
    that has drifted from the library is caught. Dynamic blocks come back with their
    parameters, the values each accepts and which are read-only. A library file is opened
    read-only on the side and never becomes a reference to it.

    `has_more` tells you `limit` cut the list short; call again with a larger
    limit for the rest.
    """
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    try:
        result = _client().call("blocks", source=source, limit=int(limit) + 1)
    except (PluginError, PluginUnavailable) as exc:
        raise BackendError(str(exc)) from exc
    blocks = result.get("blocks", [])
    result["blocks"] = blocks[: int(limit)]
    result["count"] = min(len(blocks), int(limit))
    result["has_more"] = len(blocks) > int(limit)
    return {"ok": True, **result}


@mcp.tool(annotations=_READONLY)
def element_types(category: str | None = None, check_library: bool = True) -> dict[str, Any]:
    """What the element catalogue offers, and whether the library still matches it.

    The catalogue is a plain JSON file naming, for each element, which library block stands
    for it and which parameters select a size. A project supplies its own file — an
    institute's model numbers and wall thicknesses are its own — and this reports anything
    that could not be read, anything it had to skip, and every entry whose block the
    library has not got.
    """
    loaded = catalog.current()
    out: dict[str, Any] = {
        "ok": True,
        "count": len(loaded.elements),
        "skipped": loaded.skipped,
        "library": {str(path): library for path, library in loaded.libraries.items()},
        "categories": {
            name: [element.describe() for element in items]
            for name, items in loaded.by_category(category).items()
        },
    }
    if check_library:
        problems = _check_catalog(loaded)
        out["problems"] = problems
        out["matches_library"] = not problems
    return out


def _check_catalog(loaded: catalog.Catalog) -> list[str]:
    """Compare the catalogue against the libraries it names, reading each one once."""
    try:
        client = _client()
    except (PluginError, PluginUnavailable):
        return ["AutoCAD is not reachable, so the catalogue could not be checked"]

    known: dict[str, list[str]] = {}
    for source in loaded.sources():
        if source in known:
            continue
        try:
            known[source] = [
                block["name"] for block in client.call("blocks", source=source)["blocks"]
            ]
        except PluginError as exc:
            return [f"could not read '{source}': {exc}"]
    return loaded.check_against(known)


@mcp.tool(annotations=_MUTATING)
def place_element(
    type: str,  # noqa: A002 - the tool surface speaks the caller's language
    at: Any,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    tag: bool = True,
    tag_offset: float | None = None,
) -> dict[str, Any]:
    """Place one catalogue element from the library, as a single step.

    The block's definition is imported into the drawing if it is not there yet, and the
    reference is placed at `at`. `variant` picks a size from the catalogue ("900" for a
    900 mm door) and `params` overrides individual dynamic properties on top. With
    tag=True the next free tag number is derived from the tags already in the drawing —
    numbers are never stored, so a tag added by hand cannot desynchronise them.
    """
    be = _require_plugin()
    element = _entry(type)

    def work() -> list[Any]:
        tags = ops._numbering(be, element, 1) if tag else []
        return ops.place_element(
            be,
            element,
            geom.xy(at),
            variant=variant,
            params=params,
            rotation=float(rotation),
            scale=float(scale),
            tag=tags[0] if tags else None,
            tag_offset=tag_offset,
        )

    return _placement(
        be,
        "place_element",
        f"{element.label} {variant or element.default_variant}".strip(),
        {"type": element.key, "at": geom.xy(at), "variant": variant, "params": params or {}},
        work,
    )


@mcp.tool(annotations=_MUTATING)
def place_along(
    type: str,  # noqa: A002
    start: Any,
    end: Any,
    count: int | None = None,
    spacing: float | None = None,
    inset: float = 0.0,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float | None = None,
    tag: bool = True,
) -> dict[str, Any]:
    """Repeat a catalogue element along a run, as one step.

    Give either `count` (that many, evenly spread, ends included) or `spacing` (every that
    far, starting at `start`). `inset` pulls the ends in, which is what keeps a column off
    a corner. `rotation` defaults to the direction of the run, so a row of doors lies in
    the wall rather than all facing the same way.
    """
    be = _require_plugin()
    element = _entry(type)
    return _placement(
        be,
        "place_along",
        f"{len(_positions(start, end, count, spacing, inset))} x {element.label}",
        {
            "type": element.key,
            "start": geom.xy(start),
            "end": geom.xy(end),
            "count": count,
            "spacing": spacing,
        },
        lambda: ops.place_along(
            be,
            element,
            geom.xy(start),
            geom.xy(end),
            count=count,
            spacing=spacing,
            inset=float(inset),
            rotation=None if rotation is None else float(rotation),
            variant=variant,
            params=params,
            tag=tag,
        ),
    )


@mcp.tool(annotations=_MUTATING)
def place_grid(
    type: str,  # noqa: A002
    xs: list[float],
    ys: list[float],
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    tag: bool = True,
) -> dict[str, Any]:
    """Place a catalogue element at every intersection of two coordinate lists.

    A frame of columns is dimensioned to its axis positions, so those are what this takes:
    the same `xs` and `ys` the axis grid was drawn to.
    """
    be = _require_plugin()
    element = _entry(type)
    return _placement(
        be,
        "place_grid",
        f"{len(xs) * len(ys)} x {element.label}",
        {"type": element.key, "xs": list(xs), "ys": list(ys)},
        lambda: ops.place_grid(
            be,
            element,
            list(xs),
            list(ys),
            variant=variant,
            params=params,
            rotation=float(rotation),
            tag=tag,
        ),
    )


def _positions(start: Any, end: Any, count: int | None, spacing: float | None, inset: float):
    """The points a run would place at, for the step's label. Raises the same way the op does."""
    return catalog.positions_along(
        geom.xy(start), geom.xy(end), count=count, spacing=spacing, inset=float(inset)
    )


def _strict_tool_arguments() -> None:
    """Reject unknown tool arguments instead of dropping them silently.

    The SDK builds each tool's pydantic input model with the default
    extra="ignore", so a misspelt parameter is discarded and the tool runs on
    defaults — a stale server answering a newer client fails exactly that way,
    and the drawing comes out wrong with ok=True. Forbidding extras turns it
    into a validation error that names the field.
    """
    for tool in mcp._tool_manager.list_tools():
        model = getattr(getattr(tool, "fn_metadata", None), "arg_model", None)
        if model is None:
            continue
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)


_strict_tool_arguments()


def _surface_input_errors() -> None:
    """Let a tool's ValueError reach the client with its message intact.

    Bad geometry input — one distinct point for a wall, a room ring that
    self-intersects — is raised by geom/ops as a plain ValueError, and the SDK
    masks anything that is not a ToolError behind "Error executing tool
    <name>", so the model never learns what was wrong with its own arguments.
    Re-raising as BackendError (a ToolError) keeps the message after that
    prefix — the SDK re-wraps even ToolError, so the tool name appears twice
    on the wire, which is cosmetic and not worth fighting. Wrapped here, once,
    rather than in 44 tool bodies.
    """
    for tool in mcp._tool_manager.list_tools():
        fn = tool.fn

        def guarded(*args: Any, _fn: Any = fn, **kwargs: Any) -> Any:
            try:
                return _fn(*args, **kwargs)
            except ValueError as exc:
                raise BackendError(str(exc)) from exc

        tool.fn = guarded


_surface_input_errors()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
