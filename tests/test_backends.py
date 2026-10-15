"""End-to-end tests against the headless DXF backend — no AutoCAD required."""

from __future__ import annotations

import sys
from pathlib import Path

import ezdxf
import pytest
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import layers, ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


@pytest.fixture()
def be():
    return make_backend("dxf")


def test_unknown_backend_is_rejected():
    with pytest.raises(BackendError):
        make_backend("solidworks")


def test_setup_layers_creates_the_standard_set(be):
    created = ops.setup_layers(be)
    assert "A-WALL" in created and "A-DOOR" in created
    assert be.ensure_layer("A-WALL") is not None


def test_wall_draws_two_faces_and_caps(be):
    before = be.info()["entities"]
    ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    # two face polylines plus the two end caps
    assert be.info()["entities"] - before == 4


def test_wall_hatch_adds_poche(be):
    before = be.info()["entities"]
    ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200, hatch=True)
    assert be.info()["entities"] - before == 5


def test_room_area_excludes_the_walls(be):
    report = ops.draw_room(
        be, [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], "\u6d4b\u8bd5\u5ba4", 200
    )
    assert report["area_m2"] == pytest.approx(3800 * 2800 / 1e6, rel=1e-6)
    assert report["centroid"] == (2000.0, 1500.0)


def test_room_without_a_name_still_reports_area(be):
    report = ops.add_room_label(be, [(0, 0), (2000, 0), (2000, 2000), (0, 2000)], None, 200)
    assert report["area_m2"] == pytest.approx(1800 * 1800 / 1e6, rel=1e-6)


def test_openings_punch_the_wall_into_segments(be):
    report = ops.draw_wall_with_openings(
        be,
        [(0, 0), (10000, 0)],
        [
            {"start": 2000, "width": 1000, "kind": "door", "tag": "M1022"},
            {"start": 6000, "width": 1500, "kind": "window", "tag": "C1212"},
        ],
        200,
    )
    assert report["segments"] == 3
    assert report["openings"] == 2
    assert report["total_length"] == 10000


def test_openings_are_clamped_to_the_run_length(be):
    report = ops.draw_wall_with_openings(
        be, [(0, 0), (5000, 0)], [{"start": 4000, "width": 1500, "kind": "door"}], 200
    )
    assert report["segments"] == 1  # only the 0..4000 stub survives
    assert report["clamped_openings"] == 1


def test_an_opening_past_the_end_is_dropped_not_drawn(be):
    report = ops.draw_wall_with_openings(
        be, [(0, 0), (5000, 0)], [{"start": 6000, "width": 900, "kind": "door"}], 200
    )
    assert report["openings"] == 0
    assert report["clamped_openings"] == 1
    assert report["symbols"] == 0


def test_door_tag_offset_flips_the_tag_across_the_wall(be):
    near = ops.draw_door(be, (0, 0), 900, 200, rotation=0.0, tag="M0921")
    far = ops.draw_door(be, (0, 0), 900, 200, rotation=0.0, tag="M0921", tag_offset=-300)
    assert near[-1].dxf.insert[1] > 0 > far[-1].dxf.insert[1]


def test_door_emits_leaf_arc_and_two_jambs(be):
    made = ops.draw_door(be, (0, 0), 900, 200, rotation=0.0, tag="M0921")
    types = [e.dxftype() for e in made]
    assert "ARC" in types and types.count("LINE") == 3  # leaf + two jamb ticks


def test_window_emits_four_panes_and_two_jambs(be):
    made = ops.draw_window(be, (0, 0), 1500, 200, rotation=0.0)
    assert [e.dxftype() for e in made].count("LINE") == 6


def test_axis_grid_labels_default_to_numbers_and_letters(be):
    report = ops.draw_axis_grid(be, [0, 6000, 12000], [0, 8000], extent=(0, 0, 12000, 8000))
    assert report == {"x_axes": 3, "y_axes": 2}
    assert ops._alpha(0) == "A" and ops._alpha(25) == "Z" and ops._alpha(26) == "AA"


def test_leader_note_aligns_text_away_from_the_anchor(be):
    made = ops.leader_note(be, (0, 0), "note", direction=0.0, length=1000)
    assert made[0].dxftype() == "LINE"


