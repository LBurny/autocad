"""The drawing reconciled against what the journal says it drew."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server, verify  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


def _row(index, op, entities, state="applied"):
    return {
        "index": index,
        "op": op,
        "label": "",
        "params": {},
        "state": state,
        "entities": entities,
    }


def test_an_empty_drawing_with_no_steps_matches():
    report = verify.reconcile([], [])
    assert report["verdict"] == "matches" and report["ok"] is True
    assert report["expected_entities"] == 0 and report["actual_entities"] == 0


def test_a_drawing_holding_exactly_what_was_drawn_matches():
    rows = [_row(1, "draw_wall_with_openings", 8), _row(2, "draw_room", 4)]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 12)
    assert report["verdict"] == "matches"
    assert report["difference"] == 0
    assert report["by_op"] == {"draw_wall_with_openings": 8, "draw_room": 4}


def test_more_entities_than_recorded_are_reported_as_unaccounted():
    report = verify.reconcile([_row(1, "draw_line", 2)], [{"type": "LINE"}] * 5)
    assert report["verdict"] == "unaccounted" and report["ok"] is False
    assert report["difference"] == 3


def test_fewer_entities_than_recorded_are_reported_as_missing():
    """A save that dropped entities, or an erase that went too far."""
    report = verify.reconcile([_row(1, "draw_room", 4)], [{"type": "TEXT"}])
    assert report["verdict"] == "missing" and report["ok"] is False
    assert report["difference"] == -3


def test_undone_steps_are_excluded_from_the_expectation_but_still_reported():
    rows = [_row(1, "draw_line", 2), _row(2, "draw_room", 4, state="undone")]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 2)
    assert report["verdict"] == "matches"
    assert report["steps"] == {"applied": 1, "undone": 1}
    assert report["undone_entities"] == 4


def test_a_truncated_walk_says_so_rather_than_calling_it_a_mismatch():
    """A cut-short listing is a floor, not a count, so the comparison means nothing."""
    report = verify.reconcile([_row(1, "draw_room", 4)], [{"type": "TEXT"}] * 200, truncated=True)
    assert report["verdict"] == "walk_truncated" and report["ok"] is None


def test_a_drawing_with_entities_but_no_steps_has_no_baseline_to_check_against():
    """A file opened from elsewhere holds entities no step ever recorded.

    Reporting that as `unaccounted` with `ok: false` cried wolf on the one case a read-back
    is most wanted, and would stop a caller's automated loop on a perfectly good drawing.
    """
    report = verify.reconcile([], [{"type": "LINE"}] * 247)
    assert report["verdict"] == "no_baseline" and report["ok"] is None
    assert report["difference"] == 247
    assert report["warnings"] and "no steps" in report["warnings"][0]


def test_a_positive_difference_names_the_likely_cause():
    report = verify.reconcile([_row(1, "draw_line", 2)], [{"type": "LINE"}] * 5)
    assert report["verdict"] == "unaccounted"
    assert any("drawing_open" in w for w in report["warnings"])


def test_an_unreadable_row_is_counted_and_warned_about_never_raised():
    rows = [_row(1, "draw_line", 2), {"index": 2, "op": "draw_room", "state": "applied"}]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 2)
    assert report["verdict"] == "matches"
    assert len(report["warnings"]) == 1 and "step 2" in report["warnings"][0]


def test_a_row_that_is_not_a_row_at_all_is_warned_about_too():
    report = verify.reconcile(["nonsense"], [])
    assert report["verdict"] == "matches" and len(report["warnings"]) == 1


def test_steps_that_drew_nothing_are_listed_informationally():
    """`layer_create` and friends legitimately record no entities."""
    rows = [_row(1, "layer_create", 0), _row(2, "draw_line", 2)]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 2)
    assert report["verdict"] == "matches" and report["empty_steps"] == [1]


def test_the_drawings_own_count_is_reported_beside_the_walk_never_reconciled():
    """On com the two can differ, so both are reported and neither is adjusted."""
    report = verify.reconcile([_row(1, "draw_line", 2)], [{"type": "LINE"}] * 2, reported=9)
    assert report["verdict"] == "matches" and report["reported_entities"] == 9


@pytest.fixture()
def fresh_dxf():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_the_record_and_the_drawing_agree_through_save_reopen_undo_and_redo(fresh_dxf, tmp_path):
    """The invariant `drawing_verify` rests on, pinned on all four paths."""
    server.draw_wall_with_openings(
        points=[[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
        closed=True,
        openings=[{"start": 1000, "width": 900, "kind": "door"}],
        thickness=200,
    )
    server.draw_room(boundary=[[0, 0], [6000, 0], [6000, 4000], [0, 4000]], name="A", thickness=200)
    server.place_element(type="door", at=[1000, 0], variant="900", tag=True)

    first = server.drawing_verify()
    assert first["verdict"] == "matches" and first["expected_entities"] == 15
    assert first["backend"] == "dxf"

    target = str(tmp_path / "verified.dxf")
    server.drawing_save(target)
    assert server.drawing_verify()["verdict"] == "matches"

    server.drawing_open(target)
    reopened = server.drawing_verify()
    assert reopened["verdict"] == "matches" and reopened["expected_entities"] == 15

    server.drawing_undo()
    after_undo = server.drawing_verify()
    assert after_undo["verdict"] == "matches" and after_undo["expected_entities"] == 10
    assert after_undo["undone_entities"] == 5

    server.drawing_redo()
    assert server.drawing_verify()["expected_entities"] == 15


def test_a_missing_entity_is_reported_rather_than_passed_over(fresh_dxf):
    """Erase an entity behind the journal's back: the books stop agreeing."""
    server.draw_line([0, 0], [1000, 0])
    server.draw_line([0, 0], [0, 1000])
    be = server._state.backend
    be.msp.delete_entity(next(iter(be.msp)))

    report = server.drawing_verify()
    assert report["verdict"] == "missing" and report["difference"] == -1


