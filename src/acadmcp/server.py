"""The MCP server: the tool surface an AI client talks to.

Every tool is a thin wrapper over :mod:`acadmcp.ops` and a backend, so the same
tool works whether the target is a live AutoCAD session (``backend="com"``) or a
headless DXF file (``backend="dxf"``).

Run it with ``acadmcp`` or ``python -m acadmcp.server``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # mcp 2.x renamed FastMCP to MCPServer; the decorator API is unchanged.
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image

from . import geom, layers, ops
from .backends import make_backend
from .backends.base import Backend, BackendError

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
        },
        "workflow": [
            "drawing_new(backend='dxf'|'com', path=...) or drawing_open(path)",
            "draw_axis_grid(...) to lay out the structural grid",
            "draw_wall / draw_wall_with_openings / draw_room for the envelope and partitions",
            "draw_door, draw_window, draw_column for openings and structure",
            "draw_stair, draw_lift, draw_shaft for the core and risers",
            "draw_room / add_room_label for names and computed areas",
            "add_dimension / add_dimension_chain, annotate for annotation",
            "drawing_preview(...) to eyeball it, drawing_save(...) to keep it",
        ],
        "conventions": {
            "layers": sorted(layers.LAYERS),
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
    else:
        raise BackendError(f"unknown backend {backend!r}; expected 'dxf' or 'com'")
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