def test_dimension_chain_adds_an_overall_dimension(be):
    report = ops.dimension_chain(be, [(0, 0), (6000, 0), (12000, 0)], offset=-1500)
    assert report["segments"] == 2
    assert report["total"] == 12000
    assert report["dimensions"] == 3


def test_text_lands_on_a_cjk_capable_style(be):
    ops.annotate(be, (0, 0), "无菌室")
    style = be.doc.styles.get(be.text_style)
    # A TrueType face, because an SHX font without a big font has no CJK glyphs.
    assert style.dxf.font.lower().endswith((".ttf", ".ttc"))
    texts = [e for e in be.msp if e.dxftype() == "TEXT"]
    assert texts and all(e.dxf.style == be.text_style for e in texts)


def test_text_style_can_match_a_client_standard(be):
    be.set_text_style("OFFICE", "gbenor.shx", "gbcbig.shx")
    style = be.doc.styles.get("OFFICE")
    assert style.dxf.font == "gbenor.shx"
    assert style.dxf.bigfont == "gbcbig.shx"
    ops.annotate(be, (0, 0), "无菌室")
    assert [e for e in be.msp if e.dxftype() == "TEXT"][-1].dxf.style == "OFFICE"


def test_screenshot_writes_a_real_png(be, tmp_path):
    pytest.importorskip("matplotlib")
    ops.draw_room(be, [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], "无菌室", 200)
    out = be.screenshot(str(tmp_path / "shot.png"))
    with PILImage.open(out) as image:
        assert image.format == "PNG"
        # The canvas follows the drawing's aspect; the long edge is what was asked.
        assert max(image.width, image.height) == 1600


def test_screenshot_window_frames_a_different_region(be, tmp_path):
    pytest.importorskip("matplotlib")
    ops.draw_room(be, [(0, 0), (8000, 0), (8000, 6000), (0, 6000)], "原料间", 200)
    full = Path(be.screenshot(str(tmp_path / "full.png"), width=600, height=400)).read_bytes()
    near = Path(
        be.screenshot(str(tmp_path / "near.png"), window=[0, 0, 2000, 1500], width=600, height=400)
    ).read_bytes()
    assert full != near


def test_preview_delegates_to_screenshot(be, tmp_path):
    pytest.importorskip("matplotlib")
    with PILImage.open(be.preview(str(tmp_path / "p.png"))) as image:
        assert image.format == "PNG"


def test_full_plan_saves_and_reopens(tmp_path):
    be = make_backend("dxf")
    ops.setup_layers(be)
    ops.draw_wall_with_openings(
        be,
        [(0, 0), (12000, 0)],
        [{"start": 1000, "width": 1500, "kind": "door", "tag": "FM\u4e591521"}],
        240,
    )
    ops.draw_room(be, [(0, 0), (6000, 0), (6000, 4000), (0, 4000)], "\u539f\u6599\u95f4", 200)
    ops.draw_stair(be, (100, 100), (2900, 3900), steps=8)
    ops.draw_lift(be, (6100, 100), (8900, 3900), tag="1#\u8d27\u68af1.6T")
    ops.draw_shaft(be, (9100, 100), (11900, 3900), "\u5f3a\u7535\u4e95")

    out = be.save(str(tmp_path / "plan.dxf"))
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    assert len(msp) > 20
    assert {"A-WALL", "A-DOOR", "A-STAIR", "A-LIFT", "A-SHAFT"} <= {
        layer.dxf.name for layer in doc.layers
    }


def test_com_backend_reports_clearly_when_autocad_is_absent():
    """The COM driver must fail with advice, not a bare COM traceback."""
    com = pytest.importorskip("win32com.client")
    del com
    from acadmcp.backends.com import ComBackend

    try:
        ComBackend(create=False)
    except BackendError as exc:
        assert "AutoCAD" in str(exc)
    except Exception as exc:  # noqa: BLE001 - a live AutoCAD may be present
        pytest.skip(f"AutoCAD session available: {exc}")


# ---------------------------------------------------------------------------
# wall rings, openings and the jamb that used to be drawn twice
# ---------------------------------------------------------------------------

RING = [(0, 0), (48000, 0), (48000, 24000), (0, 24000)]


