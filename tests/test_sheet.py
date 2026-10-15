"""The sheet: paper, scale, frame, title block, and the two read-backs."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import sheet  # noqa: E402


def test_the_paper_sizes_are_the_a_series_in_millimetres():
    """GB/T 50001 takes its sheet sizes from the ISO A series, so these are that series."""
    assert sheet.PAPERS["A3"] == (420.0, 297.0)
    assert sheet.PAPERS["A0"] == (1189.0, 841.0)
    assert sheet.PAPERS["A4"] == (297.0, 210.0)


def test_an_unknown_paper_names_what_there_is():
    with pytest.raises(ValueError, match="A3"):
        sheet.resolve_paper("A9")


def test_portrait_swaps_the_sides_and_landscape_does_not():
    assert sheet.resolve_paper("A3").width == 420.0
    portrait = sheet.resolve_paper("A3", "portrait")
    assert (portrait.width, portrait.height) == (297.0, 420.0)
    assert portrait.landscape is False


def test_an_orientation_that_is_not_one_is_refused():
    with pytest.raises(ValueError, match="orientation"):
        sheet.resolve_paper("A3", "sideways")


def test_the_margins_are_gb_t_14689s():
    """A 25 mm bound edge on every size; 10 mm on the other three down to A2, 5 mm on A3/A4."""
    assert sheet.resolve_paper("A3").binding == 25.0
    assert sheet.resolve_paper("A2").margin == 10.0
    assert sheet.resolve_paper("A3").margin == 5.0
    assert sheet.resolve_paper("A4").margin == 5.0
    for name in sheet.PAPERS:
        paper = sheet.resolve_paper(name)
        assert paper.binding > paper.margin


def test_the_frame_is_inside_the_paper_by_its_margins():
    paper = sheet.resolve_paper("A3")
    xs = [p[0] for line in sheet.frame_outline(paper) for p in line]
    ys = [p[1] for line in sheet.frame_outline(paper) for p in line]
    assert min(xs) == paper.binding and max(xs) == paper.width - paper.margin
    assert min(ys) == paper.margin and max(ys) == paper.height - paper.margin


def test_the_title_block_sits_in_the_bottom_right_corner_of_the_frame():
    paper = sheet.resolve_paper("A3")
    lines, texts = sheet.title_block(paper, {"图名": "生产车间平面图", "比例": "1:100"})
    assert lines and texts
    xs = [p[0] for line in lines for p in line]
    ys = [p[1] for line in lines for p in line]
    frame_xs = [p[0] for line in sheet.frame_outline(paper) for p in line]
    frame_ys = [p[1] for line in sheet.frame_outline(paper) for p in line]
    assert max(xs) == max(frame_xs) and min(ys) == min(frame_ys)


def test_every_title_block_field_reaches_the_paper():
    _, texts = sheet.title_block(
        sheet.resolve_paper("A3"), {"图名": "生产车间平面图", "图号": "JS-01"}
    )
    drawn = [t[1] for t in texts]
    assert "生产车间平面图" in drawn and "JS-01" in drawn


def test_a_title_too_long_for_its_cell_is_shrunk_rather_than_crossing_the_frame():
    """Eighteen characters at 5 mm is 127.8 mm wide in a 125.6 mm cell, so it must shrink."""
    paper = sheet.resolve_paper("A3")
    long_title = "生产车间平面布置图设计总说明及门窗表"
    _, texts = sheet.title_block(paper, {"图名": long_title})
    assert sheet.text_width(long_title, sheet.TITLE_BLOCK_TEXT) > 125.6, "the cell must be tight"

    right_edge = paper.width - paper.margin
    for (x, _y), text, height, _align in texts:
        assert x + sheet.text_width(text, height) <= right_edge + 1e-6, text
    shrunk = next(t for t in texts if t[1] == long_title)
    assert shrunk[2] < sheet.TITLE_BLOCK_TEXT, "and it must actually have shrunk"


def test_more_fields_than_rows_are_dropped_not_drawn_over_the_rulings():
    _, texts = sheet.title_block(
        sheet.resolve_paper("A3"), {"图名": "图", "图号": "1", "日期": "2", "人": "3", "比例": "4"}
    )
    assert len(texts) == sheet.TITLE_BLOCK_ROWS * 2


def test_the_viewport_leaves_the_title_block_clear():
    """The viewport wraps the title block in an L: the frame's full width, above the block."""
    paper = sheet.resolve_paper("A3")
    left, bottom, right, top = sheet.viewport_rect(paper)
    assert left > paper.binding and top < paper.height - paper.margin
    block_x, block_y = paper.title_block_origin
    assert right > block_x, "the strip above the block runs the frame's full width"
    assert bottom >= block_y + sheet.TITLE_BLOCK_HEIGHT, "nothing runs under the title block"


