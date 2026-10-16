"""Phase C — the shipped templates: a registry, committed artefacts and the fingerprint gate.

Three things are pinned here.

* **The registry's contract**, which is the catalogue loader's restated: a broken entry is
  *counted*, never raised, and a template nobody shipped refuses by name listing the ones that
  were.
* **The binding to the sheet.** Every shipped spec's paper, orientation and scale are run
  through ``sheet.resolve_paper`` / ``sheet.resolve_scale`` — the same parsers ``drawing_new``
  will use — so a template the builder cannot place fails here rather than at the printer.
* **The reproducibility gate.** ``templates/*.dxf`` are built artefacts, not hand-drawn files,
  and ``scripts/build_templates.py --check`` rebuilds them and compares a **geometry
  fingerprint** — never the bytes, because DXF embeds timestamps, GUIDs and an occasionally
  re-ordered BLOCK_RECORD (the evaluation-fixture lesson), so a byte or `git status` check would
  report drift that is not there.

No AutoCAD is needed; the ``--dwt`` twin is a live-only path and is not exercised here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from acadmcp import server, settings, sheet, standards, templates  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402

SCRIPT = ROOT / "scripts" / "build_templates.py"

#: The keys the package ships, in the order ``Templates.keys()`` reports them (lower-cased,
#: sorted). Named once because a dozen assertions below are about *the set that ships*.
SHIPPED_KEYS = ["gb_a3_arch", "gb_a4_arch", "gbt50103_a2_site", "gbt50104_a3_plan"]


@pytest.fixture(autouse=True)
def _fresh_caches():
    """``templates.current()`` resolves through ``settings.current()``, so both caches are
    dropped around every test — the isolation test_standards.py uses for the same chain."""
    settings.reset()
    templates.reset()
    yield
    settings.reset()
    templates.reset()


def _project(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "acadmcp.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _templates_file(directory: Path, name: str, entries: list[dict]) -> Path:
    path = directory / name
    path.write_text(json.dumps({"templates": entries}, ensure_ascii=False), encoding="utf-8")
    return path


def _load_script():
    """The build script as a module, loaded by path — its `fingerprint` is what a test reads."""
    spec = importlib.util.spec_from_file_location("_build_templates_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_the_package_ships_the_gbt_templates():
    registry = templates.current()

    assert registry.keys() == SHIPPED_KEYS
    assert registry.skipped == []


def test_resolve_template_refuses_an_unknown_key_by_name():
    with pytest.raises(ValueError) as caught:
        templates.resolve_template("gb_a2_arch")

    message = str(caught.value)
    assert "gb_a2_arch" in message
    assert all(key in message for key in SHIPPED_KEYS), (
        "a refusal has to list what does exist, or the caller cannot pick one"
    )


def test_a_template_key_is_case_forgiving_like_the_catalogue():
    """`Templates.get` lower-cases a key, exactly as `Catalog.get` resolves an element key
    (`self.elements.get(key.strip().lower())`). The consistency check (F20) found the two
    registries keep one rule, so `GB_A3_ARCH` reaching the same spec is deliberate, not a bug."""
    a3 = templates.resolve_template("gb_a3_arch")

    assert templates.resolve_template("GB_A3_ARCH") is a3
    assert templates.current().get(" Gb_A3_Arch ") is a3


def test_the_shipped_specs_are_the_named_gbt_templates():
    a3 = templates.resolve_template("gb_a3_arch")
    a4 = templates.resolve_template("gb_a4_arch")
    plan = templates.resolve_template("gbt50104_a3_plan")
    site = templates.resolve_template("gbt50103_a2_site")

    assert (a3.standard, a3.paper, a3.orientation, a3.scale) == (
        "gbt50001",
        "A3",
        "landscape",
        "1:100",
    )
    assert (a4.standard, a4.paper, a4.orientation, a4.scale) == (
        "gbt50001",
        "A4",
        "portrait",
        "1:50",
    )
    assert a3.label == "A3 建筑平面（GB/T）"
    assert a4.label == "A4 建筑平面（GB/T）"
    assert a3.title_fields == {"图名": "", "图号": ""}
    # The two bound to a transcribed standard: an architectural plan at 1:100 on A3, a site plan
    # at 1:500 on A2 — GB/T 50103's 总平面 scales are the small ones.
    assert (plan.standard, plan.paper, plan.orientation, plan.scale) == (
        "gbt50104",
        "A3",
        "landscape",
        "1:100",
    )
    assert (site.standard, site.paper, site.orientation, site.scale) == (
        "gbt50103",
        "A2",
        "landscape",
        "1:500",
    )
    assert plan.label == "A3 建筑平面（GB/T 50104）"
    assert site.label == "A2 总平面（GB/T 50103）"
    assert plan.title_fields == {"图名": "", "图号": ""}


@pytest.mark.parametrize("key", SHIPPED_KEYS)
def test_a_specs_standard_is_one_the_registry_loaded(key):
    """A template naming a standard nothing loads would build through ``standards.in_force``'s
    None and fall back to the built-in layer scheme, silently dropping the standard it claims."""
    spec = templates.resolve_template(key)

    assert spec.standard in standards.current(), spec.standard


@pytest.mark.parametrize("key", SHIPPED_KEYS)
def test_a_specs_paper_and_scale_are_the_ones_the_sheet_accepts(key):
    """The binding that matters: what the template names, the sheet parser must take."""
    spec = templates.resolve_template(key)

    paper = sheet.resolve_paper(spec.paper, spec.orientation)  # raises if unknown
    numerator, denominator = sheet.resolve_scale(spec.scale)  # raises if not a ratio

    assert paper.name == spec.paper
    assert (paper.landscape, spec.orientation == "landscape") in ((True, True), (False, False))
    assert denominator / numerator > 0


def test_a_specs_artefact_path_is_committed_and_relative():
    for key in templates.current():
        spec = templates.resolve_template(key)
        assert spec.artefact == f"templates/{key}.dxf", "the artefact is where the tool says"
        assert not Path(spec.artefact).is_absolute()


def test_template_rows_report_every_spec():
    rows = templates.template_rows()
    by_key = {row["key"]: row for row in rows}

    assert set(by_key) == set(SHIPPED_KEYS)
    for key in by_key:
        row = by_key[key]
        assert set(row) >= {
            "key",
            "label",
            "standard",
            "paper",
            "orientation",
            "scale",
            "title_fields",
            "artefact",
        }
        assert row["title_fields"]["图名"] == ""


# ---------------------------------------------------------------------------
# loading: tolerant, the catalogue's rule
# ---------------------------------------------------------------------------


def test_a_broken_entry_is_counted_not_raised(tmp_path):
    broken = tmp_path / "templates.json"
    broken.write_text(
        """{"templates": [
            {"label": "no key here", "paper": "A3", "scale": "1:100"},
            "not an object",
            {"key": "no_paper", "scale": "1:100"}
        ]}""",
        encoding="utf-8",
    )

    registry = templates.load([broken])

    assert registry.keys() == SHIPPED_KEYS, "the shipped ones must survive"
    joined = " | ".join(registry.skipped)
    assert "no key" in joined or "key" in joined
    assert "not an object" in joined
    assert "paper" in joined


def test_a_project_file_can_add_a_template_without_erasing_the_shipped_ones(tmp_path):
    project = tmp_path / "templates.json"
    project.write_text(
        """{"templates": [
            {"key": "office_a2", "label": "A2 平面", "standard": "gbt50001",
             "paper": "A2", "orientation": "landscape", "scale": "1:200"}
        ]}""",
        encoding="utf-8",
    )

    registry = templates.load([project])

    assert registry.keys() == [*SHIPPED_KEYS, "office_a2"]
    office = registry.get("office_a2")
    assert office is not None and office.artefact == "templates/office_a2.dxf"


# ---------------------------------------------------------------------------
# the committed artefacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SHIPPED_KEYS)
def test_the_artefact_is_committed(key):
    spec = templates.resolve_template(key)

    assert (ROOT / spec.artefact).is_file(), f"{spec.artefact} must be built and committed"


@pytest.mark.parametrize("key", SHIPPED_KEYS)
def test_a_committed_artefact_records_the_standard_its_spec_names(key):
    """The artefact's own layer table came from its spec's standard, and the file says which.

    Without the record, opening `templates/gbt50104_a3_plan.dxf` directly adopts the package's
    gbt50001 — while the file's A-WALL is drawn at 50104's 0.70 mm — and a save then records
    gbt50001 as the standard that drawing was drawn under. The same failure
    `drawing_new(template=)` was fixed for, on the file the template ships.
    """
    spec = templates.resolve_template(key)
    be = make_backend("dxf", path=str(ROOT / spec.artefact))
    recorded = be.journal.meta["standard"]

    assert recorded["name"] == spec.standard
    assert recorded["digest"] == standards.in_force(spec.standard).digest()
    assert be.sheet["paper"] == spec.paper, "and it is still a drawing, not only a record"


def test_the_build_records_the_standard_in_the_artefact_it_writes(tmp_path):
    script = _load_script()

    script.build_all(tmp_path)

    for spec in templates.load().specs():
        be = make_backend("dxf", path=str(tmp_path / f"{spec.key}.dxf"))
        assert be.journal.meta["standard"]["name"] == spec.standard, spec.key


def test_a_committed_artefact_carries_its_sheet():
    """The artefact is a real drawing: reopening it reports the page setup it was built with."""
    spec = templates.resolve_template("gb_a3_arch")
    be = make_backend("dxf", path=str(ROOT / spec.artefact))

    assert be.sheet["paper"] == "A3"
    assert be.sheet["scale"] == "1:100"
    assert be.entities(), "a sheet is drawing content, so the file holds entities"


def test_the_fingerprint_is_stable_and_counts_geometry():
    script = _load_script()
    path = ROOT / templates.resolve_template("gb_a3_arch").artefact

    once = script.fingerprint(path)
    again = script.fingerprint(path)

    assert once == again
    assert sum(once.values()) > 0
    assert any(dxftype == "LINE" for dxftype, *_ in once), "the frame is lines"


def test_the_built_artefact_passes_the_standard_gate(tmp_path):
    """D4: the build fails if the artefact it produced breaks the standard it encodes."""
    script = _load_script()

    report = script.build_all(tmp_path)

    assert {row["key"] for row in report} == set(SHIPPED_KEYS)
    for row in report:
        assert row["errors"] == [], row["errors"]


def test_artefacts_rebuild_identically():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# the tool surface
# ---------------------------------------------------------------------------


@pytest.fixture
def _tool_session():
    """A fresh dxf drawing behind the tool surface, dropped again afterwards."""
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_template_list_returns_the_shipped_templates():
    rows = server.template_list()["templates"]

    assert {row["key"] for row in rows} == set(SHIPPED_KEYS)
    assert all(row["paper"] and row["scale"] and row["standard"] for row in rows)


def test_drawing_new_from_a_template_carries_the_sheet(_tool_session):
    """Tool level: the template's sheet is drawn as a real step and reported in info()."""
    out = server.drawing_new(backend="dxf", template="gb_a3_arch")

    assert out["ok"] is True
    assert out["template"]["key"] == "gb_a3_arch"
    assert out["sheet"]["paper"] == "A3"
    assert out["sheet"]["scale"] == "1:100"

    be = server._state.backend
    assert be.entities(layer="A-SHEET"), "the frame is drawn content on the sheet's own layer"


