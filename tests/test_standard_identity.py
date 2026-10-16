"""The drawing carries the drafting standard that drew it.

The standard is ambient: the package's tables plus whichever project standards files are
found by walking up from the working directory. Reopen the same drawing elsewhere and it is
silently checked against something else — its layers and its text-height series mean what
some other office decided. So the drawing remembers the standard it was drawn under — its
name and its *effective* tables — and reports when the same-named standard in force
disagrees. That is the catalogue's treatment, in the standards' vocabulary.

No AutoCAD needed: the identity travels inside the journal, which the dxf backend keeps in
the file. Every fixture standard here is synthetic, so nothing depends on a shipped GB/T
file another wave may be editing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server, settings, standards  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.standards import Standard  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_ambient():
    """Every test gets the standards as shipped. `settings.reset` drops the standards,
    templates and catalogue caches together — all three resolve through that file — and the
    session backend is reset so no test inherits another's drawing."""
    settings.reset()
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    settings.reset()
    server._state.backend = None


# --- synthetic standards, so no test depends on shipped GB/T data ----------


def _acme_tables(color: int = 5, height: float = 3.5) -> dict:
    """A tiny but valid standard: one layer and one nominal text height."""
    return {
        "layers": {"A-WALL": {"color": color, "linetype": "CONTINUOUS", "lineweight": None}},
        "text_heights": {str(height): {"height": height}},
    }


def _acme(color: int = 5, height: float = 3.5, label: str = "Acme Co. Standard") -> dict:
    return {"name": "acme", "label": label, "tables": _acme_tables(color, height)}


def _acme_standard(color: int = 5, height: float = 3.5) -> Standard:
    return Standard(name="acme", label="Acme Co. Standard", tables=_acme_tables(color, height))


def _write_project(root: Path, *, standards_entries=None, templates_entries=None) -> None:
    """A project's `acadmcp.json` naming the files its standards and templates live in."""
    payload: dict = {}
    if standards_entries is not None:
        (root / "std.json").write_text(
            json.dumps({"standards": standards_entries}), encoding="utf-8"
        )
        payload["standards"] = ["std.json"]
    if templates_entries is not None:
        (root / "tpl.json").write_text(
            json.dumps({"templates": templates_entries}), encoding="utf-8"
        )
        payload["templates"] = ["tpl.json"]
    (root / "acadmcp.json").write_text(json.dumps(payload), encoding="utf-8")


_ACME_TEMPLATE = {
    "key": "acme_plan",
    "label": "Acme plan",
    "standard": "acme",
    "paper": "A3",
    "scale": "1:100",
    "title_fields": {"图名": "", "图号": ""},
}


# --- identity --------------------------------------------------------------


def test_identity_adopts_back_to_the_same_tables_and_digest():
    loaded = standards.in_force("gbt50001")
    again = standards.Standards().adopt(standards.identity(loaded))
    assert again is not None
    assert again.name == loaded.name
    assert again.digest() == loaded.digest()
    assert again.tables == loaded.tables
    assert again.skipped == []


def test_identity_carries_the_tables_not_just_a_name():
    """A drawing that records only a name resolves to something else on the next machine,
    which is the whole failure this exists to prevent."""
    payload = standards.identity(_acme_standard(color=5))
    assert payload["name"] == "acme" and payload["label"] == "Acme Co. Standard"
    assert payload["digest"].startswith("sha256:")
    assert payload["tables"]["layers"]["A-WALL"]["color"] == 5
    assert "extends" not in payload  # stands alone, so nothing to declare


def test_the_digest_changes_when_a_table_row_does():
    first = standards.identity(_acme_standard(color=5))
    second = standards.identity(_acme_standard(color=7))
    assert first["digest"] != second["digest"]


def test_identity_refuses_a_non_standard_by_name():
    with pytest.raises(ValueError, match="identity needs a Standard"):
        standards.identity(None)  # type: ignore[arg-type]


def test_identity_captures_the_effective_tables_of_an_extending_standard(tmp_path, monkeypatch):
    """An `extends` child's digest covers its *merged* tables, so an adopted copy needs no
    parent present to stand whole."""
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(), {"name": "acme_site", "extends": "acme"}])
    settings.reset()
    child = standards.in_force("acme_site")
    payload = standards.identity(child)
    assert payload["extends"] == "acme"
    assert payload["tables"]["layers"]["A-WALL"]["color"] == 5  # the parent's row, inherited

    adopted = standards.Standards().adopt(payload)
    assert adopted.digest() == child.digest()
    assert adopted.layers["A-WALL"][0] == 5


# --- adopt's tolerance and precedence --------------------------------------


def test_adopt_is_tolerant_and_counts_what_it_skips():
    registry = standards.Standards()
    adopted = registry.adopt({"name": "acme", "tables": {"layers": None, "good": {"x": {"a": 1}}}})
    assert adopted is not None and "good" in adopted.tables and "layers" not in adopted.tables
    assert len(registry.skipped) == 1 and "must be an object" in registry.skipped[0]


