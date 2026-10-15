"""The dxf backend journals stepped drawing; eager drawing is unchanged."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.dxf import DxfBackend  # noqa: E402
from acadmcp.catalog import Element, Variant  # noqa: E402


def _op_element(**over):
    """An op-only column, the shape `tests/test_catalog_ops.py` builds for one.

    Copied rather than imported: every test module here sets up its own fixtures and path.
    """
    base = dict(
        key="column",
        label="柱",
        category="structure",
        block="",
        source="",
        layer="A-COLUMN",
        op="column",
        variants={"600": Variant(name="600", params={"size": 600})},
        tag_prefix="G",
        default_variant="600",
    )
    base.update(over)
    return Element(**base)


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
    xrecord = doc.rootdict[DxfBackend.JOURNAL_DICT][DxfBackend.JOURNAL_KEY]
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
    doc.rootdict[DxfBackend.JOURNAL_DICT][DxfBackend.JOURNAL_KEY].tags = Tags(
        [DXFTag(1, "not json at all")]
    )
    doc.saveas(target)

    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps == []
    assert "ignored" in reopened.journal_unreadable


# --- entities(), which the tag numbering reads ------------------------------


def test_entities_reports_text_so_tag_numbering_can_continue(be):
    """`entities()` returned only `{"type", "layer"}`, so `ops.existing_tags` saw an empty
    drawing and numbering restarted at 1 on every call — two runs of placed columns shared
    the tags G-1 and G-2, which is the "an empty entity list restarts numbering at 1"
    hazard AGENTS.md records for com.py, still live on the headless backend."""
    first = ops.place_along(be, _op_element(), (0, 0), (6000, 0), count=2)
    second = ops.place_along(be, _op_element(), (0, 3000), (6000, 3000), count=2)

    assert first["tags"] == ["G-1", "G-2"]
    assert second["tags"] == ["G-3", "G-4"]
    assert ops.existing_tags(be) == ["G-1", "G-2", "G-3", "G-4"]


def test_entities_reports_a_block_reference_under_both_names(be):
    """Parity with the plugin's `entities`: a dynamic block can resolve to an anonymous
    `*U` record, so neither `block` nor `definition` is on its own the name that was asked
    for. ezdxf resolves both to the reference's own name."""
    block = be.doc.blocks.new("TEST_BLOCK")
    block.add_line((0, 0), (1, 0))
    be.msp.add_blockref("TEST_BLOCK", (0, 0))

    inserts = [row for row in be.entities() if row["type"] == "INSERT"]
    assert inserts == [
        {
            "type": "INSERT",
            "layer": "0",
            "block": "TEST_BLOCK",
            "definition": "TEST_BLOCK",
            "at": [0.0, 0.0],
            "rotation": 0.0,
            "xscale": 1.0,
            "yscale": 1.0,
        }
    ]


def test_an_insert_row_says_where_the_reference_landed(be):
    """The read-only surface can answer "where did my element go?" without a round trip.

    An INSERT row used to be `{type, layer, block, definition}` and nothing more, so an agent
    that had just placed an element had to save the DXF and read it with ezdxf to confirm the
    placement — the one question its own listing could not answer."""
    block = be.doc.blocks.new("PLACED")
    block.add_line((0, 0), (1, 0))
    be.msp.add_blockref("PLACED", (1500, 2500), dxfattribs={"rotation": 30.0})
    be.msp.add_blockref("PLACED", (0, 0), dxfattribs={"xscale": 2.0, "yscale": 0.5})

    rows = [row for row in be.entities() if row["type"] == "INSERT"]
    assert rows[0]["at"] == [1500.0, 2500.0] and rows[0]["rotation"] == 30.0
    assert rows[1]["xscale"] == 2.0 and rows[1]["yscale"] == 0.5
    # And only INSERT rows carry placement: the shape of every other row is unchanged.
    be.msp.add_line((0, 0), (1, 1))
    line = next(row for row in be.entities() if row["type"] == "LINE")
    assert line == {"type": "LINE", "layer": "0"}


# --- the name our record goes under ----------------------------------------


def test_the_journal_names_itself_and_leaves_no_legacy_record(tmp_path):
    """`ACADMCP` is claimed by another server too, so our record says its own name."""
    import ezdxf

    be = make_backend("dxf")
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    target = str(tmp_path / "named.dxf")
    be.save(target)

    doc = ezdxf.readfile(target)
    assert DxfBackend.JOURNAL_DICT in doc.rootdict
    assert be.JOURNAL_KEY in doc.rootdict[DxfBackend.JOURNAL_DICT]
    assert DxfBackend.LEGACY_JOURNAL_DICT not in doc.rootdict


def test_a_journal_written_under_the_old_name_is_still_read(tmp_path):
    """A drawing saved by an older build must not lose its undo history."""
    import ezdxf
    from ezdxf.lldxf.tags import Tags
    from ezdxf.lldxf.types import DXFTag

    payload = json.dumps(
        {
            "version": 1,
            "steps": [
                {
                    "index": 1,
                    "op": "room",
                    "label": "",
                    "params": {},
                    "primitives": [],
                    "handles": [],
                    "state": "applied",
                }
            ],
        },
        ensure_ascii=True,
    )
    doc = ezdxf.new("R2010")
    record = doc.rootdict.get_required_dict(DxfBackend.LEGACY_JOURNAL_DICT).add_xrecord(
        DxfBackend.JOURNAL_KEY
    )
    record.tags = Tags([DXFTag(1, payload)])
    target = str(tmp_path / "legacy.dxf")
    doc.saveas(target)

    reopened = make_backend("dxf", path=target)
    assert [s.op for s in reopened.journal.steps] == ["room"]
    assert reopened.journal_unreadable == ""


def test_migrating_a_legacy_record_leaves_a_shared_dictionary_standing(tmp_path):
    """Only our own record moves. Another application's use of `ACADMCP` is its own."""
    import ezdxf
    from ezdxf.lldxf.tags import Tags
    from ezdxf.lldxf.types import DXFTag

    doc = ezdxf.new("R2010")
    shared = doc.rootdict.get_required_dict(DxfBackend.LEGACY_JOURNAL_DICT)
    shared.add_xrecord(DxfBackend.JOURNAL_KEY).tags = Tags(
        [DXFTag(1, '{"version": 1, "steps": []}')]
    )
    shared.add_xrecord("SOMEONE_ELSE").tags = Tags([DXFTag(1, "theirs")])
    target = str(tmp_path / "shared.dxf")
    doc.saveas(target)

    be = make_backend("dxf", path=target)
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    be.save()

    doc = ezdxf.readfile(target)
    assert DxfBackend.LEGACY_JOURNAL_DICT in doc.rootdict, "their record must survive"
    assert "SOMEONE_ELSE" in doc.rootdict[DxfBackend.LEGACY_JOURNAL_DICT]
    assert be.JOURNAL_KEY not in doc.rootdict[DxfBackend.LEGACY_JOURNAL_DICT]