def test_drawing_new_refuses_an_unknown_template_by_name(_tool_session):
    with pytest.raises(ValueError, match="unknown template"):
        server.drawing_new(backend="dxf", template="gb_a2_arch")


# ---------------------------------------------------------------------------
# project templates: named by acadmcp.json, the standards rule
# ---------------------------------------------------------------------------
#
# The project's own files are read through ``settings`` exactly as its standards are, so the
# cases below mirror test_standards.py's: the parsing of the key, the resolution relative to the
# file that named it, the merge's precedence, and that a reset makes an edit visible.


def test_settings_reads_a_templates_string_as_a_one_item_list(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path, {"template": "mine.json"}))
    settings.reset()

    assert settings.current().templates == [str(tmp_path / "mine.json")]


def test_settings_resolves_template_paths_against_the_settings_file(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path, {"templates": ["a.json", "sheets/b.json"]}))
    settings.reset()

    assert settings.current().templates == [
        str(tmp_path / "a.json"),
        str(tmp_path / "sheets" / "b.json"),
    ]


def test_settings_without_a_templates_key_names_none(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path, {"library": ""}))
    settings.reset()

    assert settings.current().templates == []


def test_settings_describe_names_the_templates_files():
    described = settings.Settings(templates=["a.json"]).describe()

    assert described["templates"] == ["a.json"]


