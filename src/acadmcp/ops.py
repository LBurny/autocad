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

from . import catalog, geom, layers, settings
from .backends.base import Backend
from .catalog import Element
from .geom import Point, Vec

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def setup_layers(be: Backend) -> list[str]:
    """Create the standard architectural layers. Safe to call repeatedly.

    A project's own layers are laid over the standard ones: its scheme is the one that
    applies to its drawings, and the standard set is only a starting point for a drawing
    that has no project behind it.
    """
    scheme = {**layers.LAYERS, **settings.current().layers}
    for name, (color, linetype) in scheme.items():
        be.ensure_layer(name, color=color, linetype=linetype)
    return sorted(scheme)


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
    """The portion of a polyline between two distances, keeping its corners.

    The input is deliberately *not* deduped: a ring handed over as a run repeats its
    first vertex at the end, and removing that repeat would shorten the run by its
    final segment — which is where the wall between the last opening and the seam
    lives. `geom.point_along` steps over the repeated vertex, and the offset drops it.
    """
    pts = [geom.xy(p) for p in points]
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
    closed: bool = False,
) -> dict[str, Any]:
    """Draw a wall run and punch door/window openings into it.

    Each opening is ``{"start": mm along the run, "width": mm,
    "kind": "door" | "window" | "opening", ...}`` with optional ``swing``
    ("left"/"right"), ``open_angle``, ``rotation_delta``, ``tag`` and
    ``tag_offset`` (moves the tag across the wall).

    ``closed=True`` reads `points` as a ring — the way a building envelope is drawn —
    and mitres the corners into one another. Openings are then measured along the
    ring from its first vertex.
    """
    ring = [geom.xy(p) for p in points]
    if closed and geom.is_closed(ring):
        ring = ring[:-1]
    if closed and len(ring) < 3:
        raise ValueError("a closed wall run needs at least three corners")

    for i, op in enumerate(openings):
        if "start" not in op or "width" not in op:
            raise ValueError(
                f"opening {i} needs 'start' and 'width' (mm along the run); got {sorted(op)}"
            )
        kind = op.get("kind", "door")
        if kind not in ("door", "window", "opening"):
            raise ValueError(
                f"opening {i}: kind must be 'door', 'window' or 'opening', got {kind!r}"
            )
        swing = op.get("swing", "left")
        if swing not in ("left", "right"):
            raise ValueError(f"opening {i}: swing must be 'left' or 'right', got {swing!r}")

    total = geom.polyline_length([*ring, ring[0]]) if closed else geom.polyline_length(ring)
    ops = sorted(openings, key=lambda o: float(o["start"]))

    if closed and not ops:
        # Nothing to punch, so the ring is one closed wall and every corner mitres.
        draw_wall_run(be, ring, thickness, closed=True, layer=layer, hatch=hatch)
        return {
            "segments": 1,
            "openings": 0,
            "clamped_openings": 0,
            "total_length": round(total, 3),
            "symbols": 0,
            "closed": True,
        }

    seam_kind: str | None = None
    if closed:
        ring, ops = _rebase_ring(ring, total, ops)
        seam_kind = ops[0].get("kind", "door")
    pts = ring

    wall_segments: list[tuple[float, float, bool, bool]] = []
    symbols: list[Any] = []
    cursor = 0.0
    clamped = 0
    previous: str | None = None

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
        kind = op.get("kind", "door")
        if start > cursor + 1e-6:
            wall_segments.append((cursor, start, _needs_cap(cursor, previous), kind == "opening"))

        angle = geom.angle_along(pts, start + width / 2.0)
        angle += float(op.get("rotation_delta", 0.0))
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
        previous = kind

    if cursor < total - 1e-6 or not wall_segments:
        # A closed ring's run ends where its first opening starts, so that end is the
        # opening's to close if the opening draws a symbol of its own.
        tail_cap = True if seam_kind is None else seam_kind == "opening"
        wall_segments.append((cursor, total, _needs_cap(cursor, previous), tail_cap))

    for seg_start, seg_end, cap_start, cap_end in wall_segments:
        seg = _sub_polyline(pts, seg_start, seg_end)
        if len(seg) < 2:
            continue
        draw_wall_run(be, seg, thickness, layer=layer, hatch=hatch, cap_ends=False)
        if cap_start or cap_end:
            left, right = geom.offset_pair(seg, thickness)
            if cap_start:
                be.line(left[0], right[0], layer=layer)
            if cap_end:
                be.line(left[-1], right[-1], layer=layer)

    return {
        "segments": len(wall_segments),
        "openings": len(ops) - clamped,
        "clamped_openings": clamped,
        "total_length": round(total, 3),
        "symbols": len(symbols),
        "closed": closed,
    }