def test_adopt_of_nonsense_is_none_rather_than_an_error():
    """Loading never raises, and a drawing with a damaged embedded standard must still open."""
    registry = standards.Standards()
    assert registry.adopt(None) is None
    assert registry.adopt({"tables": {"layers": {}}}) is None  # no name
    assert registry.adopt({"name": "acme", "tables": "not an object"}) is None
    assert registry.adopt({"name": "acme", "tables": {}}) is None
    assert len(registry.skipped) == 4


def test_adopt_never_overrides_a_name_already_in_force(tmp_path, monkeypatch):
    """The local standard is the authority: a drawing's copy of a standard the project also
    defines is not adopted over it."""
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(color=5)])
    settings.reset()
    before = standards.current().get("acme")

    returned = standards.current().adopt(_acme(color=9))

    assert returned is before
    after = standards.current().get("acme")
    assert after is before and after.digest() == before.digest()
    assert after.layers["A-WALL"][0] == 5  # not the drawing's 9


def test_adopt_installs_an_unknown_name_whole_so_it_resolves():
    registry = standards.Standards()
    assert registry.get("acme") is None
    registry.adopt(_acme(color=9))
    resolved = registry.in_force("acme")
    assert resolved is not None and resolved.layers["A-WALL"][0] == 9


# --- the drawing remembers it (dxf journal, no AutoCAD) --------------------


def test_a_saved_drawing_records_the_standard(tmp_path):
    be = make_backend("dxf")
    be.journal.note_meta(standard=standards.identity(_acme_standard(color=5)))
    be.save(str(tmp_path / "plan.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    recorded = reopened.journal.meta["standard"]
    assert recorded["name"] == "acme" and recorded["tables"]["layers"]["A-WALL"]["color"] == 5


def test_a_drawing_saved_without_a_standard_records_nothing(tmp_path):
    be = make_backend("dxf")
    be.save(str(tmp_path / "plain.dxf"))
    reopened = make_backend("dxf", path=str(tmp_path / "plain.dxf"))
    assert "standard" not in reopened.journal.meta


def test_the_embedded_digest_matches_the_standard_that_wrote_it(tmp_path):
    """`standard_warning` is a digest comparison, so the digest has to survive the round trip
    — a standard that hashed differently after being embedded would report a mismatch on
    every reopen."""
    written = _acme_standard(color=5)
    be = make_backend("dxf")
    be.journal.note_meta(standard=standards.identity(written))
    be.save(str(tmp_path / "plan.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    assert (
        standards.Standards().adopt(reopened.journal.meta["standard"]).digest() == written.digest()
    )


# --- stamping on drawing_new ------------------------------------------------


def test_drawing_new_stamps_the_standard_in_force():
    server.drawing_new(backend="dxf")
    recorded = server._state.backend.journal.meta["standard"]
    assert recorded["name"] == standards.in_force("").name


def test_drawing_new_from_the_shipped_template_stamps_the_templates_standard():
    server.drawing_new(backend="dxf", template="gb_a3_arch")
    recorded = server._state.backend.journal.meta["standard"]
    expected = standards.in_force("gbt50001")
    assert recorded["name"] == expected.name
    assert recorded["digest"] == expected.digest()


def test_a_template_stamps_its_own_standard_not_the_ambient_one(tmp_path, monkeypatch):
    """The layers a template sets come from *its* standard, so that is what the drawing must
    be checked against after a reopen — not whatever this process happens to have in force."""
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(color=5)], templates_entries=[_ACME_TEMPLATE])
    settings.reset()
    assert standards.in_force("").name != "acme"  # acme is not the ambient default

    server.drawing_new(backend="dxf", template="acme_plan")

    recorded = server._state.backend.journal.meta["standard"]
    assert recorded["name"] == "acme"
    assert recorded["tables"]["layers"]["A-WALL"]["color"] == 5


def test_a_template_makes_its_standard_the_one_in_force(tmp_path, monkeypatch):
    """`in_force` means "the standard a drawing is drawn under", so a session that opened a
    template declaring `acme` must report `acme` — not the package default.

    Measured in a sandbox run first: a GB/T 50104 template stamped and drew under 50104 while
    `drawing_status` still said gbt50001 was in force, with no warning, so a caller reading
    `in_force` got an answer the drawing was not drawn under."""
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(color=5)], templates_entries=[_ACME_TEMPLATE])
    settings.reset()

    server.drawing_new(backend="dxf", template="acme_plan")

    assert standards.in_force("").name == "acme"
    assert server.drawing_status()["standard"]["in_force"] == "acme"


def test_a_drawing_without_a_template_leaves_the_default_in_force():
    """The selection is a drawing's declaration, not a side effect of every new drawing."""
    server.drawing_new(backend="dxf")
    assert standards.in_force("").name == standards.DEFAULT_STANDARD


# --- drawing_open: adoption, and the warning --------------------------------


