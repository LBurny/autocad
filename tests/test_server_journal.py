"""Auto-stepping and the journal tools, against the headless dxf backend.

`@mcp.tool()` returns the decorated function unchanged in the installed mcp
version, so these call the tools directly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_session():
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_every_drawing_tool_call_is_one_step():
    server.draw_line([0, 0], [1000, 0], layer="A-WALL")
    server.draw_circle([500, 500], 100)
    steps = server._state.backend.journal.steps
    assert [s.op for s in steps] == ["draw_line", "draw_circle"]
    assert steps[0].params["layer"] == "A-WALL"


def test_drawing_undo_takes_back_only_the_last_call():
    server.draw_line([0, 0], [1000, 0])
    server.draw_circle([500, 500], 100)
    report = server.drawing_undo()
    assert report["ok"] and report["erased"] == 1
    assert server._state.backend.info()["entities"] == 1
    server.drawing_redo()
    assert server._state.backend.info()["entities"] == 2


def test_drawing_history_is_compact_text():
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="测试室")
    out = server.drawing_history()
    assert out["ok"] and "draw_room" in out["rendered"] and "测试室" in out["rendered"]


def test_a_wrong_coordinate_costs_two_calls_not_a_redraw():
    server.draw_wall([[0, 0], [5000, 0]], thickness=200)
    server.draw_wall([[0, 0], [0, 3000]], thickness=200)  # oops, wrong
    server.drawing_undo()
    server.draw_wall([[5000, 0], [5000, 3000]], thickness=200)
    assert [s.op for s in server._state.backend.journal.steps] == [
        "draw_wall",
        "draw_wall",
    ]
    assert server._state.backend.info()["entities"] == 8


def test_replay_redraws_the_journal_on_a_fresh_dxf_backend(tmp_path):
    server.drawing_new(backend="dxf")
    server.draw_line([0, 0], [1000, 0])
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="测试室")
    target = str(tmp_path / "replayed.dxf")
    report = server.drawing_replay(backend="dxf", path=target)
    assert report["ok"] and report["replayed"] == 2
    assert server._state.backend.info()["entities"] > 0
    assert [s.op for s in server._state.backend.journal.steps] == ["draw_line", "draw_room"]


def test_replay_reports_steps_a_backend_cannot_apply(tmp_path):
    server.drawing_new(backend="dxf")
    be = server._state.backend
    # A `with be.step(...)` block would commit on exit and refuse the insert
    # before the replay ever saw it, so the record is journaled by hand: open a
    # step, stage the primitive, append the record, abort the step.
    be.begin_step("place", params={})
    be._pending.append({"kind": "insert", "points": [0.0, 0.0], "block": "X"})
    be.journal.append("place", "", {}, list(be._pending), [])
    be.abort_step()
    report = server.drawing_replay(backend="dxf", path=str(tmp_path / "r.dxf"))
    assert report["ok"] and len(report["failed"]) == 1
    assert report["failed"][0]["op"] == "place" and "insert" in report["failed"][0]["reason"]


def test_rollback_to_leaves_the_named_step_standing():
    server.draw_line([0, 0], [1000, 0])
    server.draw_circle([500, 500], 100)
    server.draw_line([0, 0], [0, 1000])
    report = server.drawing_rollback_to(1)
    assert report["ok"] and [s["index"] for s in report["undone"]] == [3, 2]
    assert server._state.backend.info()["entities"] == 1


def test_rollback_to_zero_empties_the_drawing():
    server.draw_line([0, 0], [1000, 0])
    server.drawing_rollback_to(0)
    assert server._state.backend.info()["entities"] == 0


def test_rollback_to_an_index_past_the_end_changes_nothing():
    server.draw_line([0, 0], [1000, 0])
    report = server.drawing_rollback_to(99)
    assert report["undone"] == [] and server._state.backend.info()["entities"] == 1


def test_history_carries_machine_readable_rows():
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    rows = server.drawing_history()["steps"]
    assert rows[0]["index"] == 1 and rows[0]["op"] == "draw_room"
    assert rows[0]["params"]["name"] == "会议室"
