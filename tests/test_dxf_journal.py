"""The dxf backend journals stepped drawing; eager drawing is unchanged."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


@pytest.fixture()
def be():
    return make_backend("dxf")


def test_eager_drawing_outside_a_step_is_unchanged(be):
    be.line((0, 0), (1000, 0))
    assert be.info()["entities"] == 1
    assert be.journal.steps == []


def test_a_step_draws_at_commit_and_journals_handles(be):
    with be.step("draw_line", params={"p1": [0, 0]}):
        be.line((0, 0), (1000, 0), layer="A-WALL")
        assert be.info()["entities"] == 0  # nothing drawn until commit
    assert be.info()["entities"] == 1
    step = be.journal.steps[0]
    assert step.op == "draw_line" and len(step.handles) == 1


def test_undo_removes_exactly_the_steps_entities(be):
    be.line((0, 0), (1, 0))  # eager, not journaled
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    total = be.info()["entities"]
    report = be.undo(1)
    assert report["erased"] == 4  # two faces + two caps
    assert be.info()["entities"] == total - 4 == 1


def test_redo_redraws_and_a_second_undo_still_works(be):
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    be.undo(1)
    assert be.info()["entities"] == 0
    be.redo(1)
    assert be.info()["entities"] == 4
    report = be.undo(1)  # handles were refreshed by redo
    assert report["erased"] == 4 and report["missing"] == 0


def test_erase_tolerates_entities_deleted_elsewhere(be):
    with be.step("two lines"):
        be.line((0, 0), (1, 0))
        be.line((0, 1), (1, 1))
    victim = next(e for e in be.msp if e.dxf.handle == be.journal.steps[0].handles[0])
    be.msp.delete_entity(victim)
    report = be.undo(1)
    assert report["erased"] == 1 and report["missing"] == 1


def test_insert_inside_a_step_is_refused(be):
    from acadmcp.backends.base import BackendError

    with pytest.raises(BackendError), be.step("place"):
        be.insert("SOME_BLOCK", (0, 0))


def test_journal_survives_save_and_reopen(tmp_path):
    be = make_backend("dxf")
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    target = str(tmp_path / "plan.dxf")
    be.save(target)

    reopened = make_backend("dxf", path=target)
    assert [s.op for s in reopened.journal.steps] == ["room"]
    report = reopened.undo(1)  # handles still resolve after the round trip
    assert report["erased"] == 4 and reopened.info()["entities"] == 0


def test_a_journal_without_steps_writes_nothing(tmp_path):
    be = make_backend("dxf")
    be.line((0, 0), (1, 0))
    target = str(tmp_path / "plain.dxf")
    be.save(target)
    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps == [] and reopened.journal_unreadable == ""


def test_chinese_labels_round_trip(tmp_path):
    be = make_backend("dxf")
    with be.step("draw_room", label="生产车间", params={"名称": "车间"}):
        be.line((0, 0), (1, 0))
    target = str(tmp_path / "zh.dxf")
    be.save(target)
    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps[0].label == "生产车间"
    assert reopened.journal.steps[0].params == {"名称": "车间"}


# --- additions below the plan's own blocks ---------------------------------


def test_every_primitive_kind_records_and_undoes(be):
    """The hybrid branch and the dispatch table must cover all eight kinds."""
    with be.step("everything"):
        be.line((0, 0), (1000, 0))
        be.polyline([(0, 0), (0, 1000), (1000, 1000)])
        be.circle((500, 500), 100)
        be.arc((0, 0), 200, 0, 90)
        be.text((0, 0), "车间", 250)
        be.mtext((0, 0), "生产车间", 250, 4000)
        be.hatch([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
        be.linear_dimension((0, 0), (1000, 0), 500)
    assert be.info()["entities"] == 8
    report = be.undo(1)
    assert report["erased"] == 8 and report["missing"] == 0
    assert be.info()["entities"] == 0


def test_a_large_journal_chunks_and_reassembles(tmp_path):
    """A payload past JOURNAL_CHUNK splits across several code-1 tags and rejoins."""
    from ezdxf import readfile

    from acadmcp.backends.dxf import DxfBackend

    chunk = DxfBackend.JOURNAL_CHUNK
    be = make_backend("dxf")
    with be.step("room", label="L" * (chunk * 2), params={"note": "x" * chunk}):
        be.line((0, 0), (1000, 0))
    target = str(tmp_path / "big.dxf")
    be.save(target)

    # Read the file with plain ezdxf: the journal is ordinary document data.
    doc = readfile(target)
    xrecord = doc.rootdict["ACADMCP"]["JOURNAL"]
    chunks = [str(tag.value) for tag in xrecord.tags if tag.code == 1]
    assert len(chunks) > 1  # genuinely split, not one giant tag
    assert json.loads("".join(chunks)) == be.journal.to_dict()

    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps[0].label == be.journal.steps[0].label
    assert reopened.journal_unreadable == ""


def test_an_unreadable_journal_is_reported_not_raised(tmp_path):
    from ezdxf import readfile
    from ezdxf.lldxf.tags import Tags
    from ezdxf.lldxf.types import DXFTag

    be = make_backend("dxf")
    with be.step("room"):
        be.line((0, 0), (1, 0))
    target = str(tmp_path / "corrupt.dxf")
    be.save(target)

    doc = readfile(target)
    doc.rootdict["ACADMCP"]["JOURNAL"].tags = Tags([DXFTag(1, "not json at all")])
    doc.saveas(target)

    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps == []
    assert "ignored" in reopened.journal_unreadable