def test_reopening_elsewhere_adopts_the_recorded_standard(tmp_path, monkeypatch):
    """The point of the embedded copy: a drawing opened on a machine that never saw its
    standards file is still checked against the standard it was drawn under."""
    source = tmp_path / "a"
    source.mkdir()
    monkeypatch.chdir(source)
    _write_project(source, standards_entries=[_acme(color=5)], templates_entries=[_ACME_TEMPLATE])
    settings.reset()
    server.drawing_new(backend="dxf", template="acme_plan")
    plan = source / "plan.dxf"
    server.drawing_save(str(plan))

    elsewhere = tmp_path / "b"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings.reset()
    assert standards.get("acme") is None  # this process has never seen the project file

    report = server.drawing_open(str(plan))

    assert report["standard_source"] == "drawing"
    assert "standard_warning" not in report
    assert standards.get("acme") is not None  # adopted whole from the drawing
    status = server.drawing_status()
    assert status["drawing_standard"]["standard"]["name"] == "acme"
    assert status["drawing_standard"]["loaded"]["name"] == "acme"


def test_a_matching_standard_reports_no_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(color=5)], templates_entries=[_ACME_TEMPLATE])
    settings.reset()
    server.drawing_new(backend="dxf", template="acme_plan")
    plan = tmp_path / "plan.dxf"
    server.drawing_save(str(plan))

    settings.reset()  # re-read the same project
    report = server.drawing_open(str(plan))

    assert report["standard_source"] == "project"
    assert "standard_warning" not in report


def test_a_recorded_standard_this_session_lacks_is_reported_not_replaced_in_silence(
    tmp_path, monkeypatch
):
    """A foreign drawing whose standard this office does not define is the case the mechanism
    exists for, and it used to be the one case that said nothing.

    Measured: with a project's standards file in force and a drawing recording an 'acme' the
    project does not define, `drawing_open` answered `standard_source: project` with no warning
    while the journal's `acme` record became `gbt50001` — the drawing's own account of what drew
    it dropped, silently, and the file it was saved to afterwards claiming the package standard.
    The project's tables still win, exactly as before; what changed is that the caller is told
    which tables the drawing was actually drawn with.
    """
    be = make_backend("dxf")
    be.journal.note_meta(standard=standards.identity(_acme_standard(color=5)))
    plan = tmp_path / "plan.dxf"
    be.save(str(plan))

    monkeypatch.chdir(tmp_path)
    # The project declares a standard of its own, so the project's tables win — and 'acme',
    # the name this drawing recorded, is a standard it has not got at all.
    _write_project(
        tmp_path, standards_entries=[dict(_acme(color=5), name="office", label="Other office")]
    )
    settings.reset()
    assert standards.get("acme") is None

    report = server.drawing_open(str(plan))

    assert report["standard_source"] == "project"
    assert "'acme'" in report["standard_warning"], report
    assert "not loaded" in report["standard_warning"], report


def test_a_conflicting_same_name_project_standard_is_reported(tmp_path, monkeypatch):
    """The local standard wins — but silently checking somebody else's drawing against this
    office's tables is the failure the whole mechanism exists to prevent."""
    be = make_backend("dxf")
    be.journal.note_meta(standard=standards.identity(_acme_standard(color=5)))
    plan = tmp_path / "plan.dxf"
    be.save(str(plan))

    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(color=7)])  # same name, one row changed
    settings.reset()

    report = server.drawing_open(str(plan))

    assert report["standard_source"] == "project"
    assert "different 'acme'" in report["standard_warning"]
    # The project's tables actually win.
    assert standards.get("acme").layers["A-WALL"][0] == 7


def test_drawing_status_reports_a_recorded_standard_that_disagrees(tmp_path, monkeypatch):
    """Read-only: a drawing whose journal carries a standard the one in force disagrees with
    is reported by status without adopting or stamping anything."""
    be = make_backend("dxf")
    be.journal.note_meta(standard=standards.identity(_acme_standard(color=5)))
    server._state.reset(be, "dxf")

    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, standards_entries=[_acme(color=7)])
    settings.reset()

    status = server.drawing_status()

    assert status["drawing_standard"]["standard"]["name"] == "acme"
    assert status["drawing_standard"]["loaded"]["digest"] == standards.get("acme").digest()
    assert "different 'acme'" in status["standard_warning"]


# --- drawing_check prefers the recorded standard ----------------------------


def test_drawing_check_prefers_the_recorded_standard(tmp_path):
    """A session that started from a template drew under that template's standard; checking
    it against the ambient one empty would be a verdict about the wrong tables."""
    be = make_backend("dxf")
    be.journal.note_meta(standard=standards.identity(_acme_standard(color=5)))
    plan = tmp_path / "plan.dxf"
    be.save(str(plan))
    assert standards.in_force("").name != "acme"  # the fallback would be the wrong standard

    server.drawing_open(str(plan))
    report = server.drawing_check(check="standard")

    assert report["checks"]["standard"]["standard"] == "acme"
    assert report["checks"]["standard"]["digest"] == standards.get("acme").digest()


def test_an_old_drawing_with_no_recorded_standard_behaves_as_before():
    """No journal meta means byte-identical behaviour: empty `standard` is `in_force("")`."""
    be = make_backend("dxf")
    server._state.reset(be, "dxf")

    status = server.drawing_status()
    assert status["drawing_standard"]["standard"] is None
    assert "standard_warning" not in status

    report = server.drawing_check(check="standard")
    assert report["checks"]["standard"]["standard"] == standards.in_force("").name
