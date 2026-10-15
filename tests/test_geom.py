"""Geometry tests: the offset maths is what every wall and opening depends on."""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import geom  # noqa: E402


def test_area_of_square():
    assert geom.area([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]) == 1_000_000


def test_area_is_orientation_independent():
    cw = [(0, 0), (0, 1000), (1000, 1000), (1000, 0)]
    assert geom.area(cw) == 1_000_000


def test_centroid_of_square():
    assert geom.centroid([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]) == (500.0, 500.0)


def test_offset_open_polyline_shifts_left():
    left = geom.offset_polyline([(0, 0), (1000, 0)], 50)
    assert left == [(0.0, 50.0), (1000.0, 50.0)]


def test_offset_pair_wraps_the_centreline():
    left, right = geom.offset_pair([(0, 0), (1000, 0)], 200)
    assert left == [(0.0, 100.0), (1000.0, 100.0)]
    assert right == [(0.0, -100.0), (1000.0, -100.0)]


def test_offset_mitres_a_right_angle():
    left = geom.offset_polyline([(0, 0), (1000, 0), (1000, 1000)], 100)
    assert left[0] == (0.0, 100.0)
    assert left[1] == (900.0, 100.0)
    assert left[2] == (900.0, 1000.0)


def test_closed_offset_keeps_vertex_count_and_closes():
    ring = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
    inner = geom.offset_polyline(ring, 100, closed=True)
    assert len(inner) == 4
    assert geom.area(inner) < geom.area(ring)


def test_inner_boundary_shrinks_by_the_wall():
    ring = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
    inner = geom.inner_boundary(ring, 200)
    # 3800 x 2800 mm, whatever winding the caller passed in.
    assert geom.area(inner) == 3800 * 2800


def test_inner_boundary_is_winding_independent():
    ccw = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
    cw = list(reversed(ccw))
    assert geom.area(geom.inner_boundary(ccw, 200)) == geom.area(geom.inner_boundary(cw, 200))


def test_point_along_walks_corners():
    pts = [(0, 0), (1000, 0), (1000, 1000)]
    assert geom.point_along(pts, 500) == (500.0, 0.0)
    assert geom.point_along(pts, 1500) == (1000.0, 500.0)


def test_angle_along_returns_the_local_direction():
    pts = [(0, 0), (1000, 0), (1000, 1000)]
    assert geom.angle_along(pts, 500) == 0.0
    assert geom.angle_along(pts, 1500) == 90.0


def test_polar_and_rotate():
    x, y = geom.polar((0, 0), 90, 100)
    assert math.isclose(x, 0.0, abs_tol=1e-9) and math.isclose(y, 100.0, abs_tol=1e-9)
    x, y = geom.rotate((100, 0), (0, 0), 90)
    assert math.isclose(x, 0.0, abs_tol=1e-9) and math.isclose(y, 100.0, abs_tol=1e-9)


def test_rect_corners_orders_them_around_the_ring():
    corners = geom.rect_corners((1000, 2000), (0, 0))
    assert corners == [(0.0, 0.0), (1000.0, 0.0), (1000.0, 2000.0), (0.0, 2000.0)]


def test_dedupe_removes_repeats():
    assert geom.dedupe([(0, 0), (0, 0), (1, 1)]) == [(0.0, 0.0), (1.0, 1.0)]


def test_ring_self_intersects_flags_a_bowtie():
    bowtie = [(0, 0), (3000, 3000), (3000, 0), (0, 3000)]
    assert geom.ring_self_intersects(bowtie)


def test_ring_self_intersects_passes_simple_rings():
    square = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
    l_shape = [(0, 0), (6000, 0), (6000, 3000), (3000, 3000), (3000, 1500), (0, 1500)]
    triangle = [(0, 0), (1000, 0), (500, 800)]
    assert not geom.ring_self_intersects(square)
    assert not geom.ring_self_intersects(l_shape)
    assert not geom.ring_self_intersects(triangle)
    # A ring handed over with its first vertex repeated is the same ring.
    assert not geom.ring_self_intersects([*square, square[0]])


def test_ring_self_intersects_flags_a_ring_that_touches_itself():
    # The last segment returns to a vertex lying on the first segment: no
    # crossing, but no meaningful inside either.
    touching = [(0, 0), (1000, 0), (1000, 1000), (500, 0)]
    assert geom.ring_self_intersects(touching)


def test_dist_point_segment_measures_to_the_segment_not_its_line():
    # Beside the middle, where the foot of the perpendicular is on the segment, the
    # answer is the same as to the infinite line.
    assert geom.dist_point_segment((5, 3), (0, 0), (10, 0)) == 3.0
    assert geom.dist_point_segment((3, 0), (0, 0), (10, 0)) == 0.0


def test_dist_point_segment_clamps_past_both_ends():
    # Past an end the nearest point is that end, not the line's foot — which is the
    # whole reason a wall centreline is tested segment by segment.
    assert geom.dist_point_segment((15, 4), (0, 0), (10, 0)) == math.hypot(5, 4)
    assert geom.dist_point_segment((-5, 4), (0, 0), (10, 0)) == math.hypot(5, 4)


def test_dist_point_segment_handles_a_degenerate_segment():
    assert geom.dist_point_segment((3, 4), (1, 1), (1, 1)) == math.hypot(2, 3)


# --- xy: a point is exactly two numbers -------------------------------------


def test_xy_takes_any_two_item_sequence():
    assert geom.xy([1, 2]) == (1.0, 2.0)
    assert geom.xy((1, 2)) == (1.0, 2.0)


def test_xy_refuses_a_point_that_is_not_exactly_two_coordinates():
    """A z silently dropped is a wrong-looking point nothing catches. `xy` is where a
    placement's `at` lands (server.py calls `geom.xy(at)`), so a 3-tuple must refuse."""
    import pytest

    for bad in ([0, 0, 0], (0, 0, 5), [0], []):
        with pytest.raises(ValueError, match="two numbers"):
            geom.xy(bad)


def test_xy_refuses_a_member_that_is_not_a_number():
    import pytest

    with pytest.raises(ValueError, match="numbers"):
        geom.xy([0, "y"])
    with pytest.raises(ValueError, match="numbers"):
        geom.xy([None, 0])
