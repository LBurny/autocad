"""Text heights follow the sheet's plot scale.

A text height is a *paper* height from the standard's nominal series times the
plot scale, so a plan headed for 1:200 draws its labels at 700/500 mm and passes
drawing_check_standard, instead of the 1:100 constants failing every text the
moment the sheet is not 1:100. The shipped 1:100 constants themselves sit inside
the GB/T series — H_TEXT was 300 (3.0 mm, off it) and H_TAG 180 (1.8 mm, off
it), which sandbox agents had to work around by hand.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import layers, ops, sheet, standards  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


def test_text_height_is_the_roles_paper_height_times_the_scale():
    assert layers.text_height("room_name", 100.0) == 350.0
    assert layers.text_height("room_name", 200.0) == 700.0
    assert layers.text_height("tag", 100.0) == 250.0
    assert layers.text_height("axis", 50.0) == 250.0


def test_an_unknown_text_role_is_refused_by_name():
    with pytest.raises(ValueError, match="title"):
        layers.text_height("title", 100.0)


def test_the_shipped_heights_sit_on_the_gbt_series_at_1_100():
    series = standards.current().get("gbt50001").text_heights
    heights = (
        layers.H_TEXT,
        layers.H_ROOM_NAME,
        layers.H_ROOM_AREA,
        layers.H_DIM,
        layers.H_TAG,
        layers.H_AXIS,
    )
    for h in heights:
        assert h / 100.0 in series, f"{h} mm is off the nominal series at 1:100"


def test_the_series_layers_uses_is_the_standards_own():
    std = standards.current().get("gbt50001")
    assert tuple(sorted(std.text_heights)) == layers.TEXT_HEIGHT_SERIES


def test_a_backend_without_a_sheet_assumes_1_100():
    assert make_backend("dxf").text_scale() == 100.0


def test_the_text_scale_follows_the_page_setup():
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 200))
    assert be.text_scale() == 200.0


def test_a_reopened_drawing_adopts_its_sheets_text_scale(tmp_path):
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 200))
    target = str(tmp_path / "scaled.dxf")
    be.save(target)

    assert make_backend("dxf", path=target).text_scale() == 200.0


def test_room_labels_follow_the_sheet_scale():
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 200))
    ops.draw_room(be, [(0, 0), (5000, 0), (5000, 4000), (0, 4000)], name="办公室")

    heights = {round(e.dxf.height, 3) for e in be.msp if e.dxftype() == "TEXT"}
    assert 700.0 in heights  # the name, 3.5 mm on paper
    assert 500.0 in heights  # the area, 2.5 mm on paper


def test_axis_bubble_text_is_a_series_height_not_a_bubble_fraction():
    be = make_backend("dxf")
    ops.draw_axis_grid(be, [0.0], [0.0], extent=(-1000.0, -1000.0, 1000.0, 1000.0))

    heights = {e.dxf.height for e in be.msp if e.dxftype() == "TEXT"}
    assert heights == {layers.text_height("axis", 100.0)}


def test_snap_to_series_rounds_down_in_paper_terms():
    assert layers.snap_to_series(320.0, 100.0) == 250.0
    assert layers.snap_to_series(350.0, 100.0) == 350.0
    assert layers.snap_to_series(200.0, 100.0) == 200.0  # below the minimum: kept
    assert layers.snap_to_series(640.0, 200.0) == 500.0  # 3.2 paper at 1:200 -> 2.5


def test_a_shrunken_room_label_snaps_to_the_series():
    """fit_text_height's answer is off the series; the label must still land on it."""
    be = make_backend("dxf")
    # 6 CJK chars x 1.42 advance: across ~3029 mm inner width the fit is ~320 mm.
    ops.draw_room(be, [(0, 0), (3229, 0), (3229, 3000), (0, 3000)], name="无障碍卫生间")

    names = [e for e in be.msp if e.dxftype() == "TEXT" and e.dxf.layer == "A-ROOM-NAME"]
    assert len(names) == 1
    assert round(names[0].dxf.height, 3) == 250.0  # snapped down to 2.5 mm paper, not ~320