def test_drawing_verify_asks_for_a_second_look_when_the_walk_was_cut_short(fresh_dxf):
    server.draw_line([0, 0], [1000, 0])
    server.draw_line([0, 0], [0, 1000])
    report = server.drawing_verify(limit=1)
    assert report["verdict"] == "walk_truncated" and report["ok"] is None
    assert report["actual_entities"] == 2, "the count is a floor and says so"


def test_a_limit_equal_to_the_entity_count_is_not_reported_as_a_truncation(fresh_dxf):
    """A caller's natural move — read the count from drawing_status, pass it as `limit`.

    Asking for exactly as many rows as there are used to come back "walk_truncated", because
    a walk that ends at the limit and one cut short by it are the same length.
    """
    for i in range(5):
        server.draw_line([i * 100, 0], [i * 100, 500])

    report = server.drawing_verify(limit=5)
    assert report["verdict"] == "matches" and report["truncated"] is False
    assert report["actual_entities"] == 5
    assert server.drawing_verify(limit=4)["verdict"] == "walk_truncated"


def test_an_opened_drawing_with_no_steps_is_no_baseline_not_unaccounted(fresh_dxf, tmp_path):
    """The read-back's most-wanted case: a drawing that came from somewhere else.

    Written with plain ezdxf rather than through the tools, because that is what a drawing
    from another program actually looks like: entities and no journal at all.
    """
    import ezdxf

    target = str(tmp_path / "foreign.dxf")
    doc = ezdxf.new("R2010")
    for i in range(7):
        doc.modelspace().add_line((i * 1000, 0), (i * 1000, 500))
    doc.saveas(target)

    server.drawing_open(target)
    report = server.drawing_verify()
    assert report["verdict"] == "no_baseline" and report["ok"] is None
    assert report["expected_entities"] == 0 and report["actual_entities"] == 7
    assert report["warnings"], "a drawing with nothing to check against must say so"


def test_drawing_verify_refuses_a_limit_that_walks_nothing(fresh_dxf):
    with pytest.raises(ValueError, match="limit"):
        server.drawing_verify(limit=0)
