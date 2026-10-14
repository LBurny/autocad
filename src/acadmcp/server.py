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

import subprocess
from pathlib import Path
from typing import Any

try:  # mcp 2.x renamed FastMCP to MCPServer; the decorator API is unchanged.
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image

from . import catalog, geom, layers, ops, settings
from .acad_plugin_client import PluginClient, PluginError, PluginUnavailable
from .backends import make_backend
from .backends.base import Backend, BackendError
from .backends.plugin import PluginBackend
from .catalog import Element
from .steps import StepSession, describe_steps

mcp = _Server("AutoCAD-MCP")


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


@mcp.tool()
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
            "plugin_connect() + step_begin/step_commit to draw in AutoCAD as steps",
            "draw_axis_grid(...) to lay out the structural grid",
            "draw_wall / draw_wall_with_openings / draw_room for the envelope and partitions",
            "draw_door, draw_window, draw_column for openings and structure",
            "place_element / place_along / place_grid to repeat a library element — a frame"
            " of columns, a wall of windows — sized through the block's dynamic parameters",
            "draw_stair, draw_lift, draw_shaft for the core and risers",
            "draw_room / add_room_label for names and computed areas",
            "add_dimension / add_dimension_chain, annotate for annotation",
            "drawing_preview(...) to eyeball it, drawing_save(...) to keep it",
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


@mcp.tool()
def drawing_status() -> dict[str, Any]:
    """Report the active backend, document, entity count and layer list."""
    be = _state.require()
    return _state.backend.info() if _state.backend else be.info()


@mcp.tool()
def drawing_new(backend: str = "dxf", path: str | None = None) -> dict[str, Any]:
    """Start a new drawing on the chosen backend.

    backend="dxf" builds a headless document (optionally at `path`);
    backend="com" asks the running AutoCAD for a new drawing.
    """
    kind = backend.strip().lower()
    if kind == "com":
        be = make_backend("com", new_document=True)
    elif kind == "dxf":
        be = make_backend("dxf", path=path)
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
    return {"ok": True, **be.info()}


@mcp.tool()
def drawing_open(path: str) -> dict[str, Any]:
    """Open an existing DXF file so further tools edit it."""
    be = make_backend("dxf", path=path)
    _state.reset(be, "dxf")
    return {"ok": True, **be.info()}


@mcp.tool()
def acad_connect(visible: bool = True, new_document: bool = False) -> dict[str, Any]:
    """Attach to a running AutoCAD (launching and showing it if needed).

    Call this before issuing drawing commands with backend="com".
    """
    be = make_backend("com", visible=visible, new_document=new_document)
    _state.reset(be, "com")
    ops.setup_layers(be)
    return {"ok": True, **be.info()}


@mcp.tool()
def acad_zoom_extents() -> dict[str, Any]:
    """Zoom the AutoCAD view to fit the whole drawing."""
    be = _state.require()
    zoom = getattr(be, "zoom_extents", None)
    if zoom is None:
        return {"ok": False, "reason": "only available on the com backend"}
    zoom()
    return {"ok": True}


@mcp.tool()
def drawing_save(path: str | None = None) -> dict[str, Any]:
    """Save the drawing. On the com backend this saves through AutoCAD.

    Note: saving a live AutoCAD drawing renames that document, so prefer
    drawing_export_dxf for a non-destructive copy.
    """
    be = _state.require()
    return {"ok": True, "path": be.save(path)}


@mcp.tool()
def drawing_export_dxf(path: str) -> dict[str, Any]:
    """Write the current drawing out as DXF (works from the live AutoCAD session too)."""
    be = _state.require()
    target = str(Path(path).with_suffix(".dxf"))
    return {"ok": True, "path": be.save(target)}


@mcp.tool()
def drawing_preview(path: str | None = None, theme: str = "dark") -> dict[str, Any]:
    """Render a PNG of the drawing and return only its path.

    Use drawing_screenshot instead when you want to actually look at the result —
    this one is for scripts that just need a file on disk.
    """
    be = _state.require()
    target = path or "preview.png"
    out = be.preview(target, theme=theme)
    return {"ok": True, "path": out}