def test_a_portrait_a4_still_has_a_viewport_of_positive_size():
    """The measured bug: placed left of a 180 mm title block, 210 mm of paper leaves -15 mm."""
    paper = sheet.resolve_paper("A4", "portrait")
    left, bottom, right, top = sheet.viewport_rect(paper)
    assert right > left and top > bottom


def test_the_view_height_is_the_viewport_span_times_the_scale_denominator():
    """1:100 means one paper millimetre is 100 model millimetres."""
    paper = sheet.resolve_paper("A3")
    _left, bottom, _right, top = sheet.viewport_rect(paper)
    assert sheet.view_height_model(paper, (1, 100)) == pytest.approx((top - bottom) * 100)
    assert top - bottom == pytest.approx(221.0), (
        "297 less two 5 mm margins, the 56 mm title block and two 5 mm gaps"
    )
    assert sheet.view_height_model(paper, (1, 100)) == pytest.approx(22100.0)


def test_a_scale_label_resolves_and_a_nonsense_one_is_refused():
    assert sheet.resolve_scale("1:100") == (1, 100)
    assert sheet.resolve_scale(" 2:1 ") == (2, 1)
    with pytest.raises(ValueError, match="ratio"):
        sheet.resolve_scale("1 to 100")
    with pytest.raises(ValueError, match="positive"):
        sheet.resolve_scale("1:0")


def test_every_scale_this_package_offers_resolves():
    for label, ratio in sheet.SCALES.items():
        assert sheet.resolve_scale(label) == ratio


def test_millimetres_become_points_the_way_a_pdf_counts_them():
    assert sheet.mm_to_pt(25.4) == pytest.approx(72.0)
    assert sheet.mm_to_pt(420.0) == pytest.approx(1190.5511811024, abs=1e-6)


def test_text_width_is_the_one_the_rest_of_the_package_measures_with():
    """One measured constant, not a second copy of it in this module."""
    from acadmcp import layers

    assert sheet.text_width("生产车间", 350.0) == layers.text_width("生产车间", 350.0)
    assert sheet.CJK_ADVANCE == layers.CJK_ADVANCE


def test_a_pdf_is_measured_not_assumed(tmp_path):
    """The whole point of the read-back: the file is asked, not our own arithmetic."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def write(path, width_mm, height_mm):
        fig = plt.figure(figsize=(sheet.mm_to_pt(width_mm) / 72, sheet.mm_to_pt(height_mm) / 72))
        fig.add_axes((0.0, 0.0, 1.0, 1.0)).set_axis_off()
        fig.savefig(path)
        plt.close(fig)

    a3 = sheet.resolve_paper("A3")
    right = tmp_path / "a3.pdf"
    write(right, a3.width, a3.height)
    assert sheet.read_pdf_mediabox_mm(right) == pytest.approx((420.0, 297.0), abs=0.05)

    wrong = tmp_path / "a4.pdf"
    write(wrong, 297.0, 210.0)
    assert sheet.read_pdf_mediabox_mm(wrong) != pytest.approx((420.0, 297.0), abs=0.05)


def test_a_file_with_no_page_box_is_refused_rather_than_guessed(tmp_path):
    broken = tmp_path / "not-a.pdf"
    broken.write_bytes(b"%PDF-1.4\nnothing to see here\n%%EOF\n")
    with pytest.raises(ValueError, match="MediaBox"):
        sheet.read_mediabox(broken)


def test_a_plan_is_measured_against_what_the_viewport_shows():
    paper = sheet.resolve_paper("A3")
    assert sheet.model_window(paper, (1, 100)) == pytest.approx((38000.0, 22100.0))
    assert sheet.fits_on_sheet(paper, (1, 100), (0.0, 0.0, 30000.0, 8000.0)) is True
    assert sheet.fits_on_sheet(paper, (1, 100), (0.0, 0.0, 40000.0, 8000.0)) is False
    assert sheet.fits_on_sheet(paper, (1, 200), (0.0, 0.0, 40000.0, 8000.0)) is True


def test_the_largest_scale_that_fits_is_the_most_detailed_one_that_holds_the_plan():
    paper = sheet.resolve_paper("A3")
    assert sheet.largest_scale_that_fits(paper, (0.0, 0.0, 30000.0, 0.0)) == "1:100"
    assert sheet.largest_scale_that_fits(paper, (0.0, 0.0, 12000.0, 8000.0)) == "1:50"
    assert sheet.largest_scale_that_fits(paper, (0.0, 0.0, 3000.0, 2000.0)) == "1:10"
    assert sheet.largest_scale_that_fits(paper, (0.0, 0.0, 80000.0, 0.0)) == "1:250"
    assert sheet.largest_scale_that_fits(paper, (0.0, 0.0, 500000.0, 0.0)) == "1:1000"
