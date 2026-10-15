"""The journal is pure bookkeeping; the backends own erasing and redrawing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp.journal import Journal  # noqa: E402


def _filled(n=3):
    j = Journal()
    for i in range(n):
        j.append(f"op{i}", f"label{i}", {"i": i}, [{"kind": "line"}], [f"H{i}"])
    return j


def test_append_numbers_steps_in_order():
    j = _filled()
    assert [s.index for s in j.steps] == [1, 2, 3]
    assert j.steps[1].handles == ["H1"]


def test_undo_marks_newest_applied_first():
    j = _filled()
    doomed = j.undo(2)
    assert [s.op for s in doomed] == ["op2", "op1"]
    assert [s.state for s in j.steps] == ["applied", "undone", "undone"]


def test_redo_reapplies_oldest_undone_first():
    j = _filled()
    j.undo(2)
    back = j.redo(1)
    assert [s.op for s in back] == ["op1"]
    assert j.steps[2].state == "undone"


def test_a_new_step_drops_the_undone_tail():
    j = _filled()
    j.undo(2)
    j.append("opX", "", {}, [], ["HX"])
    assert [s.op for s in j.steps] == ["op0", "opX"]
    assert [s.index for s in j.steps] == [1, 2]


def test_rollback_to_undoes_everything_after_index():
    j = _filled(4)
    doomed = j.rollback_to(2)
    assert [s.op for s in doomed] == ["op3", "op2"]
    assert [s.state for s in j.steps] == ["applied", "applied", "undone", "undone"]


def test_rollback_to_zero_wipes_everything():
    j = _filled()
    doomed = j.rollback_to(0)
    assert len(doomed) == 3
    assert all(s.state == "undone" for s in j.steps)


def test_round_trip_through_dict():
    j = _filled()
    j.undo(1)
    clone = Journal.from_dict(j.to_dict())
    assert [(s.op, s.state, s.handles) for s in clone.steps] == [
        (s.op, s.state, s.handles) for s in j.steps
    ]


def test_from_dict_rejects_garbage():
    with pytest.raises(ValueError):
        Journal.from_dict({"steps": [{"no_op": True}]})


def test_describe_is_one_line_per_step():
    j = _filled(2)
    j.undo(1)
    text = j.describe()
    assert "op0" in text and "undone" in text and len(text.splitlines()) == 2


# --- meta: facts about the drawing, not about a step ------------------------


def test_meta_round_trips():
    j = Journal()
    j.note_meta(catalog={"digest": "abc"}, units="mm")
    clone = Journal.from_dict(j.to_dict())
    assert clone.meta["catalog"] == {"digest": "abc"} and clone.meta["units"] == "mm"


def test_note_meta_drops_nothing_values():
    j = Journal()
    j.note_meta(catalog=None, units="mm")
    assert "catalog" not in j.meta and j.meta["units"] == "mm"


def test_a_payload_without_meta_still_loads():
    """Every drawing saved before this change has no meta section."""
    j = Journal.from_dict({"version": 1, "steps": []})
    assert j.meta == {} and j.steps == []


def test_a_newer_journal_version_is_refused_by_number():
    with pytest.raises(ValueError) as caught:
        Journal.from_dict({"version": 2, "steps": []})
    assert "2" in str(caught.value) and "1" in str(caught.value)


def test_meta_is_a_copy_and_an_empty_one_is_not_written():
    """Two rules with one reason: the payload of a drawing with no meta must be exactly
    what it was before meta existed, and the books must not be writable through the
    accessor."""
    j = Journal()
    assert j.to_dict() == {"version": 1, "steps": []}
    j.note_meta(catalog={"digest": "abc"})
    j.meta["catalog"] = "tampered"
    assert j.meta["catalog"] == {"digest": "abc"}
    assert j.to_dict()["meta"] == {"catalog": {"digest": "abc"}}
