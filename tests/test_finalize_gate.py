"""The critique, finalize and capture tools, wired onto the server surface.

The checks themselves live in tests/test_critique.py and the capture machinery in
tests/test_capture.py; this file pins the tool wiring: the tools answer through the
registry's own functions, the finalize gate refuses what is wrong, and a captured
element resolves again after a save and reopen.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog, server  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_session():
    catalog.reset()
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None
    catalog.reset()


def test_critique_tool_runs_end_to_end():
    server.draw_wall_with_openings(
        [[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
        openings=[{"start": 1000, "width": 900, "kind": "door", "tag": "M0921"}],
        closed=True,
    )
    server.draw_door([1000, 0], rotation=0, width=900)
    out = server.drawing_critique()
    assert out["ok"] and out["counts"]["error"] == 0


def test_critique_reports_a_floating_door_as_an_error():
    server.draw_wall([[0, 0], [4000, 0]])
    server.draw_door([0, 5000], rotation=0, width=900)
    out = server.drawing_critique()
    assert out["counts"]["error"] == 1
    assert out["issues"][0]["code"] == "door_without_opening"


def test_critique_refuses_an_unknown_focus_with_a_readable_error():
    with pytest.raises(ValueError, match="bogus"):
        server.drawing_critique(focus="bogus")


def test_finalize_refuses_a_drawing_with_a_floating_door():
    server.draw_wall([[0, 0], [4000, 0]])
    server.draw_door([0, 5000], rotation=0, width=900)
    with pytest.raises(BackendError, match="not finished"):
        server.drawing_finalize()


def test_finalize_passes_a_clean_drawing():
    server.draw_wall_with_openings(
        [[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
        openings=[{"start": 1000, "width": 900, "kind": "door", "tag": "M0921"}],
        closed=True,
    )
    out = server.drawing_finalize()
    assert out["ok"] and out["verdict"] == "matches"
    assert out["warnings"] == 0 and out["rooms"] == 0


def test_element_capture_tool_and_place_after_reopen(tmp_path):
    server.draw_line([0, 0], [1200, 0])
    server.draw_line([1200, 0], [1200, 300])
    indices = [s["index"] for s in server.drawing_history()["steps"]]
    out = server.element_capture(indices, key="rail", label="护栏")
    assert out["ok"] and out["block"] == "ACADMCP_RAIL"
    placed = server.place_element("rail", [5000, 5000], tag=False)
    assert placed["placed"] == 1
    target = str(tmp_path / "plan.dxf")
    server.drawing_save(target)
    server.drawing_open(target)
    # The catalogue travels inside the drawing: the captured key still resolves.
    entries = [
        e
        for group in server.element_types(check_library=False)["categories"].values()
        for e in group
    ]
    assert any(e["key"] == "rail" for e in entries)
    assert server.place_element("rail", [9000, 5000], tag=False)["placed"] == 1
