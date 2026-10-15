"""The sheet as it lands in the file: the page setup, read back out of a saved DXF."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import sheet  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


def _layout(path, name="A3"):
    import ezdxf

    return ezdxf.readfile(path).layouts.get(name)


def test_a_new_dxf_has_no_page_setup_at_all():
    """The gap this closes, pinned so it cannot come back unnoticed."""
    layout = make_backend("dxf").doc.layouts.get("Layout1")
    assert layout.dxf.get("paper_width") is None
    assert layout.dxf.get("unit_factor") is None


def test_the_page_setup_reaches_the_saved_file(tmp_path):
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 100))
    target = str(tmp_path / "sheet.dxf")
    be.save(target)

    layout = _layout(target)
    assert layout.dxf.paper_width == pytest.approx(420.0)
    assert layout.dxf.paper_height == pytest.approx(297.0)
    assert layout.dxf.paper_size == "A3"


def test_the_scale_is_written_where_a_plot_reads_it(tmp_path):
    """75, 147 and the ratio must agree, or the sheet plots at a scale it does not claim."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 200))
    target = str(tmp_path / "scale.dxf")
    be.save(target)

    layout = _layout(target)
    assert (layout.dxf.scale_numerator, layout.dxf.scale_denominator) == (1, 200)
    assert layout.dxf.unit_factor == pytest.approx(200.0)
    assert layout.dxf.standard_scale_type == 16, "16 is the DXF code for 1:200"


def test_a_scale_with_no_enum_row_does_not_get_an_invented_one():
    """The measured failure mode: group 75 and group 147 disagreeing about the scale."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 123))
    layout = be._sheet_layout
    assert layout.dxf.unit_factor == pytest.approx(123.0)
    assert layout.dxf.get("standard_scale_type") in (None, 0)


def test_the_documents_own_paper_layout_is_reused_and_named(tmp_path):
    """One sheet, not a stray Layout1 beside it — two layouts is how a reader plots the wrong."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A2"), (1, 200))
    target = str(tmp_path / "named.dxf")
    be.save(target)
    import ezdxf

    names = [layout.name for layout in ezdxf.readfile(target).layouts]
    assert names == ["Model", "A2"]


def test_the_margins_follow_the_frame():
    be = make_backend("dxf")
    paper = sheet.resolve_paper("A3")
    be.set_page_setup(paper, (1, 100))
    layout = be._sheet_layout
    assert layout.dxf.left_margin == paper.binding
    assert layout.dxf.bottom_margin == paper.margin
    assert layout.dxf.plot_paper_units == 1, "1 is millimetres"


def test_the_style_sheet_name_is_carried_when_given():
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 100), style_sheet="monochrome.ctb")
    assert be._sheet_layout.dxf.current_style_sheet == "monochrome.ctb"
    assert be.sheet["style_sheet"] == "monochrome.ctb"


def test_the_sheet_the_backend_reports_is_what_a_caller_needs():
    be = make_backend("dxf")
    report = be.set_page_setup(sheet.resolve_paper("A3", "portrait"), (1, 100))
    assert report["paper"] == "A3" and report["orientation"] == "portrait"
    assert report["scale"] == "1:100" and report["layout"] == "A3"
    assert report["width"] == 297.0 and report["height"] == 420.0