def test_current_reads_the_project_templates_named_by_settings(tmp_path, monkeypatch):
    project = _project(tmp_path, {"templates": ["mine.json"]})
    _templates_file(
        project,
        "mine.json",
        [{"key": "office_a2", "label": "A2 平面", "paper": "A2", "scale": "1:200"}],
    )
    monkeypatch.chdir(project)
    settings.reset()

    registry = templates.current()

    assert registry.keys() == [*SHIPPED_KEYS, "office_a2"], "the shipped ones stay"
    office = registry.get("office_a2")
    assert office is not None and office.scale == "1:200"


def test_a_project_template_wins_a_same_key_without_erasing_the_rest(tmp_path, monkeypatch):
    project = _project(tmp_path, {"templates": ["mine.json"]})
    _templates_file(
        project,
        "mine.json",
        [
            {
                "key": "gb_a3_arch",
                "label": "House A3",
                "standard": "gbt50001",
                "paper": "A3",
                "orientation": "landscape",
                "scale": "1:50",
            }
        ],
    )
    monkeypatch.chdir(project)
    settings.reset()

    registry = templates.current()

    assert registry.keys() == SHIPPED_KEYS, "an override adds nothing"
    assert registry.get("gb_a3_arch").label == "House A3"
    assert registry.get("gb_a3_arch").scale == "1:50"
    assert registry.get("gb_a4_arch").label == "A4 建筑平面（GB/T）", "the other is inherited"


