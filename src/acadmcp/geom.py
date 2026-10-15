"""Pure 2D geometry.

Nothing here imports a CAD library, so the architecture maths is unit-testable on
its own and the COM and DXF drivers share exactly the same numbers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = Sequence[float]
Vec = tuple[float, float]

EPS = 1e-9


def xy(p: Point) -> Vec:
    """Normalise any point-like input to a plain (x, y) float tuple."""
    return (float(p[0]), float(p[1]))


def sub(a: Point, b: Point) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def add(a: Point, b: Point) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def mul(a: Point, k: float) -> Vec:
    return (a[0] * k, a[1] * k)


def dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def length(a: Point) -> float:
    return math.hypot(a[0], a[1])


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(a: Point) -> Vec:
    n = length(a)
    if n < EPS:
        raise ValueError("cannot normalise a zero-length vector")
    return (a[0] / n, a[1] / n)


def left_normal(a: Point) -> Vec:
    """Unit vector 90 degrees counter-clockwise from `a`."""
    u = unit(a)
    return (-u[1], u[0])


def polar(origin: Point, angle_deg: float, distance: float) -> Vec:
    t = math.radians(angle_deg)
    return (origin[0] + distance * math.cos(t), origin[1] + distance * math.sin(t))


def rotate(p: Point, origin: Point, angle_deg: float) -> Vec:
    t = math.radians(angle_deg)
    c, s = math.cos(t), math.sin(t)
    dx, dy = p[0] - origin[0], p[1] - origin[1]
    return (origin[0] + dx * c - dy * s, origin[1] + dx * s + dy * c)


def angle_of(a: Point, b: Point) -> float:
    """Direction of a->b in degrees, CCW from +X."""
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def polyline_length(points: Sequence[Point]) -> float:
    return sum(dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def signed_area(points: Sequence[Point]) -> float:
    """Shoelace area; positive when the ring is counter-clockwise."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        total += a[0] * b[1] - b[0] * a[1]
    return total / 2.0


def area(points: Sequence[Point]) -> float:
    return abs(signed_area(points))


def centroid(points: Sequence[Point]) -> Vec:
    """Area-weighted centroid, falling back to the vertex mean for degenerate rings."""
    a = signed_area(points)
    if abs(a) < EPS:
        n = len(points)
        return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)
    cx = cy = 0.0
    n = len(points)
    for i in range(n):
        p, q = points[i], points[(i + 1) % n]
        f = p[0] * q[1] - q[0] * p[1]
        cx += (p[0] + q[0]) * f
        cy += (p[1] + q[1]) * f
    return (cx / (6 * a), cy / (6 * a))


