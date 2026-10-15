"""Input guards on the drawing ops: a caller's slip reads as a slip, not a crash.

Each guard here was born from a reproduced failure — an empty polyline that reported success
while drawing nothing, an empty wall run and an empty dimension chain that raised a bare
``IndexError`` the MCP client masks behind "Error executing tool". They are ops-level checks,
so they need no AutoCAD and no backend beyond the headless one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


@pytest.fixture()
def be():
    return make_backend("dxf")


def _primitives(be) -> list[dict]:
    return [p for step in be.journal.steps for p in step.primitives]


def test_an_empty_polyline_is_refused(be):
    """It used to record a step that drew nothing and return ok with 0 vertices — the step,
    the drawing and the caller all claimed a polyline that was never there."""
    with pytest.raises(ValueError) as caught:
        ops.draw_polyline(be, [])

    assert "at least 2 points" in str(caught.value)
    assert "got 0" in str(caught.value)
    assert _primitives(be) == []


def test_a_one_point_polyline_is_refused(be):
    with pytest.raises(ValueError) as caught:
        ops.draw_polyline(be, [(0, 0)])

    assert "got 1" in str(caught.value)


def test_a_two_point_polyline_is_drawn(be):
    with be.step("draw_polyline"):
        made = ops.draw_polyline(be, [(0, 0), (1000, 0)])

    assert made
    drawn = [p for p in _primitives(be) if p["kind"] == "polyline"]
    assert len(drawn) == 1
    assert drawn[0]["points"] == [0.0, 0.0, 1000.0, 0.0]


def test_a_wall_run_of_fewer_than_two_points_is_refused(be):
    """The empty case was a bare ``IndexError`` (masked from the client); one point was an
    empty step at the commit. Both are a caller slip and say so here."""
    for points in ([], [(0, 0)]):
        with pytest.raises(ValueError) as caught:
            ops.draw_wall_with_openings(be, points, [], 200)
        assert "at least 2 points" in str(caught.value)


def test_a_dimension_chain_of_fewer_than_two_points_is_refused(be):
    for points in ([], [(0, 0)]):
        with pytest.raises(ValueError) as caught:
            ops.dimension_chain(be, points)
        assert "at least 2 points" in str(caught.value)


def test_a_bare_wall_run_is_still_guarded(be):
    """`draw_wall_run` was already guarded by the geometry layer; this pins that it stays a
    readable ValueError rather than regressing into a backend crash."""
    for points in ([], [(0, 0)]):
        with pytest.raises(ValueError):
            ops.draw_wall_run(be, points, 200)
