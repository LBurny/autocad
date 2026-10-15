"""The drawing carries the catalogue that drew it.

The catalogue is ambient: a package file plus whichever project ``acadmcp.json`` is found
by walking up from the working directory. Reopen the same drawing elsewhere and its doors
silently mean something else. So the drawing remembers the catalogue — its identity *and
its entries* — and reports when the one loaded now disagrees.

No AutoCAD needed: the identity travels inside the journal, which the dxf backend keeps in
the file, and the COM sidecar is plain file writing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog, settings  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.com import read_sidecar, sidecar_path, write_sidecar  # noqa: E402
from acadmcp.catalog import Element, Variant  # noqa: E402
from acadmcp.journal import Journal  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_catalogue():
    """Every test gets the catalogue and settings as shipped. Adopting a drawing merges its
    entries into the catalogue in force, so a test that left one behind would report it to the
    next file's tests."""
    catalog.reset()
    settings.reset()
    yield
    catalog.reset()
    settings.reset()


def _element(**over):
    """An op-only door, the shape a project's catalogue would carry for one."""
    base = dict(
        key="door",
        label="门",
        category="opening",
        block="",
        source="",
        layer="A-DOOR",
        op="door_symbol",
        width_param="",
        variants={"900": Variant(name="900", params={"width": 900})},
        tag_prefix="M",
        default_variant="900",
    )
    base.update(over)
    return Element(**base)


def _op_element(**over):
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


# --- identity --------------------------------------------------------------


def test_identity_survives_adopt():
    loaded = catalog.Catalog.load()
    again = catalog.Catalog.adopt(loaded.identity())
    assert again.keys() == loaded.keys()
    assert again.skipped == []


def test_the_digest_changes_when_an_entry_does():
    first = catalog.Catalog({"door": _element(layer="A-DOOR")}).identity()
    second = catalog.Catalog({"door": _element(layer="A-OTHER")}).identity()
    assert first["digest"] != second["digest"]


def test_identity_carries_the_entries_not_just_a_path():
    """A drawing that records only a file path resolves to something else on the next
    machine, which is the whole failure this exists to prevent."""
    identity = catalog.Catalog({"door": _element()}).identity(library="L")
    assert identity["library"] == "L"
    assert identity["count"] == 1
    assert identity["elements"][0]["key"] == "door"
    assert identity["digest"].startswith("sha256:")


def test_adopt_is_tolerant_and_counts_what_it_skips():
    adopted = catalog.Catalog.adopt(
        {
            "library": "L",
            "elements": [{"key": "ghost", "label": "?"}, {"key": "column", "op": "column"}],
        }
    )
    assert adopted.get("ghost") is None and adopted.get("column").op == "column"
    assert len(adopted.skipped) == 1 and "neither a block nor an op" in adopted.skipped[0]


def test_adopt_of_nonsense_is_empty_rather_than_an_error():
    """Loading never raises, and a drawing with a damaged embedded catalogue must still
    open — the report counts what it could not read."""
    assert catalog.Catalog.adopt(None).keys() == []
    assert catalog.Catalog.adopt({"elements": "not a list"}).keys() == []


def test_adopt_also_reads_a_report_shaped_payload():
    payload = {"categories": {"opening": [{"key": "door", "op": "door_symbol"}]}}
    assert catalog.Catalog.adopt(payload).get("door").op == "door_symbol"


# --- what a drawing carries that the catalogue in force has not got --------
# A project's own files win the precedence, and they should. But the drawing also carries the
# elements it was drawn with, and one captured in it and never published was unreachable the
# moment the project had a catalogue of its own — while its block definition stayed in the file,
# so it could be neither placed nor captured again under that name.


def _project(tmp_path: Path, entries: list[dict]) -> None:
    (tmp_path / "elements.json").write_text(json.dumps({"elements": entries}), encoding="utf-8")
    (tmp_path / "acadmcp.json").write_text(
        json.dumps({"catalog": ["elements.json"]}), encoding="utf-8"
    )


def test_adopt_merges_the_drawings_entries_into_the_one_in_force(tmp_path, monkeypatch):
    _project(tmp_path, [{"key": "project_door", "block": "P", "source": "ours.dwg"}])
    monkeypatch.chdir(tmp_path)
    settings.reset()
    catalog.reset()
    drawing = catalog.Catalog({"orphan_door": _element(key="orphan_door")}).identity()

    adopted = catalog.Catalog.adopt(drawing)  # what drawing_open does, via embedded_catalog

    assert adopted.get("orphan_door") is not None  # the rebuild still returns the drawing's own
    in_force = catalog.current()
    assert in_force.get("project_door") is not None  # the project's entry stands
    assert in_force.get("orphan_door") is not None  # ...and the drawing's is reachable
    # Adopted, exactly like the existing path: no catalogue file behind it, so a
    # `geometry_file` cannot be resolved for it and the caller is told by name.
    assert in_force.source_file("orphan_door") is None


def test_a_colliding_key_keeps_the_entry_in_force(tmp_path, monkeypatch):
    """The local standard wins: a drawing's copy of an element the project also defines is not
    adopted over it, and the key is reported so the difference is visible."""
    _project(tmp_path, [{"key": "door", "block": "OURS", "source": "ours.dwg"}])
    monkeypatch.chdir(tmp_path)
    settings.reset()
    catalog.reset()
    drawing = catalog.Catalog({"door": _element(key="door", block="THEIRS")}).identity()

    catalog.Catalog.adopt(drawing)

    assert catalog.current().get("door").block == "OURS"
    assert catalog.current().adopt_missing(catalog.Catalog({"door": _element()})) == ["door"]


