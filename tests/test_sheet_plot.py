"""The plotted sheet: the paper it says it is, and the plan on it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server, sheet  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


def _tool(name: str):
    """The tool as the wire sees it, so a caller's mistake arrives as a ToolError."""
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}[name].fn


@pytest.fixture()
def session():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_the_pdf_is_the_paper_the_sheet_claims(session, tmp_path):
    _tool("draw_line")([0, 0], [30000, 0])
    _tool("drawing_sheet")(paper="A3", scale="1:100")
    report = _tool("drawing_plot_pdf")(str(tmp_path / "sheet.pdf"))

    assert report["verified"] is True
    assert report["paper_mm"] == pytest.approx([420.0, 297.0], abs=0.05)
    assert report["asked_for_mm"] == pytest.approx([420.0, 297.0], abs=0.05)
    assert report["mismatch_mm"] == pytest.approx(0.0, abs=0.05)


def test_the_measurement_comes_from_the_file_not_from_our_own_number(session, tmp_path):
    """The read-back is the point: the PDF is asked, and an A2 is not an A3."""
    _tool("drawing_sheet")(paper="A2", scale="1:100")
    target = str(tmp_path / "a2.pdf")
    report = _tool("drawing_plot_pdf")(target)

    assert sheet.read_pdf_mediabox_mm(target) == pytest.approx((594.0, 420.0), abs=0.05)
    assert report["paper_mm"] == pytest.approx([594.0, 420.0], abs=0.05)
    assert report["verified"] is True


def test_a_portrait_sheet_plots_portrait(session, tmp_path):
    _tool("drawing_sheet")(paper="A3", scale="1:100", orientation="portrait")
    report = _tool("drawing_plot_pdf")(str(tmp_path / "portrait.pdf"))
    assert report["paper_mm"] == pytest.approx([297.0, 420.0], abs=0.05)


def test_the_plan_is_in_shot_at_the_plot_scale(session, tmp_path):
    """At 1:100 an A3's 221 mm viewport shows 22.1 m of building, and no more."""
    _tool("drawing_sheet")(paper="A3", scale="1:100")
    report = _tool("drawing_plot_pdf")(str(tmp_path / "scale.pdf"))
    assert report["plan_span_mm"][1] == pytest.approx(22100.0)
    assert report["plan_span_mm"][0] == pytest.approx(38000.0), (
        "the width follows from the viewport's proportions, so the plan is not stretched"
    )
    assert report["viewport_mm"] == pytest.approx([380.0, 221.0])


def test_a_half_scale_plot_shows_half_as_much_of_the_model(session, tmp_path):
    _tool("drawing_sheet")(paper="A3", scale="1:200")
    report = _tool("drawing_plot_pdf")(str(tmp_path / "half.pdf"))
    assert report["plan_span_mm"][1] == pytest.approx(44200.0)
    assert report["scale"] == "1:200"


def test_plotting_without_a_sheet_says_what_is_missing(session, tmp_path):
    _tool("draw_line")([0, 0], [6000, 0])
    with pytest.raises(BackendError, match="no sheet"):
        _tool("drawing_plot_pdf")(str(tmp_path / "nosheet.pdf"))


def test_a_reopened_sheetless_drawing_still_refuses_to_plot(session, tmp_path):
    """The phantom consequence: a save/load round trip used to materialise ezdxf's default
    page setup onto Layout1, so a sheetless drawing reopened reported an A3 at 1:1 and this
    call *succeeded* on a frameless drawing. It refuses by name again."""
    _tool("draw_line")([0, 0], [6000, 0])
    target = str(tmp_path / "plain.dxf")
    _tool("drawing_save")(target)
    reopened = _tool("drawing_open")(target)
    assert reopened["sheet"] == {}

    with pytest.raises(BackendError, match="no sheet"):
        _tool("drawing_plot_pdf")(str(tmp_path / "phantom.pdf"))


def test_plotting_refuses_on_a_backend_without_paperspace():
    class _NoSheet:
        name = "plugin"
        supports_sheet = False
        sheet: dict = {}

    server._state.reset(_NoSheet(), "plugin")
    try:
        with pytest.raises(BackendError, match="paperspace"):
            _tool("drawing_plot_pdf")()
    finally:
        server._state.backend = None


def test_plotting_without_a_path_writes_beside_the_temp_dir_not_the_project(session):
    """A default output path must never land in somebody's repository root."""
    import tempfile

    _tool("drawing_sheet")(paper="A4", scale="1:100")
    report = _tool("drawing_plot_pdf")()
    try:
        assert Path(report["path"]).parent == Path(tempfile.gettempdir())
        assert Path(report["path"]).suffix == ".pdf"
    finally:
        Path(report["path"]).unlink(missing_ok=True)