def test_a_reopened_drawing_still_knows_its_sheet(tmp_path):
    """The sheet travels in the file, or a second session plots a page it cannot describe."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3", "portrait"), (1, 100), style_sheet="mono.ctb")
    target = str(tmp_path / "reopen.dxf")
    be.save(target)

    again = make_backend("dxf", path=target)
    assert again.sheet["paper"] == "A3"
    assert again.sheet["orientation"] == "portrait"
    assert again.sheet["scale"] == "1:100"
    assert again.sheet["style_sheet"] == "mono.ctb"
    assert again._sheet_layout is not None


def test_a_drawing_saved_without_a_sheet_reports_none():
    be = make_backend("dxf")
    assert be.sheet == {} and be._sheet_layout is None


def test_the_live_drivers_declare_they_cannot_make_a_sheet():
    """Declared, not probed, for the same reason `supports_insert` is."""
    from acadmcp.backends.com import ComBackend
    from acadmcp.backends.plugin import PluginBackend

    assert ComBackend.supports_sheet is False
    assert PluginBackend.supports_sheet is False


def test_a_driver_without_paperspace_refuses_by_name():
    """The shared refusal, exercised on the base every driver without a sheet inherits."""
    from acadmcp.backends.recording import RecordingBackend

    paper = sheet.resolve_paper("A3")
    be = RecordingBackend()
    with pytest.raises(BackendError, match="paperspace"):
        be.set_page_setup(paper, (1, 100))
    with pytest.raises(BackendError, match="paperspace"):
        be.paper_primitives([])
    with pytest.raises(BackendError, match="paperspace"):
        be.plot_pdf("x.pdf", paper=paper, scale=(1, 100))


# ---------------------------------------------------------------------------
# the sheet as drawing content, through the tool
# ---------------------------------------------------------------------------

from acadmcp import server  # noqa: E402


def _sheet_entities(fields: int) -> int:
    """What one sheet step draws: the frame's 4 sides, the title block's 8 rulings, a name and
    a value for each field (the tool always adds 比例), and one viewport."""
    return 4 + 8 + fields * 2 + 1


def _tool(name: str):
    """The tool as the wire sees it: the wrapped `fn`, which is what turns a ValueError into a
    ToolError. Calling `server.drawing_sheet` directly would let the raw ValueError out."""
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}[name].fn


@pytest.fixture()
def session():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_the_frame_and_title_block_land_in_paperspace_not_in_the_plan(session, tmp_path):
    """A frame drawn into the model would scale with the plan; on the layout it is paper."""
    _tool("draw_line")([0, 0], [6000, 0])
    target = str(tmp_path / "paper.dxf")
    _tool("drawing_sheet")(paper="A3", scale="1:100", title="生产车间平面图", number="JS-01")
    _tool("drawing_save")(target)

    import ezdxf

    doc = ezdxf.readfile(target)
    layout = doc.layouts.get("A3")
    drawn = {e.dxf.text for e in layout if e.dxftype() == "TEXT"}
    assert drawn >= {"图名", "生产车间平面图", "图号", "JS-01", "比例", "1:100"}, (
        "the caller's fields, and the scale the sheet always states"
    )
    assert len(list(doc.modelspace())) == 1, "the plan itself is untouched"
    assert not [
        e
        for e in doc.modelspace()
        if e.dxftype() == "TEXT" and "图名" in str(e.dxf.get("text", ""))
    ], "nothing of the sheet may land in the model"


def test_the_sheet_draws_the_frame_the_title_block_and_one_viewport(session):
    report = _tool("drawing_sheet")(paper="A3", scale="1:100", title="图", number="1")
    # The frame's four sides; the title block's box, its three row rulings and its column
    # split; and 图名/图号 plus the 比例 the tool always adds, a name and a value each.
    assert report["lines"] == 4 + 8
    assert report["texts"] == 6
    assert report["viewports"] == 1
    assert report["recorded"] == _sheet_entities(3)
    assert report["title_block"] == ["图名", "图号", "比例"]


def test_the_layout_holds_one_viewport_framed_on_the_plan(session, tmp_path):
    _tool("draw_line")([0, 0], [30000, 0])
    _tool("draw_line")([0, 20000], [30000, 20000])
    target = str(tmp_path / "vp.dxf")
    _tool("drawing_sheet")(paper="A3", scale="1:100")
    _tool("drawing_save")(target)

    import ezdxf

    viewports = [e for e in ezdxf.readfile(target).layouts.get("A3") if e.dxftype() == "VIEWPORT"]
    assert len(viewports) == 1
    assert viewports[0].dxf.view_height == pytest.approx(22100.0), "221 mm at 1:100"
    centre = viewports[0].dxf.view_center_point
    # A Vec3, which does not slice: read the axes, not the first two items.
    assert (centre.x, centre.y) == pytest.approx((15000.0, 10000.0))


def test_a_plan_away_from_the_origin_is_still_centred_on_its_sheet(session, tmp_path):
    """Plans are drawn where the building is, not at 0,0."""
    _tool("draw_line")([400000, 300000], [430000, 300000])
    target = str(tmp_path / "far.dxf")
    _tool("drawing_sheet")(paper="A3", scale="1:100")
    _tool("drawing_save")(target)
    import ezdxf

    viewport = next(
        e for e in ezdxf.readfile(target).layouts.get("A3") if e.dxftype() == "VIEWPORT"
    )
    centre = viewport.dxf.view_center_point
    assert (centre.x, centre.y) == pytest.approx((415000.0, 300000.0))


def test_an_empty_drawing_frames_the_origin_rather_than_failing(session):
    report = _tool("drawing_sheet")(paper="A3", scale="1:100")
    assert report["ok"] is True and report["viewports"] == 1


def test_the_sheet_is_one_undoable_step_that_takes_its_own_entities_back(session):
    """One tool call is one step, the rule every drawing tool follows — paperspace included."""
    _tool("draw_line")([0, 0], [6000, 0])
    before = _tool("drawing_verify")()
    assert before["verdict"] == "matches"

    _tool("drawing_sheet")(paper="A3", scale="1:100", title="图")
    after = _tool("drawing_verify")()
    assert after["verdict"] == "matches", "the paperspace frame counts as part of the drawing"
    assert after["expected_entities"] == before["expected_entities"] + _sheet_entities(2)

    rows = _tool("drawing_history")()["steps"]
    assert rows[-1]["op"] == "drawing_sheet"
    assert rows[-1]["entities"] == _sheet_entities(2)

    _tool("drawing_undo")()
    back = _tool("drawing_verify")()
    assert back["verdict"] == "matches"
    assert back["expected_entities"] == before["expected_entities"]
    assert back["actual_entities"] == before["actual_entities"]


def test_a_redo_of_the_sheet_draws_it_again(session):
    _tool("drawing_sheet")(paper="A3", scale="1:100")
    _tool("drawing_undo")()
    _tool("drawing_redo")()
    report = _tool("drawing_verify")()
    assert report["verdict"] == "matches"
    assert report["actual_entities"] == _sheet_entities(1)


def test_the_sheet_survives_a_save_and_reopen_as_drawing_content(session, tmp_path):
    """Reopened, the layout still carries the frame — the handles are journaled, not guessed."""
    target = str(tmp_path / "roundtrip.dxf")
    _tool("drawing_sheet")(paper="A3", scale="1:100", title="图")
    _tool("drawing_save")(target)

    reopened = _tool("drawing_open")(target)
    assert reopened["ok"] is True and reopened["sheet"]["paper"] == "A3"
    assert reopened["entities"] == _sheet_entities(2)
    status = _tool("drawing_status")()
    assert status["sheet"]["scale"] == "1:100"
    assert _tool("drawing_verify")()["verdict"] == "matches"

    _tool("drawing_undo")()
    assert _tool("drawing_verify")()["actual_entities"] == 0, "an undone sheet erases its frame"


def test_the_sheet_is_reported_by_drawing_status(session):
    _tool("drawing_sheet")(paper="A3", scale="1:100", style_sheet="mono.ctb")
    status = _tool("drawing_status")()
    assert status["sheet"]["paper"] == "A3" and status["sheet"]["scale"] == "1:100"
    assert status["sheet"]["style_sheet"] == "mono.ctb"


def test_a_paper_or_scale_this_package_lacks_is_refused_by_name(session):
    with pytest.raises(BackendError, match="A9"):
        _tool("drawing_sheet")(paper="A9")
    with pytest.raises(BackendError, match="ratio"):
        _tool("drawing_sheet")(scale="1 to 100")
    with pytest.raises(BackendError, match="orientation"):
        _tool("drawing_sheet")(orientation="sideways")


def test_the_sheet_tool_refuses_on_a_backend_without_paperspace():
    """The refusal a client sees, exercised without a live AutoCAD."""

    class _NoSheet:
        name = "plugin"
        supports_sheet = False

    server._state.reset(_NoSheet(), "plugin")
    try:
        with pytest.raises(BackendError, match="paperspace"):
            _tool("drawing_sheet")(paper="A3", scale="1:100")
    finally:
        server._state.backend = None


def test_a_plan_too_big_for_the_page_is_reported_not_silently_cut(session):
    """At 1:100 an A3 shows 38 x 22.1 m; a 40 m bay does not fit and the reply must say so."""
    _tool("draw_line")([0, 0], [40000, 0])
    report = _tool("drawing_sheet")(paper="A3", scale="1:100")
    assert report["fits"] is False
    assert report["plan_mm"] == [40000.0, 0.0]
    assert "1:150" in report["note"], "and it names the largest scale that does hold it"


def test_a_plan_that_fits_says_so_and_carries_no_note(session):
    _tool("draw_line")([0, 0], [12000, 0])
    _tool("draw_line")([0, 8000], [12000, 8000])
    report = _tool("drawing_sheet")(paper="A3", scale="1:100")
    assert report["fits"] is True and "note" not in report
    assert report["plan_mm"] == [12000.0, 8000.0]


def test_a_sheet_on_an_empty_drawing_reports_no_plan_rather_than_a_zero_one(session):
    report = _tool("drawing_sheet")(paper="A3", scale="1:100")
    assert "fits" not in report and "plan_mm" not in report


def test_a_second_sheet_is_refused_rather_than_drawn_beside_the_first(session):
    """A frame is drawn content, so a second sheet would leave two of everything on the layout."""
    _tool("drawing_sheet")(paper="A3", scale="1:100")
    with pytest.raises(BackendError, match="already carries a sheet"):
        _tool("drawing_sheet")(paper="A3", scale="1:200")
    assert len(_tool("listing_entities")(layer="A-SHEET")["entities"]) == 12

    _tool("drawing_undo")()
    again = _tool("drawing_sheet")(paper="A3", scale="1:200")
    assert again["ok"] is True and again["scale"] == "1:200"
    assert len(_tool("listing_entities")(layer="A-SHEET")["entities"]) == 12
