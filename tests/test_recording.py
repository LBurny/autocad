"""RecordingBackend, exercised through an in-memory fake — no CAD anywhere."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp.backends.base import BackendError  # noqa: E402
from acadmcp.backends.recording import RecordingBackend, unflatten_points  # noqa: E402


class ListBackend(RecordingBackend):
    """Applies primitives by appending them to a list; handles are positions."""

    def __init__(self):
        super().__init__()
        self.drawn: list[dict] = []

    def _apply(self, primitives):
        for prim in primitives:
            self.drawn.append(prim)
        return [f"H{len(self.drawn) - len(primitives) + i}" for i in range(len(primitives))]

    def _erase(self, handles):
        erased = missing = 0
        for h in handles:
            index = int(h[1:])
            if 0 <= index < len(self.drawn) and self.drawn[index] is not None:
                self.drawn[index] = None
                erased += 1
            else:
                missing += 1
        return erased, missing


def test_recording_outside_a_step_raises():
    be = ListBackend()
    with pytest.raises(BackendError):
        be._record("line", [(0, 0), (1, 1)])


def test_commit_draws_and_journals_the_handles():
    be = ListBackend()
    with be.step("draw_line", params={"a": 1}):
        be._record("line", [(0, 0), (1, 1)], layer="A-WALL")
    assert be.drawn == [{"kind": "line", "points": [0.0, 0.0, 1.0, 1.0], "layer": "A-WALL"}]
    step = be.journal.steps[0]
    assert step.op == "draw_line" and step.handles == ["H0"] and step.params == {"a": 1}


def test_none_values_are_not_recorded():
    be = ListBackend()
    with be.step("x"):
        be._record("line", [(0, 0), (1, 1)], linetype=None)
    assert "linetype" not in be.drawn[0]


def test_an_exception_inside_step_leaves_nothing_behind():
    be = ListBackend()
    with pytest.raises(RuntimeError), be.step("boom"):
        be._record("line", [(0, 0), (1, 1)])
        raise RuntimeError("halfway")
    assert be.drawn == [] and be.journal.steps == [] and be.open_step == ""


def test_undo_erases_and_redo_redraws():
    be = ListBackend()
    with be.step("a"):
        be._record("line", [(0, 0), (1, 0)])
    with be.step("b"):
        be._record("circle", [(5, 5)], radius=10.0)
    report = be.undo(1)
    assert report["erased"] == 1 and be.drawn[1] is None
    report = be.redo(1)
    assert report["redrawn"] == 1 and be.drawn[-1] == {"kind": "circle", "points": [5.0, 5.0], "radius": 10.0}


def test_steps_do_not_nest():
    be = ListBackend()
    be.begin_step("outer")
    with pytest.raises(BackendError):
        be.begin_step("inner")


def test_replay_step_appends_a_foreign_record():
    source = ListBackend()
    with source.step("draw_room", label="room", params={"w": 4}):
        source._record("line", [(0, 0), (1, 0)])
    target = ListBackend()
    record = target.replay_step(source.journal.steps[0])
    assert record.op == "draw_room" and target.drawn and record.index == 1


def test_unflatten_round_trips():
    assert unflatten_points([1.0, 2.0, 3.0, 4.0]) == [(1.0, 2.0), (3.0, 4.0)]


def test_a_failed_commit_does_not_wedge_the_backend():
    class FailingBackend(ListBackend):
        fails = 1

        def _apply(self, primitives):
            if self.fails:
                self.fails -= 1
                raise BackendError("cannot apply")
            return super()._apply(primitives)

    be = FailingBackend()
    with pytest.raises(BackendError), be.step("place"):
        be._record("insert", [(0, 0)], block="X")
    assert be.open_step == "" and be.pending == []
    with be.step("recovered"):
        be._record("line", [(0, 0), (1, 1)])