def _needs_cap(cursor: float, previous: str | None) -> bool:
    """Whether a wall segment's start needs a line across the thickness.

    A door or window symbol draws its own jamb tick, in the same place and across the
    same thickness, so capping there as well draws one line twice — on A-WALL and on
    A-DOOR at once. A bare ``opening`` draws nothing, so closing its jambs is the
    wall's job.
    """
    return cursor == 0.0 or previous == "opening"


def _rebase_ring(
    ring: Sequence[Vec], total: float, ops: Sequence[dict]
) -> tuple[list[Vec], list[dict]]:
    """The ring re-started where its first opening starts, openings re-measured.

    A run has two ends and each end is capped square across the wall. Starting the run
    exactly at its first opening puts both of those caps inside an opening — where a
    cap belongs — and leaves every corner in the middle of a segment, where the offset
    mitres it. Drawing the ring as four runs instead is the only way to get openings
    into it, and that is what left a half-thickness notch at each corner.
    """
    first = float(ops[0]["start"])
    started = [geom.point_along([*ring, ring[0]], first)]
    travelled = 0.0
    for i, point in enumerate(ring):
        following = ring[(i + 1) % len(ring)]
        travelled += geom.dist(point, following)
        if travelled > first + 1e-9:
            started.append(following)
    started.append(started[0])

    moved = [{**op, "start": (float(op["start"]) - first) % total} for op in ops]
    moved.sort(key=lambda o: float(o["start"]))
    moved[0] = {**moved[0], "start": 0.0}
    return started, moved


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
    if swing not in ("left", "right"):
        raise ValueError(f"swing must be 'left' or 'right', got {swing!r}")
    if width <= 0:
        raise ValueError(f"a door needs a positive width, got {width}")
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
    if width <= 0:
        raise ValueError(f"a window needs a positive width, got {width}")
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


def _check_room_ring(boundary: Sequence[Point]) -> None:
    """Reject rings a label cannot be trusted on.

    A two-point "room" has area zero, and a self-intersecting (bowtie) ring has
    no meaningful inside — the area is winding-dependent and the centroid can
    land outside the walls. Both used to be drawn without a word.
    """
    ring = [geom.xy(p) for p in boundary]
    if geom.is_closed(ring):
        ring = ring[:-1]
    if len(geom.dedupe(ring)) < 3:
        raise ValueError("a room boundary needs at least three distinct corners")
    if geom.ring_self_intersects(ring):
        raise ValueError("the room boundary crosses itself — the label could land outside the room")


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
    bound_layer: str | None = None,
) -> dict[str, Any]:
    """Wall a room in and label it with its name and computed floor area.

    `boundary` is the wall *centreline* ring; the area reported excludes the
    walls themselves.
    """
    _check_room_ring(boundary)
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
    if bound_layer:
        # The face the area is measured to. A-ROOM-BOUND is in the standard for this and
        # nothing drew on it before, so it is opt-in rather than extra geometry in every
        # plan.
        be.polyline(inner, closed=True, layer=bound_layer)

    _label_room(be, inner, name, area_m2, name_layer, area_layer, show_area, label_offset)
    return {"name": name, "area_m2": round(area_m2, 3), "centroid": c}


def _label_room(
    be: Backend,
    inner: Sequence[Point],
    name: str | None,
    area_m2: float,
    name_layer: str,
    area_layer: str,
    show_area: bool,
    label_offset: float = 0.0,
) -> None:
    """The name and area, sized to sit inside the room they describe.

    A fixed text height spills a long name over the walls of a small room — a
    six-character 无障碍卫生间 across a 2 m toilet ran through three rooms. The
    same shrink-to-fit the equipment and shaft captions use applies here.
    """
    x_lo, _, x_hi, _ = geom.bbox(inner)
    room_w = x_hi - x_lo
    c = geom.centroid(inner)
    if label_offset:
        c = (c[0], c[1] + label_offset)
    name_h = layers.H_ROOM_NAME
    if name:
        name_h = layers.fit_text_height(name, room_w, layers.H_ROOM_NAME)
        be.text(c, name, name_h, layer=name_layer, align="center")
    if show_area:
        area_text = f"{area_m2:.2f}{layers.AREA_UNIT}"
        area_h = layers.fit_text_height(area_text, room_w, layers.H_ROOM_AREA)
        be.text(
            (c[0], c[1] - name_h * 1.4) if name else c,
            area_text,
            area_h,
            layer=area_layer,
            align="center",
        )


