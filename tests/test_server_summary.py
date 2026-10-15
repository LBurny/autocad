"""The summary tool, and the surface that tells an agent where an element can go.

`drawing_summary` is the agent's semantic read-back: it answers "how many doors, what
sizes, what is the floor area" from the journal, with no screenshot, and it survives a
reopen because the journal persists inside the DXF.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog, server  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_session():
    catalog.reset()
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None
    catalog.reset()


def test_summary_reports_the_rooms_that_were_drawn():
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    out = server.drawing_summary()
    assert out["ok"] and out["rooms"][0]["name"] == "会议室"
    assert out["rooms"][0]["area_m2"] == pytest.approx(3.8 * 2.8, abs=0.01)


def test_summary_survives_save_and_reopen(tmp_path):
    """The reason this is derived from the journal rather than from entities."""
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    target = str(tmp_path / "plan.dxf")
    server.drawing_save(target)
    server.drawing_open(target)
    assert server.drawing_summary()["rooms"][0]["name"] == "会议室"


def test_summary_says_which_catalogue_drew_it(monkeypatch):
    server.draw_line([0, 0], [1000, 0])
    out = server.drawing_summary()
    assert out["catalog"] is not None and "digest" in out["catalog"]


def test_element_types_says_where_an_element_can_be_placed(monkeypatch):
    out = server.element_types(check_library=False)
    door = out["categories"]["opening"][0]
    assert door["placement"] in ("block", "parametric", "both")
