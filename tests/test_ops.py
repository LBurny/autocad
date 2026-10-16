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

from acadmcp import layers, ops, sheet  # noqa: E402
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


# ---------------------------------------------------------------------------
# the drafting symbols: level mark, north arrow, section mark
#
# Every size is a paper millimetre times the backend's text_scale(), so the symbol is the same
# size on the paper at any plot scale. The tests draw eagerly (outside a step) so the entities
# land in the modelspace and their coordinates can be read straight back.
# ---------------------------------------------------------------------------


def _scaled(denominator: int):
    """A dxf backend whose sheet plots at 1:`denominator`, so paper sizes can be checked."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, denominator))
    return be


def _entities(be, kind: str) -> list:
    return [e for e in be.msp if e.dxftype() == kind]


def _text_at(entity) -> tuple[float, float]:
    """The anchor of a placed TEXT, whichever alignment it carries."""
    _, p1, p2 = entity.get_placement()
    point = p1 if p2 is None else p2
    return (float(point.x), float(point.y))


# --- the level mark --------------------------------------------------------


def test_a_level_mark_is_an_apex_down_triangle_on_the_point():
    be = make_backend("dxf")
    ops.draw_level_mark(be, (1000.0, 2000.0), "\u00b10.000")

    height = ops.LEVEL_TRIANGLE_MM * 100.0
    pts = [
        (round(float(x), 6), round(float(y), 6))
        for x, y in _entities(be, "LWPOLYLINE")[0].get_points("xy")
    ]
    # Apex on the point noted, and the base a triangle-height above it.
    assert (1000.0, 2000.0) in pts
    assert (1000.0 - height, 2000.0 + height) in pts
    assert (1000.0 + height, 2000.0 + height) in pts


def test_a_level_mark_runs_its_line_to_the_right_of_the_triangle():
    be = make_backend("dxf")
    ops.draw_level_mark(be, (0.0, 0.0), "\u00b10.000")

    height = ops.LEVEL_TRIANGLE_MM * 100.0
    text_h = layers.text_height("dim", 100.0)
    line = _entities(be, "LINE")[0]
    assert (line.dxf.start.x, line.dxf.start.y) == pytest.approx((height, height))
    assert line.dxf.end.y == pytest.approx(height)
    # Long enough for the text plus a small margin.
    assert line.dxf.end.x - line.dxf.start.x == pytest.approx(
        layers.text_width("\u00b10.000", text_h) + text_h * 0.5
    )


def test_a_level_mark_writes_the_level_above_the_line():
    be = make_backend("dxf")
    ops.draw_level_mark(be, (0.0, 0.0), "\u00b10.000")

    text_h = layers.text_height("dim", 100.0)
    txt = _entities(be, "TEXT")[0]
    assert txt.dxf.text == "\u00b10.000"
    assert txt.dxf.height == pytest.approx(text_h)
    assert _text_at(txt)[0] == pytest.approx(ops.LEVEL_TRIANGLE_MM * 100.0)
    assert _text_at(txt)[1] > ops.LEVEL_TRIANGLE_MM * 100.0  # above the line


def test_a_mirrored_level_mark_runs_left_and_keeps_its_text_readable():
    be = make_backend("dxf")
    ops.draw_level_mark(be, (0.0, 0.0), "\u00b10.000", mirror=True)

    height = ops.LEVEL_TRIANGLE_MM * 100.0
    line = _entities(be, "LINE")[0]
    assert line.dxf.start.x == pytest.approx(-height)
    assert line.dxf.end.x < line.dxf.start.x
    # Anchored at the triangle's top corner with the text right-aligned: it still reads
    # left-to-right rather than mirroring with the mark.
    assert _text_at(_entities(be, "TEXT")[0])[0] == pytest.approx(-height)


def test_a_level_mark_needs_the_level_it_notes(be):
    with pytest.raises(ValueError, match="needs the level"):
        ops.draw_level_mark(be, (0.0, 0.0), "")

    assert list(be.msp) == []


def test_the_level_mark_is_a_paper_size_and_grows_with_the_plot_scale():
    for denominator in (50, 100, 200):
        be = _scaled(denominator)
        ops.draw_level_mark(be, (0.0, 0.0), "A")

        apex = _entities(be, "LWPOLYLINE")[0].get_points("xy")
        ys = [float(y) for _, y in apex]
        assert max(ys) - min(ys) == pytest.approx(ops.LEVEL_TRIANGLE_MM * denominator)
        assert _entities(be, "TEXT")[0].dxf.height == pytest.approx(
            layers.text_height("dim", denominator)
        )


# --- the north arrow -------------------------------------------------------


def test_a_north_arrow_is_a_circle_with_a_filled_needle_pointing_up():
    be = make_backend("dxf")
    ops.draw_north_arrow(be, (500.0, 500.0))

    radius = ops.NORTH_ARROW_DIAMETER_MM * 100.0 / 2.0
    circle = _entities(be, "CIRCLE")[0]
    assert (circle.dxf.center.x, circle.dxf.center.y) == pytest.approx((500.0, 500.0))
    assert circle.dxf.radius == pytest.approx(radius)

    points = [(float(x), float(y)) for x, y in _entities(be, "LWPOLYLINE")[0].get_points("xy")]
    ys = [y for _, y in points]
    assert max(ys) == pytest.approx(500.0 + radius)  # the single tip, at the circle's top
    assert min(ys) == pytest.approx(500.0 - radius)  # the base, at the circle's bottom
    assert sum(1 for y in ys if abs(y - (500.0 + radius)) < 1e-6) == 1

    # The needle is the standard's thin pointer, not a wide head: its tail is
    # NORTH_ARROW_TAIL_MM of paper, which at 1:100 is 3 mm on the sheet.
    base_xs = sorted(x for x, y in points if abs(y - (500.0 - radius)) < 1e-6)
    assert len(base_xs) == 2
    assert base_xs[1] - base_xs[0] == pytest.approx(ops.NORTH_ARROW_TAIL_MM * 100.0)

    assert _entities(be, "HATCH")[0].dxf.solid_fill == 1


def test_a_north_arrow_labels_north_above_the_circle():
    be = make_backend("dxf")
    ops.draw_north_arrow(be, (0.0, 0.0))

    radius = ops.NORTH_ARROW_DIAMETER_MM * 100.0 / 2.0
    text_h = layers.text_height("text", 100.0)
    label = _entities(be, "TEXT")[0]
    assert label.dxf.text == "\u5317"
    assert label.dxf.height == pytest.approx(text_h)
    assert _text_at(label)[1] > radius


def test_the_north_arrow_is_a_paper_size_and_grows_with_the_plot_scale():
    for denominator in (50, 100, 200):
        be = _scaled(denominator)
        ops.draw_north_arrow(be, (0.0, 0.0))

        circle = _entities(be, "CIRCLE")[0]
        assert circle.dxf.radius == pytest.approx(ops.NORTH_ARROW_DIAMETER_MM * denominator / 2.0)
        assert _entities(be, "TEXT")[0].dxf.height == pytest.approx(
            layers.text_height("text", denominator)
        )


# --- the section mark ------------------------------------------------------


def test_a_section_mark_ticks_left_and_right_of_the_travel():
    tick = ops.SECTION_TICK_MM * 100.0
    for side, sign in (("left", 1.0), ("right", -1.0)):
        be = make_backend("dxf")
        ops.draw_section_mark(be, (0.0, 0.0), (4000.0, 0.0), "1-1", side=side)

        lines = _entities(be, "LINE")
        assert len(lines) == 3  # the cut line and two ticks
        ticks = [e for e in lines if abs(e.dxf.start.x - e.dxf.end.x) < 1e-6]
        assert len(ticks) == 2
        assert {round(float(t.dxf.start.x), 3) for t in ticks} == {0.0, 4000.0}
        for t in ticks:
            # 'left' of the +X travel is +Y, 'right' is -Y.
            assert (t.dxf.end.y - t.dxf.start.y) == pytest.approx(sign * tick)


def test_a_section_mark_names_the_section_at_each_end():
    be = make_backend("dxf")
    ops.draw_section_mark(be, (0.0, 0.0), (4000.0, 0.0), "1-1")

    tick = ops.SECTION_TICK_MM * 100.0
    labels = _entities(be, "TEXT")
    assert [e.dxf.text for e in labels] == ["1-1", "1-1"]
    assert {round(_text_at(e)[0], 3) for e in labels} == {0.0, 4000.0}
    for e in labels:
        assert _text_at(e)[1] > tick  # beyond the tick's outer end


def test_a_section_mark_turns_with_its_cut_direction():
    be = make_backend("dxf")
    # A cut running up the sheet: 'left' of +Y travel is -X.
    ops.draw_section_mark(be, (0.0, 0.0), (0.0, 4000.0), "2-2", side="left")

    ticks = [e for e in _entities(be, "LINE") if abs(e.dxf.start.y - e.dxf.end.y) < 1e-6]
    assert len(ticks) == 2
    for t in ticks:
        assert (t.dxf.end.x - t.dxf.start.x) == pytest.approx(-ops.SECTION_TICK_MM * 100.0)


def test_a_section_mark_refuses_a_coincident_pair(be):
    with pytest.raises(ValueError, match="distinct"):
        ops.draw_section_mark(be, (1.0, 2.0), (1.0, 2.0), "1-1")

    assert list(be.msp) == []


@pytest.mark.parametrize("side", ["up", "Left", ""])
def test_a_section_mark_refuses_an_unknown_side(be, side):
    with pytest.raises(ValueError, match="'left' or 'right'"):
        ops.draw_section_mark(be, (0.0, 0.0), (1000.0, 0.0), "1-1", side=side)


def test_a_section_mark_needs_the_sections_name(be):
    with pytest.raises(ValueError, match="label"):
        ops.draw_section_mark(be, (0.0, 0.0), (1000.0, 0.0), "")


def test_the_section_tick_is_a_paper_size_and_grows_with_the_plot_scale():
    for denominator in (50, 100, 200):
        be = _scaled(denominator)
        ops.draw_section_mark(be, (0.0, 0.0), (4000.0, 0.0), "1-1")

        tick = max(abs(float(e.dxf.end.y) - float(e.dxf.start.y)) for e in _entities(be, "LINE"))
        assert tick == pytest.approx(ops.SECTION_TICK_MM * denominator)
        assert _entities(be, "TEXT")[0].dxf.height == pytest.approx(
            layers.text_height("text", denominator)
        )


# --- a bare opening's tag --------------------------------------------------


def test_a_tag_on_a_bare_opening_is_drawn():
    """The docstring promises `tag` for every opening, not only the two that carry a symbol.

    A sandbox run found the opposite — `kind="opening"` dropped the tag and still returned ok —
    so a 卷帘门 hole could not be named at all, and the only clue was an entity count five
    lower than the same call made with `kind="door"`.
    """
    be = make_backend("dxf")
    result = ops.draw_wall_with_openings(
        be,
        [(0.0, 0.0), (6000.0, 0.0)],
        [{"start": 2000.0, "width": 2000.0, "kind": "opening", "tag": "JLM1234"}],
        240.0,
    )

    texts = _entities(be, "TEXT")
    assert [t.dxf.text for t in texts] == ["JLM1234"]
    assert texts[0].dxf.height == pytest.approx(layers.text_height("tag", 100.0))
    assert result["symbols"] >= 1


def test_a_bare_opening_without_a_tag_leaves_the_gap_bare():
    be = make_backend("dxf")
    ops.draw_wall_with_openings(
        be,
        [(0.0, 0.0), (6000.0, 0.0)],
        [{"start": 2000.0, "width": 2000.0, "kind": "opening"}],
        240.0,
    )
    assert _entities(be, "TEXT") == []
