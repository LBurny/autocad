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
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from acadmcp import sheet, templates  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402

SCRIPT = ROOT / "scripts" / "build_templates.py"


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


def test_the_package_ships_two_templates():
    registry = templates.current()

    assert registry.keys() == ["gb_a3_arch", "gb_a4_arch"]
    assert registry.skipped == []


def test_resolve_template_refuses_an_unknown_key_by_name():
    with pytest.raises(ValueError) as caught:
        templates.resolve_template("gb_a2_arch")

    message = str(caught.value)
    assert "gb_a2_arch" in message
    assert "gb_a3_arch" in message and "gb_a4_arch" in message, (
        "a refusal has to list what does exist, or the caller cannot pick one"
    )


def test_the_shipped_specs_are_the_named_gbt_templates():
    a3 = templates.resolve_template("gb_a3_arch")
    a4 = templates.resolve_template("gb_a4_arch")

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


@pytest.mark.parametrize("key", ["gb_a3_arch", "gb_a4_arch"])
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

    assert set(by_key) == {"gb_a3_arch", "gb_a4_arch"}
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

    assert registry.keys() == ["gb_a3_arch", "gb_a4_arch"], "the shipped ones must survive"
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

    assert registry.keys() == ["gb_a3_arch", "gb_a4_arch", "office_a2"]
    office = registry.get("office_a2")
    assert office is not None and office.artefact == "templates/office_a2.dxf"


# ---------------------------------------------------------------------------
# the committed artefacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["gb_a3_arch", "gb_a4_arch"])
def test_the_artefact_is_committed(key):
    spec = templates.resolve_template(key)

    assert (ROOT / spec.artefact).is_file(), f"{spec.artefact} must be built and committed"


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

    assert {row["key"] for row in report} == {"gb_a3_arch", "gb_a4_arch"}
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