def add_room_label(
    be: Backend,
    boundary: Sequence[Point],
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    *,
    show_area: bool = True,
    name_layer: str = "A-ROOM-NAME",
    area_layer: str = "A-ROOM-AREA",
    bound_layer: str | None = None,
) -> dict[str, Any]:
    """Label an existing room ring without redrawing its walls."""
    _check_room_ring(boundary)
    inner = geom.inner_boundary(boundary, thickness)
    c = geom.centroid(inner)
    area_m2 = geom.area(inner) / 1_000_000.0
    if bound_layer:
        be.polyline(inner, closed=True, layer=bound_layer)
    _label_room(be, inner, name, area_m2, name_layer, area_layer, show_area)
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
    """A run of stairs: outline, treads, flight direction arrow and 上/下 labels.

    The arrow points the way the flight rises. `up_label` marks the end it rises
    toward and `down_label` the end it starts from, both set to one side of the arrow
    where they read without covering it.
    """
    if axis not in ("x", "y"):
        raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")
    ring = geom.rect_corners(corner1, corner2)
    made: list[Any] = [be.polyline(ring, closed=True, layer=border_layer or layer)]
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

    # 上 and 下 go to the two ends of the flight, on one side of the arrow. They used to
    # be placed at the middle of the flight, 480 mm apart, so both landed on top of the
    # arrow and of each other; the branch that was meant to swap them for the two axes
    # read `down_label if axis == "x" else down_label`, which does nothing.
    side = (other_b - other_a) * 0.28
    if axis == "x":
        up_at = (b[0], mid_other + side)
        down_at = (a[0], mid_other + side)
    else:
        up_at = (mid_other + side, b[1])
        down_at = (mid_other + side, a[1])
    if up_label:
        be.text(up_at, up_label, layers.H_TEXT, layer="A-ANNO", align="center")
    if down_label:
        be.text(down_at, down_label, layers.H_TEXT, layer="A-ANNO", align="center")
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

    The caption sits inside the shaft, which keeps it clear of neighbouring shafts and
    of whatever runs alongside — and it is shrunk to fit, because a Chinese character
    is 1.42 x its height wide, so a four-character name is wider than a 1500 shaft.
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
            be.text(
                geom.centroid(ring),
                name,
                layers.fit_text_height(name, geom.dist(ring[0], ring[1]), layers.H_TEXT),
                layer=tag_layer,
                align="center",
            )
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
        made.append(
            be.text(
                c,
                name,
                layers.fit_text_height(name, geom.dist(ring[0], ring[1]), layers.H_TEXT),
                layer=tag_layer,
                align="center",
            )
        )
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
    if len(x_labels) != len(x_positions) or len(y_labels) != len(y_positions):
        # zip would truncate silently, leaving the last axes unlabelled.
        raise ValueError(
            f"labels must match positions one-to-one: {len(x_positions)} x positions got "
            f"{len(x_labels)} labels, {len(y_positions)} y positions got {len(y_labels)}"
        )

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
    if geom.dist(p1, p2) < 1e-9:
        # The dimension line's direction is the point span itself; a coincident
        # pair divides by it and the backend answers ZeroDivisionError.
        raise ValueError("a dimension needs two distinct points")
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
    if align not in ("left", "center", "right", "top-center"):
        raise ValueError(f"align must be 'left', 'center', 'right' or 'top-center', got {align!r}")
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


# ---------------------------------------------------------------------------
# library elements
# ---------------------------------------------------------------------------


