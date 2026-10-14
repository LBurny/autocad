"""Architectural operations: walls, doors, windows, rooms, cores and annotation.

These are the primitives an AI actually needs to produce a floor plan. They are
backend-agnostic — every op takes a :class:`~acadmcp.backends.base.Backend` and
does its geometry through :mod:`acadmcp.geom`, so a plan drawn into live AutoCAD
and one written to DXF come out identical.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Sequence
from typing import Any

from . import geom, layers
from .backends.base import Backend
from .geom import Point, Vec

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def setup_layers(be: Backend) -> list[str]:
    """Create the standard architectural layers. Safe to call repeatedly."""
    for name, (color, linetype) in layers.LAYERS.items():
        be.ensure_layer(name, color=color, linetype=linetype)
    return sorted(layers.LAYERS)


# ---------------------------------------------------------------------------
# walls
# ---------------------------------------------------------------------------


def draw_wall_run(
    be: Backend,
    points: Sequence[Point],
    thickness: float = layers.WALL_THICKNESS,
    *,
    closed: bool = False,
    layer: str = "A-WALL",
    hatch: bool = False,
    hatch_layer: str = "A-WALL-PATT",
    hatch_pattern: str = "ANSI31",
    hatch_scale: float = 40.0,
    cap_ends: bool = True,
) -> list[Any]:
    """Draw a wall as two offset faces along a centreline (plus optional poché)."""
    left, right = geom.offset_pair(points, thickness, closed=closed)
    made = [
        be.polyline(left, closed=closed, layer=layer),
        be.polyline(right, closed=closed, layer=layer),
    ]

    if cap_ends and not closed:
        made.append(be.line(left[0], right[0], layer=layer))
        made.append(be.line(left[-1], right[-1], layer=layer))

    if hatch:
        ring = _wall_ring(left, right, closed)
        # Poché is decorative: a backend that cannot hatch it must not fail the wall.
        with contextlib.suppress(Exception):
            if len(ring) >= 3:
                made.append(be.hatch(ring, hatch_pattern, hatch_scale, layer=hatch_layer))
    return made


def _wall_ring(left: Sequence[Vec], right: Sequence[Vec], closed: bool) -> list[Vec]:
    ring = [*left, *reversed(right)]
    return geom.dedupe(ring) if closed else geom.dedupe([*left, *reversed(right)])


def _sub_polyline(points: Sequence[Point], d0: float, d1: float) -> list[Vec]:
    """The portion of a polyline between two distances, keeping its corners."""
    pts = geom.dedupe(points)
    out = [geom.point_along(pts, d0)]
    travelled = 0.0
    for i in range(len(pts) - 1):
        travelled += geom.dist(pts[i], pts[i + 1])
        if d0 < travelled < d1:
            out.append(pts[i + 1])
    out.append(geom.point_along(pts, d1))
    return geom.dedupe(out)


def draw_wall_with_openings(
    be: Backend,
    points: Sequence[Point],
    openings: Sequence[dict],
    thickness: float = layers.WALL_THICKNESS,
    *,
    layer: str = "A-WALL",
    door_layer: str = "A-DOOR",
    window_layer: str = "A-WINDOW",
    hatch: bool = False,
) -> dict[str, Any]:
    """Draw a wall run and punch door/window openings into it.

    Each opening is ``{"start": mm along the run, "width": mm,
    "kind": "door" | "window" | "opening", ...}`` with optional ``swing``
    ("left"/"right"), ``open_angle``, ``rotation_delta``, ``tag`` and
    ``tag_offset`` (moves the tag across the wall).
    """
    pts = geom.dedupe(points)
    total = geom.polyline_length(pts)
    ops = sorted(openings, key=lambda o: float(o["start"]))

    wall_segments: list[list[Vec]] = []
    symbols: list[Any] = []
    cursor = 0.0
    clamped = 0

    for op in ops:
        start = float(op["start"])
        width = float(op["width"])
        if start >= total:
            clamped += 1
            continue
        if start + width > total:
            # An opening running off the end of the wall is the most common
            # coordinate slip; trim it rather than drawing a symbol into space.
            width = total - start
            clamped += 1
        end = start + width
        if start > cursor + 1e-6:
            wall_segments.append(_sub_polyline(pts, cursor, start))

        angle = geom.angle_along(pts, start + width / 2.0)
        angle += float(op.get("rotation_delta", 0.0))
        kind = op.get("kind", "door")
        tag = op.get("tag")
        tag_offset = op.get("tag_offset")

        if kind == "door":
            symbols += draw_door(
                be,
                geom.point_along(pts, start),
                width,
                thickness,
                rotation=angle,
                open_angle=float(op.get("open_angle", 90.0)),
                swing=op.get("swing", "left"),
                layer=door_layer,
                tag=tag,
                tag_offset=tag_offset,
            )
        elif kind == "window":
            symbols += draw_window(
                be,
                geom.point_along(pts, start),
                width,
                thickness,
                rotation=angle,
                layer=window_layer,
                tag=tag,
                tag_offset=tag_offset,
            )
        # kind == "opening": leave the gap bare
        cursor = max(cursor, end)

    if cursor < total - 1e-6:
        wall_segments.append(_sub_polyline(pts, cursor, total))

    for seg in wall_segments:
        if len(seg) >= 2:
            draw_wall_run(be, seg, thickness, layer=layer, hatch=hatch, cap_ends=True)

    return {
        "segments": len(wall_segments),
        "openings": len(ops) - clamped,
        "clamped_openings": clamped,
        "total_length": round(total, 3),
        "symbols": len(symbols),
    }


# ---------------------------------------------------------------------------
# doors & windows
# ---------------------------------------------------------------------------


def draw_door(
    be: Backend,
    hinge: Point,
    width: float = layers.DOOR_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    *,
    rotation: float = 0.0,
    open_angle: float = 90.0,
    swing: str = "left",
    layer: str = "A-DOOR",
    tag: str | None = None,
    tag_offset: float | None = None,
    tag_layer: str = "A-ANNO",
) -> list[Any]:
    """A single-leaf door: jamb ticks, leaf and swing arc.

    `hinge` is the hinge-side jamb on the wall centreline; `rotation` is the wall
    direction in degrees, and the leaf opens by `open_angle` to the left or right.
    `tag_offset` moves the door tag across the wall (negative flips it to the far
    side), which is how you keep tags legible in a tight room cluster.
    """
    h = geom.xy(hinge)
    made: list[Any] = []

    leaf_dir = rotation + open_angle if swing == "left" else rotation - open_angle
    leaf_end = geom.polar(h, leaf_dir, width)
    made.append(be.line(h, leaf_end, layer=layer))

    if swing == "left":
        a0, a1 = rotation, rotation + open_angle
    else:
        a0, a1 = rotation - open_angle, rotation
    a0, a1 = min(a0, a1), max(a0, a1)
    made.append(be.arc(h, width, a0, a1, layer=layer))

    # Jamb ticks across the wall thickness at both ends of the opening.
    n = geom.left_normal(geom.polar((0, 0), rotation, 1.0))
    far = geom.polar(h, rotation, width)
    half = wall_thickness / 2.0
    for p in (h, far):
        made.append(
            be.line(geom.add(p, geom.mul(n, half)), geom.add(p, geom.mul(n, -half)), layer=layer)
        )

    if tag:
        mid = geom.polar(h, rotation, width / 2.0)
        out = half + layers.H_TAG * 1.2 if tag_offset is None else tag_offset
        pos = geom.add(mid, geom.mul(n, out))
        made.append(be.text(pos, tag, layers.H_TAG, layer=tag_layer, align="center"))
    return made


def draw_window(
    be: Backend,
    start: Point,
    width: float = layers.WINDOW_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    *,
    rotation: float = 0.0,
    layer: str = "A-WINDOW",
    tag: str | None = None,
    tag_offset: float | None = None,
    tag_layer: str = "A-ANNO",
) -> list[Any]:
    """A window symbol: four parallel lines spanning the opening, plus jambs."""
    s = geom.xy(start)
    n = geom.left_normal(geom.polar((0, 0), rotation, 1.0))
    end = geom.polar(s, rotation, width)
    made: list[Any] = []

    for d in (-0.5, -1.0 / 6.0, 1.0 / 6.0, 0.5):
        off = geom.mul(n, wall_thickness * d)
        made.append(be.line(geom.add(s, off), geom.add(end, off), layer=layer))

    half = wall_thickness / 2.0
    for p in (s, end):
        made.append(
            be.line(geom.add(p, geom.mul(n, half)), geom.add(p, geom.mul(n, -half)), layer=layer)
        )

    if tag:
        mid = geom.polar(s, rotation, width / 2.0)
        out = half + layers.H_TAG * 1.2 if tag_offset is None else tag_offset
        pos = geom.add(mid, geom.mul(n, out))
        made.append(be.text(pos, tag, layers.H_TAG, layer=tag_layer, align="center"))
    return made


def draw_column(
    be: Backend,
    center: Point,
    size: float = layers.COLUMN_SIZE,
    *,
    rotation: float = 0.0,
    depth: float | None = None,
    layer: str = "A-COLUMN",
    hatch: bool = True,
) -> list[Any]:
    """A structural column, drawn as a rotated rectangle (optionally filled)."""
    d = size if depth is None else depth
    half_w, half_d = size / 2.0, d / 2.0
    c = geom.xy(center)
    local = [(-half_w, -half_d), (half_w, -half_d), (half_w, half_d), (-half_w, half_d)]
    corners = [geom.rotate(geom.add(c, p), c, rotation) for p in local]
    made: list[Any] = [be.polyline(corners, closed=True, layer=layer)]
    if hatch:
        with contextlib.suppress(Exception):
            made.append(be.hatch(corners, "SOLID", 1.0, layer=layer, solid=True))
    return made


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------


def draw_room(
    be: Backend,
    boundary: Sequence[Point],
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    *,
    wall_layer: str = "A-WALL",
    name_layer: str = "A-ROOM-NAME",
    area_layer: str = "A-ROOM-AREA",
    show_area: bool = True,
    hatch: bool = False,
    hatch_layer: str = "A-HATCH",
    hatch_pattern: str = "ANSI31",
    hatch_scale: float = 40.0,
    label_offset: float = 0.0,
) -> dict[str, Any]:
    """Wall a room in and label it with its name and computed floor area.

    `boundary` is the wall *centreline* ring; the area reported excludes the
    walls themselves.
    """
    draw_wall_run(
        be,
        boundary,
        thickness,
        closed=True,
        layer=wall_layer,
        hatch=hatch,
        hatch_layer=hatch_layer,
        hatch_pattern=hatch_pattern,
        hatch_scale=hatch_scale,
    )

    inner = geom.inner_boundary(boundary, thickness)
    area_m2 = geom.area(inner) / 1_000_000.0
    c = geom.centroid(inner)
    if label_offset:
        c = (c[0], c[1] + label_offset)

    if name:
        be.text(c, name, layers.H_ROOM_NAME, layer=name_layer, align="center")
    if name and show_area:
        be.text(
            (c[0], c[1] - layers.H_ROOM_NAME * 1.4),
            f"{area_m2:.2f}{layers.AREA_UNIT}",
            layers.H_ROOM_AREA,
            layer=area_layer,
            align="center",
        )
    elif show_area and not name:
        be.text(
            c,
            f"{area_m2:.2f}{layers.AREA_UNIT}",
            layers.H_ROOM_AREA,
            layer=area_layer,
            align="center",
        )

    return {"name": name, "area_m2": round(area_m2, 3), "centroid": c}


def add_room_label(
    be: Backend,
    boundary: Sequence[Point],
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    *,
    show_area: bool = True,
    name_layer: str = "A-ROOM-NAME",
    area_layer: str = "A-ROOM-AREA",
) -> dict[str, Any]:
    """Label an existing room ring without redrawing its walls."""
    inner = geom.inner_boundary(boundary, thickness)
    c = geom.centroid(inner)
    area_m2 = geom.area(inner) / 1_000_000.0
    if name:
        be.text(c, name, layers.H_ROOM_NAME, layer=name_layer, align="center")
    if show_area:
        be.text(
            (c[0], c[1] - layers.H_ROOM_NAME * 1.4),
            f"{area_m2:.2f}{layers.AREA_UNIT}",
            layers.H_ROOM_AREA,
            layer=area_layer,
            align="center",
        )
    return {"name": name, "area_m2": round(area_m2, 3), "centroid": c}


def draw_partition(
    be: Backend,
    points: Sequence[Point],
    thickness: float = 120.0,
    *,
    layer: str = "A-WALL",
    hatch: bool = False,
) -> list[Any]:
    """A lighter internal partition wall (default 120 mm)."""
    return draw_wall_run(be, points, thickness, layer=layer, hatch=hatch)


# ---------------------------------------------------------------------------
# core elements: stairs, lifts, shafts
# ---------------------------------------------------------------------------


def draw_stair(
    be: Backend,
    corner1: Point,
    corner2: Point,
    *,
    steps: int = 9,
    axis: str = "x",
    layer: str = "A-STAIR",
    up_label: str | None = "\u4e0a",
    down_label: str | None = "\u4e0b",
    border_layer: str | None = None,
) -> dict[str, Any]:
    """A run of stairs: outline, treads, flight direction arrow and 上/下 labels."""
    ring = geom.rect_corners(corner1, corner2)
    made: list[Any] = [be.polyline(ring, closed=True, layer=border_layer or "A-ANNO")]
    x1, y1, x2, y2 = geom.bbox(ring)

    if axis == "x":
        span, other_a, other_b = x2 - x1, y1, y2
    else:
        span, other_a, other_b = y2 - y1, x1, x2

    for i in range(1, steps):
        t = i / steps
        if axis == "x":
            px = x1 + span * t
            made.append(be.line((px, other_a), (px, other_b), layer=layer))
        else:
            py = y1 + span * t
            made.append(be.line((other_a, py), (other_b, py), layer=layer))

    # Direction arrow down the middle of the flight.
    mid_other = (other_a + other_b) / 2.0
    if axis == "x":
        a = (x1 + span * 0.12, mid_other)
        b = (x1 + span * 0.88, mid_other)
    else:
        a = (mid_other, y1 + span * 0.12)
        b = (mid_other, y1 + span * 0.88)
    made.append(be.line(a, b, layer=layer))
    arrow = min(span * 0.12, 400.0)
    for sgn_angle in (30.0, 150.0):
        tip = geom.polar(b, geom.angle_of(a, b) + 180 + sgn_angle, arrow)
        made.append(be.line(b, tip, layer=layer))

    label_pos = ((x1 + x2) / 2.0, (y1 + y2) / 2.0 + (y2 - y1) * 0.28)
    if up_label:
        be.text(label_pos, up_label, layers.H_TEXT, layer="A-ANNO", align="center")
    if down_label:
        be.text(
            (label_pos[0], label_pos[1] + layers.H_TEXT * 1.6),
            down_label if axis == "x" else down_label,
            layers.H_TEXT,
            layer="A-ANNO",
            align="center",
        )
    return {"steps": steps, "axis": axis, "corners": ring}


def draw_lift(
    be: Backend,
    corner1: Point,
    corner2: Point,
    *,
    tag: str | None = None,
    layer: str = "A-LIFT",
    tag_layer: str = "A-ANNO",
) -> list[Any]:
    """A lift/elevator shaft: shaft outline, car outline and the standard X cross."""
    shaft = geom.rect_corners(corner1, corner2)
    x1, y1, x2, y2 = geom.bbox(shaft)
    pad = 120.0
    car = geom.rect_corners((x1 + pad, y1 + pad), (x2 - pad, y2 - pad))
    made: list[Any] = [
        be.polyline(shaft, closed=True, layer=layer),
        be.polyline(car, closed=True, layer=layer),
        be.line(car[0], car[2], layer=layer),
        be.line(car[1], car[3], layer=layer),
    ]
    if tag:
        made.append(
            be.text(
                ((x1 + x2) / 2.0, y1 - layers.H_TEXT * 1.2),
                tag,
                layers.H_TEXT,
                layer=tag_layer,
                align="center",
            )
        )
    return made


def draw_shaft(
    be: Backend,
    corner1: Point,
    corner2: Point,
    name: str | None = None,
    *,
    layer: str = "A-SHAFT",
    tag_layer: str = "A-ANNO",
    hatch: bool = True,
    hatch_scale: float = 60.0,
) -> list[Any]:
    """A service riser shaft (强电井/弱电井/水井): outline plus diagonal cross.

    The caption sits inside the shaft, which keeps it clear of neighbouring
    shafts and of whatever runs alongside.
    """
    ring = geom.rect_corners(corner1, corner2)
    made: list[Any] = [
        be.polyline(ring, closed=True, layer=layer),
        be.line(ring[0], ring[2], layer=layer),
        be.line(ring[1], ring[3], layer=layer),
    ]
    if hatch:
        with contextlib.suppress(Exception):
            made.append(be.hatch(ring, "ANSI37", hatch_scale, layer=layer))
    if name:
        made.append(
            be.text(geom.centroid(ring), name, layers.H_TEXT, layer=tag_layer, align="center")
        )
    return made


def draw_equipment(
    be: Backend,
    corner1: Point,
    corner2: Point,
    name: str | None = None,
    *,
    layer: str = "A-EQUIP",
    tag_layer: str = "A-ANNO",
) -> list[Any]:
    """A generic piece of fixed equipment (bench, tank, cabinet) with a caption."""
    ring = geom.rect_corners(corner1, corner2)
    made: list[Any] = [be.polyline(ring, closed=True, layer=layer)]
    if name:
        c = geom.centroid(ring)
        made.append(be.text(c, name, layers.H_TEXT, layer=tag_layer, align="center"))
    return made


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------


def draw_axis_grid(
    be: Backend,
    x_positions: Sequence[float],
    y_positions: Sequence[float],
    *,
    x_labels: Sequence[str] | None = None,
    y_labels: Sequence[str] | None = None,
    extent: tuple[float, float, float, float],
    bubble_r: float = layers.AXIS_BUBBLE_R,
    layer: str = "A-AXIS",
    text_layer: str = "A-AXIS-TEXT",
) -> dict[str, Any]:
    """Draw the structural grid with circled axis bubbles, as on the reference sheet.

    `extent` is (min_x, min_y, max_x, max_y) that the grid lines should span.
    """
    min_x, min_y, max_x, max_y = extent
    x_labels = list(x_labels) if x_labels else [str(i + 1) for i in range(len(x_positions))]
    y_labels = list(y_labels) if y_labels else [_alpha(i) for i in range(len(y_positions))]

    for pos, label in zip(x_positions, x_labels, strict=False):
        be.line((pos, min_y), (pos, max_y), layer=layer, linetype="CENTER")
        tip = max_y + bubble_r * 2.0
        be.line((pos, max_y), (pos, tip - bubble_r), layer=layer)
        be.circle((pos, tip), bubble_r, layer=layer)
        be.text((pos, tip), label, bubble_r * 1.1, layer=text_layer, align="center")

    for pos, label in zip(y_positions, y_labels, strict=False):
        be.line((min_x, pos), (max_x, pos), layer=layer, linetype="CENTER")
        tip = max_x + bubble_r * 2.0
        be.line((max_x, pos), (tip - bubble_r, pos), layer=layer)
        be.circle((tip, pos), bubble_r, layer=layer)
        be.text((tip, pos), label, bubble_r * 1.1, layer=text_layer, align="center")

    return {"x_axes": len(x_positions), "y_axes": len(y_positions)}


def _alpha(i: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (spreadsheet-style axis letters)."""
    letters = ""
    i += 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def dimension_between(
    be: Backend,
    p1: Point,
    p2: Point,
    offset: float = layers.DIM_OFFSET,
    *,
    layer: str = "A-DIM",
) -> Any:
    return be.linear_dimension(p1, p2, offset, layer=layer, text_height=layers.H_DIM)