def test_artefact_path_resolves_beside_the_declaring_file(tmp_path, monkeypatch):
    project = _project(tmp_path, {"templates": ["mine.json"]})
    _templates_file(project, "mine.json", [{"key": "office_a2", "paper": "A2", "scale": "1:200"}])
    monkeypatch.chdir(project)
    settings.reset()

    project_spec = templates.current().get("office_a2")
    assert templates.artefact_path(project_spec) == (
        Path(project).resolve() / "templates" / "office_a2.dxf"
    ), "a project artefact sits beside the file that declared it"

    package_spec = templates.load().get("gb_a3_arch")
    assert templates.artefact_path(package_spec) == ROOT / "templates" / "gb_a3_arch.dxf", (
        "a package artefact keeps the repository-root path it always had"
    )


def test_settings_reset_makes_an_edited_project_file_visible(tmp_path, monkeypatch):
    project = _project(tmp_path, {"templates": ["mine.json"]})
    _templates_file(project, "mine.json", [{"key": "office_a2", "paper": "A2", "scale": "1:200"}])
    monkeypatch.chdir(project)
    settings.reset()

    assert "office_a2" in templates.current()

    _templates_file(project, "mine.json", [{"key": "office_a1", "paper": "A1", "scale": "1:200"}])
    settings.reset()

    registry = templates.current()
    assert "office_a1" in registry
    assert "office_a2" not in registry, "the old file's key is gone, not merged forever"


def test_the_build_and_gate_cover_only_the_package_specs(tmp_path, monkeypatch):
    """The build script loads the package's own file, so a project's templates in the working
    directory — which ``templates.current()`` would see — cannot join the gated artefacts."""
    project = _project(tmp_path, {"templates": ["mine.json"]})
    _templates_file(project, "mine.json", [{"key": "office_a2", "paper": "A2", "scale": "1:200"}])
    monkeypatch.chdir(project)
    settings.reset()

    assert "office_a2" in templates.current(), "the project template is visible to the tool"

    script = _load_script()
    assert {row["key"] for row in script.build_all(tmp_path / "out")} == set(SHIPPED_KEYS)