def place_element(
    be: Backend,
    element: Element,
    at: Point,
    *,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str | None = None,
    tag: str | None = None,
    tag_offset: float | None = None,
    tag_layer: str = "A-ANNO",
    tag_height: float = layers.H_TAG,
) -> list[Any]:
    """Place one catalogue element: a library block reference, plus its tag.

    The block is inserted, not redrawn, so the drawing gets the definition the standard
    library actually holds — one that a colleague's AutoCAD can explode, edit and measure
    exactly as if they had inserted it by hand. `variant` picks a size from the catalogue
    and `params` overrides individual dynamic properties on top of it.
    """
    made: list[Any] = [
        be.insert(
            element.block,
            at,
            source=element.source or None,
            rotation=rotation,
            scale=scale,
            properties=element.settings(variant, params),
            layer=layer or element.layer,
        )
    ]
    if tag:
        made.append(_element_tag(be, tag, at, tag_offset, tag_layer, tag_height))
    return made


def place_along(
    be: Backend,
    element: Element,
    start: Point,
    end: Point,
    *,
    count: int | None = None,
    spacing: float | None = None,
    inset: float = 0.0,
    rotation: float | None = None,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    scale: float = 1.0,
    layer: str | None = None,
    tag: bool = True,
    tag_offset: float | None = None,
) -> dict[str, Any]:
    """Repeat an element along a run — a row of columns, a wall of windows.

    `rotation` defaults to the direction of the run, which is what makes a row of doors all
    lie in the wall they are in rather than all facing the same way.
    """
    points = catalog.positions_along(
        geom.xy(start), geom.xy(end), count=count, spacing=spacing, inset=inset
    )
    direction = _run_direction(start, end) if rotation is None else float(rotation)
    tags = _numbering(be, element, len(points)) if tag else []
    made: list[Any] = []
    for index, point in enumerate(points):
        made += place_element(
            be,
            element,
            point,
            variant=variant,
            params=params,
            rotation=direction,
            scale=scale,
            layer=layer,
            tag=tags[index] if index < len(tags) else None,
            tag_offset=tag_offset,
        )
    return {"placed": len(points), "rotation": direction, "tags": tags, "points": points}


def place_grid(
    be: Backend,
    element: Element,
    xs: Sequence[float],
    ys: Sequence[float],
    *,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str | None = None,
    tag: bool = True,
    tag_offset: float | None = None,
) -> dict[str, Any]:
    """Place an element at every intersection of two coordinate lists.

    That is how a frame of columns is laid out: the axis positions are what the drawing is
    dimensioned to, so they are what the placement takes.
    """
    points = catalog.positions_grid(list(xs), list(ys))
    tags = _numbering(be, element, len(points)) if tag else []
    made: list[Any] = []
    for index, point in enumerate(points):
        made += place_element(
            be,
            element,
            point,
            variant=variant,
            params=params,
            rotation=rotation,
            scale=scale,
            layer=layer,
            tag=tags[index] if index < len(tags) else None,
            tag_offset=tag_offset,
        )
    return {"placed": len(points), "tags": tags, "points": points}


def _element_tag(
    be: Backend,
    tag: str,
    at: Point,
    offset: float | None,
    layer: str,
    height: float,
) -> Any:
    """The tag beside a placed element. Offset along +Y, which is up the sheet."""
    x, y = geom.xy(at)
    dy = height * 2.0 if offset is None else float(offset)
    return be.text((x, y + dy), tag, height, layer=layer, align="center")


def _numbering(be: Backend, element: Element, how_many: int) -> list[str]:
    """The next `how_many` tag numbers, taken from the numbers the drawing already has."""
    if not element.tag_prefix or how_many < 1:
        return []
    existing = existing_tags(be)
    first = catalog.next_tag(element.tag_prefix, existing)
    if not first:
        return []
    prefix, _, number = first.rpartition("-")
    start = int(number)
    return [f"{prefix}-{start + i}" for i in range(how_many)]


def existing_tags(be: Backend, layer: str | None = None, limit: int = 2000) -> list[str]:
    """Every text in the drawing, for numbering to be derived from rather than stored."""
    return [str(row["text"]) for row in be.entities(layer=layer, limit=limit) if row.get("text")]


def _run_direction(start: Point, end: Point) -> float:
    """The run's bearing in degrees, counter-clockwise from +X."""
    x0, y0 = geom.xy(start)
    x1, y1 = geom.xy(end)
    return math.degrees(math.atan2(y1 - y0, x1 - x0))