def _wall_vertices(be) -> set[tuple[float, float]]:
    return {
        (round(p[0], 3), round(p[1], 3))
        for e in be.msp
        if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == "A-WALL"
        for p in e.get_points()
    }


def _doubled_lines(be) -> dict[tuple, list[str]]:
    seen: dict[tuple, list[str]] = {}
    for e in be.msp:
        if e.dxftype() != "LINE":
            continue
        a, b = e.dxf.start, e.dxf.end
        key = tuple(sorted([(round(a.x, 3), round(a.y, 3)), (round(b.x, 3), round(b.y, 3))]))
        seen.setdefault(key, []).append(e.dxf.layer)
    return {key: layers for key, layers in seen.items() if len(layers) > 1}


def test_a_closed_ring_with_openings_mitres_its_corners(be):
    report = ops.draw_wall_with_openings(
        be,
        RING,
        [{"start": 6000, "width": 4200, "kind": "door", "tag": "JLM4245"}],
        240,
        closed=True,
    )

    assert report["closed"] is True
    assert report["total_length"] == 144000.0  # the ring's perimeter, not one run
    assert report["openings"] == 1

    # The outer corner of an envelope whose faces are mitred. Drawn as four separate
    # runs — the only way to get openings in before `closed` existed — the west wall's
    # face stops at y=0 and the south wall's at x=0, so this point is never reached and
    # the outline steps in by 120 mm at every corner.
    vertices = _wall_vertices(be)
    assert (-120.0, -120.0) in vertices
    assert (48120.0, -120.0) in vertices
    assert (48120.0, 24120.0) in vertices
    assert (-120.0, 24120.0) in vertices

    assert _doubled_lines(be) == {}


def test_a_closed_ring_without_openings_is_one_mitred_wall(be):
    report = ops.draw_wall_with_openings(be, RING, [], 240, closed=True)

    assert report == {
        "segments": 1,
        "openings": 0,
        "clamped_openings": 0,
        "total_length": 144000.0,
        "symbols": 0,
        "closed": True,
    }
    assert (-120.0, -120.0) in _wall_vertices(be)


def test_a_closed_ring_needs_three_corners(be):
    with pytest.raises(ValueError, match="three corners"):
        ops.draw_wall_with_openings(be, [(0, 0), (1000, 0)], [], 240, closed=True)


def test_an_opening_does_not_draw_its_jamb_twice(be):
    ops.draw_wall_with_openings(
        be, [(0, 0), (10000, 0)], [{"start": 2000, "width": 1000, "kind": "door"}], 200
    )

    # The wall's end cap and the door's jamb tick are the same line across the same
    # thickness. Drawing both put each jamb in the drawing twice, on two layers, which
    # was 40 of the 166 line segments of a real plan.
    assert _doubled_lines(be) == {}

    jambs = [
        e
        for e in be.msp
        if e.dxftype() == "LINE"
        and {round(e.dxf.start.x, 3), round(e.dxf.end.x, 3)} == {2000.0}
        and {round(e.dxf.start.y, 3), round(e.dxf.end.y, 3)} == {-100.0, 100.0}
    ]
    assert [e.dxf.layer for e in jambs] == ["A-DOOR"]


def test_a_bare_opening_still_has_its_wall_closed(be):
    ops.draw_wall_with_openings(
        be, [(0, 0), (10000, 0)], [{"start": 3000, "width": 1000, "kind": "opening"}], 200
    )

    # Nothing draws a symbol there, so the wall has to close its own jambs — once each.
    jambs = [
        e
        for e in be.msp
        if e.dxftype() == "LINE"
        and {round(e.dxf.start.x, 3), round(e.dxf.end.x, 3)} in ({3000.0}, {4000.0})
    ]
    assert sorted(e.dxf.layer for e in jambs) == ["A-WALL", "A-WALL"]
    assert _doubled_lines(be) == {}


# ---------------------------------------------------------------------------
# the stair's 上/下 labels, and where its outline goes
# ---------------------------------------------------------------------------