def bbox(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def is_closed(points: Sequence[Point], tol: float = 1e-6) -> bool:
    return len(points) > 2 and dist(points[0], points[-1]) <= tol


def dedupe(points: Sequence[Point], tol: float = 1e-7) -> list[Vec]:
    """Drop repeated consecutive vertices, which break offset joins."""
    out: list[Vec] = []
    for p in points:
        q = xy(p)
        if not out or dist(out[-1], q) > tol:
            out.append(q)
    return out


def rect_corners(corner1: Point, corner2: Point) -> list[Vec]:
    """Four corners of the axis-aligned rectangle spanned by two opposite corners."""
    x1, y1 = xy(corner1)
    x2, y2 = xy(corner2)
    xa, xb = sorted((x1, x2))
    ya, yb = sorted((y1, y2))
    return [(xa, ya), (xb, ya), (xb, yb), (xa, yb)]


def line_intersection(a1: Point, a2: Point, b1: Point, b2: Point) -> Vec | None:
    """Intersection of two infinite lines, or None when they are parallel."""
    d1 = sub(a2, a1)
    d2 = sub(b2, b1)
    den = cross(d1, d2)
    if abs(den) < EPS:
        return None
    t = cross(sub(b1, a1), d2) / den
    return (a1[0] + d1[0] * t, a1[1] + d1[1] * t)


def ring_self_intersects(points: Sequence[Point]) -> bool:
    """True when the closed ring crosses or touches itself.

    A ring that fails this has no meaningful inside: the area depends on the
    winding and the centroid can land outside the walls, so callers that label
    rooms must reject it rather than draw a plausible-looking lie. Adjacent
    segments share a vertex by construction and are not tested against each
    other.
    """
    ring = dedupe(points)
    if is_closed(ring):
        ring = ring[:-1]
    n = len(ring)
    if n < 4:  # a triangle cannot cross itself
        return False
    for i in range(n):
        for j in range(i + 1, n):
            if (i + 1) % n == j or (j + 1) % n == i:
                continue
            if _segments_meet(ring[i], ring[(i + 1) % n], ring[j], ring[(j + 1) % n]):
                return True
    return False


def _segments_meet(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """Segment intersection including endpoint touches (a self-touching ring counts)."""
    d1 = cross(sub(a2, a1), sub(b1, a1))
    d2 = cross(sub(a2, a1), sub(b2, a1))
    d3 = cross(sub(b2, b1), sub(a1, b1))
    d4 = cross(sub(b2, b1), sub(a2, b1))
    if d1 == d2 == d3 == d4 == 0:
        # Collinear: the straddle test degenerates to always-true, so test
        # overlap on the axes instead.
        return max(min(a1[0], a2[0]), min(b1[0], b2[0])) <= min(
            max(a1[0], a2[0]), max(b1[0], b2[0])
        ) and max(min(a1[1], a2[1]), min(b1[1], b2[1])) <= min(max(a1[1], a2[1]), max(b1[1], b2[1]))
    return d1 * d2 <= 0 and d3 * d4 <= 0


def _offset_segment(a: Point, b: Point, d: float) -> tuple[Vec, Vec]:
    n = left_normal(sub(b, a))
    return (add(a, mul(n, d)), add(b, mul(n, d)))


def offset_polyline(points: Sequence[Point], d: float, closed: bool = False) -> list[Vec]:
    """Offset a polyline by `d` to its left, mitre-joining the corners.

    This is the workhorse behind wall faces, room inner boundaries and
    opening placement, so it handles the wrap-around join for closed rings too.
    """
    pts = dedupe(points)
    if closed and not is_closed(pts):
        pts = [*pts, pts[0]]
    if len(pts) < 2:
        raise ValueError("offset_polyline needs at least two distinct points")

    segs = [_offset_segment(pts[i], pts[i + 1], d) for i in range(len(pts) - 1)]
    out: list[Vec] = [segs[0][0]]
    for i in range(len(segs) - 1):
        p = line_intersection(segs[i][0], segs[i][1], segs[i + 1][0], segs[i + 1][1])
        out.append(p if p is not None else segs[i][1])
    out.append(segs[-1][1])

    if closed:
        p = line_intersection(segs[-1][0], segs[-1][1], segs[0][0], segs[0][1])
        if p is not None:
            out[0] = p
            out[-1] = p
        out.pop()
    return out


def offset_pair(
    points: Sequence[Point], thickness: float, closed: bool = False
) -> tuple[list[Vec], list[Vec]]:
    """The two faces of a wall centreline: (left, right) at +/- thickness/2."""
    half = thickness / 2.0
    return offset_polyline(points, half, closed), offset_polyline(points, -half, closed)


def inner_boundary(points: Sequence[Point], thickness: float, closed: bool = True) -> list[Vec]:
    """Face of a wall ring that bounds the room, so the area excludes the walls."""
    ring = dedupe(points)
    sign = 1.0 if signed_area(ring) >= 0 else -1.0
    return offset_polyline(ring, sign * thickness / 2.0, closed)


def point_along(points: Sequence[Point], distance: float) -> Vec:
    """Point at `distance` measured along a polyline from its start.

    Zero-length segments are skipped rather than divided by: a closed ring passed
    as a run carries its first vertex again at the end, and a repeated vertex must
    not look like a direction.
    """
    if distance <= 0:
        return xy(points[0])
    travelled = 0.0
    for i in range(len(points) - 1):
        seg = dist(points[i], points[i + 1])
        if seg <= EPS:
            continue
        if travelled + seg >= distance:
            u = unit(sub(points[i + 1], points[i]))
            return add(points[i], mul(u, distance - travelled))
        travelled += seg
    return xy(points[-1])


def angle_along(points: Sequence[Point], distance: float) -> float:
    """Direction of the polyline at `distance` from its start, in degrees."""
    travelled = 0.0
    for i in range(len(points) - 1):
        seg = dist(points[i], points[i + 1])
        if seg <= EPS:
            continue
        if travelled + seg >= distance or i == len(points) - 2:
            return angle_of(points[i], points[i + 1])
        travelled += seg
    return 0.0