@mcp.tool()
def drawing_screenshot(
    path: str | None = None,
    window: Any = None,
    width: int = 1600,
    height: int = 1200,
    theme: str = "dark",
) -> Any:
    """Look at the drawing. Returns an image you can see directly.

    This is the tool to call after drawing something, to check the result before
    going further or to verify a fix. Pass `window` to frame a region rather than
    the whole drawing — a full floor plan shrunk to 1600 px leaves the room
    labels unreadable, so zoom in when you need to read text.

    `window` is [min_x, min_y, max_x, max_y] in millimetres, or [p1, p2].

    What you get depends on the backend: with backend="dxf" this re-renders the
    drawing cleanly, white-on-dark or black-on-white. With backend="com" it
    photographs the live AutoCAD window, so you see the true current view and
    zoom, but also AutoCAD's ribbon and panels around it.
    """
    be = _state.require()
    target = path or "screenshot.png"
    out = be.screenshot(
        target,
        window=_window(window),
        width=int(width),
        height=int(height),
        theme=theme,
    )
    return Image(path=out)


@mcp.tool()
def acad_zoom_window(corner1: Any, corner2: Any) -> dict[str, Any]:
    """Zoom the live AutoCAD view to the rectangle spanned by two corners.

    Use it to home in on a detail before drawing_screenshot, so the capture is
    legible rather than a whole plan at thumbnail scale.
    """
    be = _state.require()
    zoom = getattr(be, "zoom_window", None)
    if zoom is None:
        return {"ok": False, "reason": "only available on the com backend"}
    zoom(_pt(corner1), _pt(corner2))
    return {"ok": True}