def test_stair_labels_sit_at_the_two_ends_of_the_flight(be):
    ops.draw_stair(be, (0, 0), (3600, 3000), steps=10, axis="y")

    marks = {
        e.dxf.text: (e.dxf.insert[0], e.dxf.insert[1]) for e in be.msp if e.dxftype() == "TEXT"
    }
    up, down = marks["上"], marks["下"]

    # At the two ends of the travel, on the same side of the arrow. They used to land
    # in the middle of the flight 480 mm apart, on top of the arrow and each other.
    assert up[1] > down[1]
    assert abs(up[1] - down[1]) > 2000
    assert abs(up[0] - down[0]) < 1e-6
    assert (0 <= up[0] <= 3600) and (0 <= up[1] <= 3000)
    assert (0 <= down[0] <= 3600) and (0 <= down[1] <= 3000)


def test_stair_labels_follow_the_flight_along_x_too(be):
    ops.draw_stair(be, (0, 0), (3000, 3600), steps=10, axis="x")

    marks = {
        e.dxf.text: (e.dxf.insert[0], e.dxf.insert[1]) for e in be.msp if e.dxftype() == "TEXT"
    }
    up, down = marks["上"], marks["下"]

    assert up[0] > down[0]
    assert abs(up[0] - down[0]) > 2000
    assert abs(up[1] - down[1]) < 1e-6


def test_the_stair_outline_is_on_the_stair_layer(be):
    ops.draw_stair(be, (0, 0), (3600, 3000), steps=4)

    # The outline is geometry, not annotation: turning A-ANNO off used to take the
    # outline with it and leave the treads floating.
    outline = [e for e in be.msp if e.dxftype() == "LWPOLYLINE" and e.closed]
    assert [e.dxf.layer for e in outline] == ["A-STAIR"]
    assert {e.dxf.layer for e in be.msp if e.dxftype() == "LINE"} == {"A-STAIR"}


# ---------------------------------------------------------------------------
# captions sized to fit what they sit in
# ---------------------------------------------------------------------------


def test_text_width_follows_the_measured_advance():
    # Measured: "生产车间" at height 350 is 1991 mm wide in AutoCAD, 497.8 mm a character.
    assert layers.text_width("生产车间", 350) == pytest.approx(1988.0, rel=0.01)
    assert layers.text_width("6000", 250) == pytest.approx(750.0, rel=0.01)
    assert layers.text_width("", 350) == 0


def test_a_long_shaft_name_shrinks_instead_of_spilling(be):
    ops.draw_shaft(be, (0, 0), (1500, 1500), "消防水井")
    wide = [e for e in be.msp if e.dxftype() == "TEXT"][-1]

    assert wide.dxf.height < layers.H_TEXT
    assert layers.text_width(wide.dxf.text, wide.dxf.height) <= 1500 * 0.9 + 1e-6


def test_a_short_shaft_name_keeps_the_normal_height(be):
    ops.draw_shaft(be, (0, 0), (1500, 1500), "水井")
    text = [e for e in be.msp if e.dxftype() == "TEXT"][-1]

    assert text.dxf.height == layers.H_TEXT


def test_equipment_captions_shrink_too(be):
    ops.draw_equipment(be, (0, 0), (1200, 800), "螺杆式空气压缩机")
    text = [e for e in be.msp if e.dxftype() == "TEXT"][-1]

    assert layers.text_width(text.dxf.text, text.dxf.height) <= 1200 * 0.9 + 1e-6


# ---------------------------------------------------------------------------
# the boundary the area is measured to
# ---------------------------------------------------------------------------


def test_the_area_boundary_is_opt_in(be):
    ops.add_room_label(be, [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], "room", 200)

    assert [e for e in be.msp if e.dxf.layer == "A-ROOM-BOUND"] == []


def test_a_room_can_draw_the_face_its_area_is_measured_to(be):
    report = ops.add_room_label(
        be,
        [(0, 0), (4000, 0), (4000, 3000), (0, 3000)],
        "room",
        200,
        bound_layer="A-ROOM-BOUND",
    )

    bounds = [e for e in be.msp if e.dxf.layer == "A-ROOM-BOUND"]
    assert len(bounds) == 1
    xs = [p[0] for p in bounds[0].get_points()]
    ys = [p[1] for p in bounds[0].get_points()]
    # 100 mm inside the centreline ring, which is the wall's half thickness.
    assert (min(xs), max(xs)) == (100.0, 3900.0)
    assert (min(ys), max(ys)) == (100.0, 2900.0)
    assert report["area_m2"] == pytest.approx(3800 * 2800 / 1e6, rel=1e-6)