def test_the_drawing_carrying_the_same_catalogue_leaves_the_digest_alone(tmp_path, monkeypatch):
    """The warning is a digest comparison, so adopting a drawing that carries exactly the
    catalogue in force must change nothing it can see — or every reopen of a drawing this
    office drew reports a mismatch."""
    _project(tmp_path, [{"key": "project_door", "block": "P", "source": "ours.dwg"}])
    monkeypatch.chdir(tmp_path)
    settings.reset()
    catalog.reset()
    before = catalog.current()

    catalog.Catalog.adopt(before.identity())

    after = catalog.current()
    assert after.digest() == before.digest()
    assert after.skipped == before.skipped


# --- the drawing remembers it ----------------------------------------------


def test_a_saved_drawing_records_the_catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"door": _element()})
    )
    be = make_backend("dxf")
    be.note_catalog(catalog.current())
    be.save(str(tmp_path / "plan.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    assert reopened.drawing_catalog()["catalog"]["count"] == 1
    assert reopened.drawing_catalog()["catalog"]["elements"][0]["key"] == "door"


def test_a_drawing_saved_without_a_catalogue_records_nothing(tmp_path):
    be = make_backend("dxf")
    be.save(str(tmp_path / "plain.dxf"))
    reopened = make_backend("dxf", path=str(tmp_path / "plain.dxf"))
    assert reopened.drawing_catalog()["catalog"] is None


def test_an_adopted_catalogue_keeps_the_entry_placeable(tmp_path):
    """The point of the embedded copy: reopening elsewhere still resolves the element."""
    be = make_backend("dxf")
    be.note_catalog(catalog.Catalog({"column": _op_element()}))
    be.save(str(tmp_path / "plan.dxf"))
    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    embedded = reopened.embedded_catalog()
    assert embedded.get("column").op == "column"


def test_the_embedded_digest_matches_the_catalogue_that_wrote_it(tmp_path):
    """`drawing_catalog`'s warning is a digest comparison, so the digest has to survive the
    round trip — a catalogue that hashed differently after being embedded would report a
    mismatch on every reopen."""
    written = catalog.Catalog({"door": _element()})
    be = make_backend("dxf")
    be.note_catalog(written)
    be.save(str(tmp_path / "plan.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    assert reopened.embedded_catalog().digest() == written.digest()


def test_a_different_catalogue_is_reported_rather_than_acted_on(tmp_path, monkeypatch):
    """The local standard wins — but silently redrawing somebody else's doors with this
    office's blocks is the failure the whole mechanism exists to prevent."""
    be = make_backend("dxf")
    be.note_catalog(catalog.Catalog({"door": _element()}))
    be.save(str(tmp_path / "plan.dxf"))

    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"door": _element(layer="A-OTHER")})
    )
    report = make_backend("dxf", path=str(tmp_path / "plan.dxf")).drawing_catalog()
    assert report["catalog"]["count"] == 1
    assert report["loaded"]["count"] == 1
    assert "different catalogue" in report["warning"]


def test_info_reports_the_recorded_catalogue_as_a_summary(tmp_path):
    be = make_backend("dxf")
    assert be.info()["catalog"] is None
    be.note_catalog(catalog.Catalog({"door": _element()}))
    be.save(str(tmp_path / "plan.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    summary = reopened.info()["catalog"]
    assert summary["count"] == 1 and summary["digest"].startswith("sha256:")


# --- the COM sidecar -------------------------------------------------------
# COM cannot write an XRecord through ActiveX, so a session's journal goes in a file
# beside the drawing. The file writing needs no AutoCAD, so it is tested directly here;
# what is untested is only the call to it from `save()`.


def test_the_sidecar_sits_beside_the_drawing():
    assert str(sidecar_path("C:/drawings/plan.dwg")).endswith("plan.dwg.acadmcp.json")


def test_a_sidecar_round_trips_a_journal(tmp_path):
    journal = Journal()
    journal.note_meta(catalog={"digest": "sha256:x", "count": 1})
    journal.append(
        "draw_line", "", {"p1": [0, 0]}, [{"kind": "line", "points": [0, 0, 1, 0]}], ["A1"]
    )

    written = write_sidecar(tmp_path / "plan.dwg", journal)

    assert written is not None and written.name == "plan.dwg.acadmcp.json"
    back, problem = read_sidecar(tmp_path / "plan.dwg")
    assert problem == ""
    assert back.meta["catalog"] == {"digest": "sha256:x", "count": 1}
    assert [s.op for s in back.steps] == ["draw_line"]
    assert back.steps[0].handles == ["A1"]


def test_a_session_with_nothing_to_say_writes_no_sidecar(tmp_path):
    """A file appearing next to somebody's drawing unannounced is not acceptable, so an
    ordinary save must not produce one at all — not even an empty one."""
    assert write_sidecar(tmp_path / "plan.dwg", Journal()) is None
    assert not sidecar_path(tmp_path / "plan.dwg").exists()


def test_an_absent_sidecar_is_not_a_problem_and_a_broken_one_is_reported(tmp_path):
    assert read_sidecar(tmp_path / "nothing.dwg") == (None, "")

    sidecar_path(tmp_path / "plan.dwg").write_text("{not json", encoding="utf-8")
    journal, problem = read_sidecar(tmp_path / "plan.dwg")
    assert journal is None and "ignored" in problem


def test_a_newer_sidecar_journal_is_reported_not_silently_empty(tmp_path):
    """The same version guard, reached through the sidecar: a future format must not
    degrade to "no journal" without saying so."""
    sidecar_path(tmp_path / "plan.dwg").write_text('{"version": 2, "steps": []}', encoding="utf-8")
    journal, problem = read_sidecar(tmp_path / "plan.dwg")
    assert journal is None and "version 2" in problem