def dimension_chain(
    be: Backend,
    points: Sequence[Point],
    offset: float = layers.DIM_OFFSET,
    *,
    layer: str = "A-DIM",
) -> dict[str, Any]:
    """A chain of dimensions along a polyline run, plus the overall dimension."""
    made = []
    total = 0.0
    for i in range(len(points) - 1):
        made.append(dimension_between(be, points[i], points[i + 1], offset, layer=layer))
        total += geom.dist(points[i], points[i + 1])
    made.append(dimension_between(be, points[0], points[-1], offset * 2.0, layer=layer))
    return {"segments": len(points) - 1, "total": round(total, 3), "dimensions": len(made)}


def annotate(
    be: Backend,
    pos: Point,
    text: str,
    *,
    height: float = layers.H_TEXT,
    rotation: float = 0.0,
    layer: str = "A-ANNO",
    align: str = "left",
) -> Any:
    return be.text(pos, text, height, layer=layer, rotation=rotation, align=align)


def leader_note(
    be: Backend,
    anchor: Point,
    text: str,
    *,
    direction: float = 45.0,
    length: float = 1500.0,
    height: float = layers.H_TEXT,
    layer: str = "A-ANNO",
) -> list[Any]:
    """A one-segment leader with the note text at its far end."""
    tail = geom.polar(anchor, direction, length)
    made = [be.line(anchor, tail, layer=layer)]
    made.append(
        be.text(
            geom.polar(tail, direction, height * 0.6),
            text,
            height,
            layer=layer,
            align="left" if math.cos(math.radians(direction)) >= 0 else "right",
        )
    )
    return made