@mcp.tool()
def listing_entities(layer: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List entities, optionally filtered to one layer."""
    be = _state.require()
    items = be.entities(layer=layer, limit=limit)
    return {"count": len(items), "entities": items}


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------


@mcp.tool()
def layer_create(
    name: str, color: int | None = None, linetype: str | None = None
) -> dict[str, Any]:
    """Create a layer (no-op if it already exists) with an ACI colour and linetype."""
    be = _state.require()
    be.ensure_layer(name, color=color, linetype=linetype)
    return {"ok": True, "layer": name}


@mcp.tool()
def layer_create_standard() -> dict[str, Any]:
    """Create the full standard architectural layer set (A-WALL, A-DOOR, ...)."""
    be = _state.require()
    return {"ok": True, "layers": ops.setup_layers(be)}


@mcp.tool()
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


@mcp.tool()
def draw_line(p1: Any, p2: Any, layer: str = "0", linetype: str | None = None) -> dict[str, Any]:
    """Draw a straight line between two points."""
    be = _state.require()
    be.line(_pt(p1), _pt(p2), layer=layer, linetype=linetype)
    return {"ok": True}


@mcp.tool()
def draw_polyline(points: Any, closed: bool = False, layer: str = "0") -> dict[str, Any]:
    """Draw a polyline through a list of points."""
    be = _state.require()
    pts = _pts(points)
    be.polyline(pts, closed=closed, layer=layer)
    return {"ok": True, "vertices": len(pts), "closed": closed}


@mcp.tool()
def draw_rect(corner1: Any, corner2: Any, layer: str = "0") -> dict[str, Any]:
    """Draw an axis-aligned rectangle from two opposite corners."""
    be = _state.require()
    be.polyline(geom.rect_corners(_pt(corner1), _pt(corner2)), closed=True, layer=layer)
    return {"ok": True}


@mcp.tool()
def draw_circle(center: Any, radius: float, layer: str = "0") -> dict[str, Any]:
    """Draw a circle."""
    be = _state.require()
    be.circle(_pt(center), float(radius), layer=layer)
    return {"ok": True}


@mcp.tool()
def draw_arc(
    center: Any, radius: float, start_deg: float, end_deg: float, layer: str = "0"
) -> dict[str, Any]:
    """Draw an arc, counter-clockwise from start_deg to end_deg."""
    be = _state.require()
    be.arc(_pt(center), float(radius), float(start_deg), float(end_deg), layer=layer)
    return {"ok": True}


# ---------------------------------------------------------------------------
# walls, rooms, openings
# ---------------------------------------------------------------------------


@mcp.tool()
def draw_wall(
    points: Any,
    thickness: float = layers.WALL_THICKNESS,
    closed: bool = False,
    hatch: bool = False,
    layer: str = "A-WALL",
) -> dict[str, Any]:
    """Draw a wall as two offset faces along a centreline polyline.

    Set closed=True for a room envelope, hatch=True to poché the wall.
    """
    be = _state.require()
    pts = _pts(points)
    ops.draw_wall_run(be, pts, float(thickness), closed=closed, hatch=hatch, layer=layer)
    return {
        "ok": True,
        "segments": len(pts) - 1,
        "thickness": float(thickness),
        "length": round(geom.polyline_length(pts), 3),
    }


@mcp.tool()
def draw_wall_with_openings(
    points: Any,
    openings: Any,
    thickness: float = layers.WALL_THICKNESS,
    hatch: bool = False,
) -> dict[str, Any]:
    """Draw a wall run and cut door/window openings into it in one step.

    `openings` is a list of {"start": mm along the run, "width": mm,
    "kind": "door"|"window"|"opening", "swing": "left"|"right", "tag": "M1522"}.
    This is the tool to reach for when reproducing a real plan: it keeps the
    wall faces and the opening symbols consistent.
    """
    be = _state.require()
    return {
        "ok": True,
        **ops.draw_wall_with_openings(be, _pts(points), openings, float(thickness), hatch=hatch),
    }


@mcp.tool()
def draw_room(
    boundary: Any,
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    hatch: bool = False,
    show_area: bool = True,
) -> dict[str, Any]:
    """Wall a room in, then label it with its name and computed area in m².

    `boundary` is the wall centreline ring as a list of points.
    """
    be = _state.require()
    result = ops.draw_room(
        be, _pts(boundary), name, float(thickness), hatch=hatch, show_area=show_area
    )
    return {"ok": True, **result}


@mcp.tool()
def add_room_label(
    boundary: Any,
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    show_area: bool = True,
) -> dict[str, Any]:
    """Label an existing room ring with its name and area, without drawing walls."""
    be = _state.require()
    result = ops.add_room_label(be, _pts(boundary), name, float(thickness), show_area=show_area)
    return {"ok": True, **result}


@mcp.tool()
def draw_partition(points: Any, thickness: float = 120.0, hatch: bool = False) -> dict[str, Any]:
    """Draw a light internal partition wall (120 mm by default)."""
    be = _state.require()
    ops.draw_partition(be, _pts(points), float(thickness), hatch=hatch)
    return {"ok": True}


@mcp.tool()
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
    be = _state.require()
    ops.draw_door(
        be,
        _pt(hinge),
        float(width),
        float(wall_thickness),
        rotation=float(rotation),
        open_angle=float(open_angle),
        swing=swing,
        tag=tag,
    )
    return {"ok": True}


@mcp.tool()
def draw_window(
    start: Any,
    width: float = layers.WINDOW_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    rotation: float = 0.0,
    tag: str | None = None,
) -> dict[str, Any]:
    """Draw a window symbol spanning an opening, starting at the given jamb."""
    be = _state.require()
    ops.draw_window(
        be, _pt(start), float(width), float(wall_thickness), rotation=float(rotation), tag=tag
    )
    return {"ok": True}


@mcp.tool()
def draw_column(
    center: Any,
    size: float = layers.COLUMN_SIZE,
    depth: float | None = None,
    rotation: float = 0.0,
    hatch: bool = True,
) -> dict[str, Any]:
    """Draw a structural column as a rotated, optionally filled rectangle."""
    be = _state.require()
    ops.draw_column(
        be, _pt(center), float(size), rotation=float(rotation), depth=depth, hatch=hatch
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# core & equipment
# ---------------------------------------------------------------------------


@mcp.tool()
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
    be = _state.require()
    result = ops.draw_stair(
        be,
        _pt(corner1),
        _pt(corner2),
        steps=int(steps),
        axis=axis,
        up_label=up_label,
        down_label=down_label,
    )
    return {"ok": True, **result}


@mcp.tool()
def draw_lift(corner1: Any, corner2: Any, tag: str | None = None) -> dict[str, Any]:
    """Draw a lift/elevator shaft with its car outline and X cross."""
    be = _state.require()
    ops.draw_lift(be, _pt(corner1), _pt(corner2), tag=tag)
    return {"ok": True}


@mcp.tool()
def draw_shaft(corner1: Any, corner2: Any, name: str | None = None) -> dict[str, Any]:
    """Draw a service riser shaft (强电井/弱电井/水井) with a diagonal cross."""
    be = _state.require()
    ops.draw_shaft(be, _pt(corner1), _pt(corner2), name=name)
    return {"ok": True}


@mcp.tool()
def draw_equipment(corner1: Any, corner2: Any, name: str | None = None) -> dict[str, Any]:
    """Draw a rectangular piece of fixed equipment with a caption."""
    be = _state.require()
    ops.draw_equipment(be, _pt(corner1), _pt(corner2), name=name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------


@mcp.tool()
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
    be = _state.require()
    ex = [float(v) for v in extent]
    result = ops.draw_axis_grid(
        be,
        [float(v) for v in x_positions],
        [float(v) for v in y_positions],
        x_labels=x_labels,
        y_labels=y_labels,
        extent=(ex[0], ex[1], ex[2], ex[3]),
        bubble_r=float(bubble_r),
    )
    return {"ok": True, **result}


@mcp.tool()
def add_text(
    pos: Any,
    text: str,
    height: float = layers.H_TEXT,
    rotation: float = 0.0,
    align: str = "left",
    layer: str = "A-ANNO",
) -> dict[str, Any]:
    """Place single-line text. align is left, center, right or top-center."""
    be = _state.require()
    ops.annotate(
        be, _pt(pos), text, height=float(height), rotation=float(rotation), layer=layer, align=align
    )
    return {"ok": True}


@mcp.tool()
def add_leader_note(
    anchor: Any,
    text: str,
    direction: float = 45.0,
    length: float = 1500.0,
    height: float = layers.H_TEXT,
) -> dict[str, Any]:
    """Draw a one-segment leader from `anchor` with the note at its far end."""
    be = _state.require()
    ops.leader_note(
        be,
        _pt(anchor),
        text,
        direction=float(direction),
        length=float(length),
        height=float(height),
    )
    return {"ok": True}


@mcp.tool()
def add_dimension(p1: Any, p2: Any, offset: float = layers.DIM_OFFSET) -> dict[str, Any]:
    """Add an aligned dimension between two points, offset sideways by `offset` mm."""
    be = _state.require()
    ops.dimension_between(be, _pt(p1), _pt(p2), float(offset))
    return {"ok": True}


@mcp.tool()
def add_dimension_chain(points: Any, offset: float = layers.DIM_OFFSET) -> dict[str, Any]:
    """Add a chained dimension along a run of points, plus the overall dimension."""
    be = _state.require()
    return {"ok": True, **ops.dimension_chain(be, _pts(points), float(offset))}


@mcp.tool()
def add_hatch(
    points: Any,
    pattern: str = "ANSI31",
    scale: float = 40.0,
    angle: float = 0.0,
    solid: bool = False,
    layer: str = "A-HATCH",
) -> dict[str, Any]:
    """Hatch a closed boundary given as points (pattern, or solid fill)."""
    be = _state.require()
    be.hatch(_pts(points), pattern, float(scale), layer=layer, angle=float(angle), solid=solid)
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
        raise BackendError("the step tools draw through the plugin; call plugin_connect first")
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


@mcp.tool()
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


@mcp.tool()
def plugin_connect(new_document: bool = False) -> dict[str, Any]:
    """Draw through the step plugin in AutoCAD from now on.

    After this, drawing tools record into a step rather than placing loose entities,
    and the user can roll any step back from the palette. Open a step with
    step_begin, draw, then step_commit.
    """
    backend = _plugin_backend_connect(new_document=new_document)
    _state.reset(backend, "plugin")
    ops.setup_layers(backend)
    return {"ok": True, "backend": "plugin", **backend.info()}


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def step_list() -> dict[str, Any]:
    """List the steps in the drawing's journal, oldest first."""
    try:
        rows = StepSession(_client().connect()).steps()
    except PluginError as exc:
        raise BackendError(str(exc)) from exc
    return {"ok": True, "count": len(rows), "steps": rows, "rendered": describe_steps(rows)}


@mcp.tool()
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


@mcp.tool()
def library_list(source: str | None = None, limit: int = 500) -> dict[str, Any]:
    """List the block definitions in the open drawing, or in a library .dwg.

    This is what the element catalogue is written against, so it is also how a catalogue
    that has drifted from the library is caught. Dynamic blocks come back with their
    parameters, the values each accepts and which are read-only. A library file is opened
    read-only on the side and never becomes a reference to it.
    """
    try:
        return {"ok": True, **_client().call("blocks", source=source, limit=int(limit))}
    except (PluginError, PluginUnavailable) as exc:
        raise BackendError(str(exc)) from exc


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        {"type": element.key, "start": geom.xy(start), "end": geom.xy(end), "count": count,
         "spacing": spacing},
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


@mcp.tool()
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
            be, element, list(xs), list(ys), variant=variant, params=params,
            rotation=float(rotation), tag=tag,
        ),
    )


def _positions(start: Any, end: Any, count: int | None, spacing: float | None, inset: float):
    """The points a run would place at, for the step's label. Raises the same way the op does."""
    return catalog.positions_along(
        geom.xy(start), geom.xy(end), count=count, spacing=spacing, inset=float(inset)
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
