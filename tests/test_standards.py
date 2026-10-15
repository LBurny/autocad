"""Phase A — the standard is data: GB/T tables with sources, refused by name.

Three things are pinned here.

* **The loader's contract**, which is the catalogue loader's restated: a broken file is
  *counted*, never raised, and a lookup outside a table refuses by name with the table's
  coverage rather than interpolating a neighbouring value.
* **The data discipline.** Every transcribed row states its source, and a table nobody has
  transcribed ships empty. The shipped layer table mirrors ``layers.LAYERS`` and the title
  block mirrors ``sheet.py``'s constants — the tests below are what keeps the two in step, so
  neither can drift while the other is edited.
* **The layer stack.** A standard's lineweight reaches a real layer table through
  ``ensure_layer`` on every driver, and ``ops.setup_layers`` applies the standard under the
  project's own overrides.

No AutoCAD is needed; the one COM test skips itself where pywin32 is absent, the way
``test_com_backend.py`` does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from acadmcp import layers, ops, settings, sheet, standards  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Both caches describe the working directory, so both are dropped around every test."""
    settings.reset()
    standards.reset()
    yield
    settings.reset()
    standards.reset()


class FakeBackend:
    """Records what a standard-driven layer setup asks for, keyword by keyword.

    A stand-in rather than a real backend on purpose: what is being pinned here is the
    *call* `setup_layers` makes, including that the lineweight travels as a keyword.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None, str | None, float | None]] = []

    def ensure_layer(
        self,
        name: str,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
    ) -> dict[str, object]:
        self.calls.append((name, color, linetype, lineweight))
        return {"layer": name}

    def specs(self) -> dict[str, tuple[int | None, str | None, float | None]]:
        return {name: (color, linetype, weight) for name, color, linetype, weight in self.calls}


def _standard(name: str, layers_table: dict) -> standards.Standard:
    return standards.Standard(name=name, label=name, tables={"layers": layers_table})


def _row(color: int, linetype: str = "CONTINUOUS", lineweight: float | None = None) -> dict:
    return {
        "color": color,
        "linetype": linetype,
        "lineweight": lineweight,
        "source": "a test table",
    }


def _project(tmp_path: Path, payload: dict, *, standards_payload: dict | None = None) -> Path:
    if standards_payload is not None:
        (tmp_path / "std.json").write_text(
            json.dumps(standards_payload, ensure_ascii=False), encoding="utf-8"
        )
    (tmp_path / "acadmcp.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# the shipped data
# ---------------------------------------------------------------------------


def test_the_package_ships_the_gbt50001_standard():
    registry = standards.current()

    assert registry.keys() == ["gbt50001"]
    assert registry.get("gbt50001") is registry.in_force()
    assert registry.skipped == []


def test_a_row_is_reached_regardless_of_case():
    std = standards.current().get("gbt50001")

    # A project file that spells a layer name the way AutoCAD shows it still lands on its row.
    assert std.lookup("layers", "a-wall") == std.lookup("layers", "A-WALL")


def test_lookup_refuses_outside_the_table_by_name():
    std = standards.current().get("gbt50001")

    with pytest.raises(ValueError, match="no row for '0.33'.*coverage"):
        std.lookup("text_heights", "0.33")


def test_an_empty_table_refuses_everything():
    std = standards.current().get("gbt50001")

    with pytest.raises(ValueError, match="not transcribed"):
        std.lookup("line_width_groups", "1:20")  # row shipped empty pending transcription


def test_an_unknown_table_names_the_table():
    std = standards.current().get("gbt50001")

    with pytest.raises(ValueError, match="no table 'materials'"):
        std.lookup("materials", "Q235")


def test_the_shipped_layer_table_mirrors_layers_LAYERS():
    std = standards.current().get("gbt50001")

    assert set(std.layers) == set(layers.LAYERS)
    for name, (color, linetype) in layers.LAYERS.items():
        spec = std.layers[name]
        assert (spec[0], spec[1]) == (color, linetype), name


def test_every_layer_row_states_its_lineweight_and_none_is_invented():
    std = standards.current().get("gbt50001")

    rows = std.rows("layers")
    assert rows, "the layer table should be transcribed"
    for name, row in rows.items():
        assert "lineweight" in row, f"{name} does not state a lineweight at all"
        assert row["lineweight"] is None, (
            f"{name} claims a line weight ({row['lineweight']}) that nobody transcribed "
            "from GB/T 50001"
        )
    assert std.layers["A-WALL"] == (2, "CONTINUOUS", None)


def test_the_title_block_mirrors_the_sheet_constants():
    std = standards.current().get("gbt50001")

    block = std.title_block
    assert block is not None
    assert float(block["width"]) == sheet.TITLE_BLOCK_WIDTH
    assert float(block["height"]) == sheet.TITLE_BLOCK_HEIGHT
    assert int(block["rows"]) == sheet.TITLE_BLOCK_ROWS
    assert float(block["text_height"]) == sheet.TITLE_BLOCK_TEXT
    assert block["source"].startswith("GB/T 10609.1")


def test_the_text_height_series_is_the_nominal_one_and_sourced():
    std = standards.current().get("gbt50001")

    assert std.text_heights == frozenset({2.5, 3.5, 5.0, 7.0, 10.0, 14.0, 20.0})
    for key, row in std.rows("text_heights").items():
        assert row.get("source"), f"text height {key} is transcribed from nowhere"


def test_the_scales_are_a_pointer_to_the_one_list_not_a_second_copy():
    std = standards.current().get("gbt50001")

    # One source for one list: the standard points at sheet.SCALES instead of restating it,
    # so there is no second copy to keep in step.
    assert std.scales is sheet.SCALES
    assert set(std.tables["scales"]) <= {"_see", "_source", "_note"}
    with pytest.raises(ValueError, match="not transcribed"):
        std.lookup("scales", "1:100")


def test_no_row_reaches_a_drawing_without_a_source():
    registry = standards.current()

    for name in registry:
        std = registry.get(name)
        assert std is not None
        for table, rows in std.tables.items():
            for key, row in rows.items():
                if key.startswith("_"):
                    continue
                assert isinstance(row, dict), f"{name}/{table}/{key} is not a row"
                assert row.get("source"), f"{name}/{table}/{key} states no source"


# ---------------------------------------------------------------------------
# loading: tolerant, later files winning row by row
# ---------------------------------------------------------------------------


def test_a_broken_project_file_is_skipped_not_raised(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")

    registry = standards.load([broken])

    assert registry.keys() == ["gbt50001"], "the package's own standard must survive"
    assert registry.skipped and "broken.json" in registry.skipped[0]


def test_an_unreadable_standard_entry_is_counted_not_raised(tmp_path):
    project = tmp_path / "std.json"
    project.write_text(
        json.dumps(
            {
                "standards": [
                    {"tables": {"layers": {}}},  # no name
                    {"name": "x", "tables": {"layers": {"A-WALL": "colour 3"}}},  # not a row
                    {"name": "y", "tables": []},  # not a table set
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = standards.load([project])

    assert "gbt50001" in registry
    joined = " | ".join(registry.skipped)
    assert "no name" in joined
    assert "layers/A-WALL must be an object" in joined
    assert "no tables" in joined


def test_a_project_file_replaces_a_row_and_leaves_the_rest(tmp_path):
    project = tmp_path / "std.json"
    project.write_text(
        json.dumps(
            {
                "standards": [
                    {
                        "name": "gbt50001",
                        "tables": {
                            "layers": {
                                "a-wall": {
                                    "color": 3,
                                    "linetype": "DASHED",
                                    "lineweight": 0.5,
                                    "source": "our office table 1",
                                }
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    std = standards.load([project]).get("gbt50001")

    assert std.layers["A-WALL"] == (3, "DASHED", 0.5)
    assert std.layers["A-DOOR"] == (4, "CONTINUOUS", None), "package rows must survive"


def test_a_standard_can_be_built_in_code_and_used():
    std = _standard("office", {"A-WALL": _row(6, "CONTINUOUS", 0.7)})

    assert std.layers == {"A-WALL": (6, "CONTINUOUS", 0.7)}
    assert std.text_heights == frozenset()
    with pytest.raises(ValueError, match="no table 'text_heights'"):
        std.lookup("text_heights", "3.5")


# ---------------------------------------------------------------------------
# A2: the lineweight travels through the layer stack
# ---------------------------------------------------------------------------


def test_dxf_writes_the_lineweight_in_hundredths_of_a_millimetre():
    be = make_backend("dxf")

    be.ensure_layer("A-WALL", 2, "CONTINUOUS", lineweight=0.7)

    assert be.doc.layers.get("A-WALL").dxf.lineweight == 70


def test_dxf_leaves_the_lineweight_alone_when_none_is_asked_for():
    be = make_backend("dxf")
    be.ensure_layer("A-WALL", 2)
    before = be.doc.layers.get("A-WALL").dxf.lineweight

    be.ensure_layer("A-WALL", 2, "CONTINUOUS", lineweight=None)

    assert be.doc.layers.get("A-WALL").dxf.lineweight == before


def test_a_lineweight_outside_autocads_own_list_is_refused():
    # AutoCAD's LineWeight enum stops at 2.11 mm (probed out of acdbmgd with ApiProbe, not
    # recalled), so a width past it is a caller slip rather than a design.
    with pytest.raises(ValueError, match="lineweight"):
        standards.lineweight_centimm(9.0)
    with pytest.raises(ValueError, match="lineweight"):
        standards.lineweight_centimm(-0.1)
    assert standards.lineweight_centimm(0.35) == 35


def test_the_plugin_forwards_the_lineweight_over_rpc():
    client = _FakeClient()
    be = _plugin_backend(client)

    be.ensure_layer("A-WALL", 2, "CONTINUOUS", 0.7)
    be.ensure_layer("A-DOOR", 4)

    assert [method for method, _ in client.calls] == ["ensure_layer"] * 2
    assert client.calls[0][1]["lineweight"] == 0.7
    assert client.calls[1][1]["lineweight"] is None


class _FakeClient:
    """Stands in for the socket, so the payload can be read without AutoCAD."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, **payload) -> dict:
        self.calls.append((method, payload))
        return {"layer": payload.get("layer"), "created": True}


def _plugin_backend(client: _FakeClient):
    from acadmcp.backends.plugin import PluginBackend

    return PluginBackend(client=client)


def test_com_sets_the_lineweight_in_hundredths_of_a_millimetre():
    pytest.importorskip("pythoncom")
    from acadmcp.backends import com

    class _Layer:
        Lineweight = -3

    class _Layers:
        def __init__(self) -> None:
            self.layer = _Layer()

        def Item(self, name: str) -> _Layer:
            return self.layer

        def Add(self, name: str) -> _Layer:
            return self.layer

    class _Linetypes:
        def Load(self, name: str, library: str) -> None:  # noqa: N802 - the COM name
            return None

    class _Doc:
        def __init__(self) -> None:
            self.Layers = _Layers()
            self.Linetypes = _Linetypes()

    be = com.ComBackend.__new__(com.ComBackend)  # no connect: the layer table is a stand-in
    be.doc = _Doc()

    be.ensure_layer("A-WALL", 2, "CONTINUOUS", lineweight=0.7)

    assert be.doc.Layers.layer.Lineweight == 70


# ---------------------------------------------------------------------------
# A3: the standard drives the layer setup, the project overlays it
# ---------------------------------------------------------------------------


def test_setup_layers_applies_the_standards_lineweight():
    standards.use(standards.Standards([_standard("test", {"A-WALL": _row(2, "CONTINUOUS", 0.7)})]))
    be = FakeBackend()

    created = ops.setup_layers(be, "test")

    assert created == ["A-WALL"]
    assert be.specs()["A-WALL"] == (2, "CONTINUOUS", 0.7)


def test_setup_layers_without_a_standard_uses_the_builtin_scheme():
    standards.use(None)
    be = FakeBackend()

    created = ops.setup_layers(be)

    assert created == sorted(layers.LAYERS)
    assert be.specs()["A-WALL"] == (2, "CONTINUOUS", None)


def test_the_shipped_standard_draws_the_same_layer_set_as_the_builtin_scheme():
    """The data change must not move a single layer."""
    be = FakeBackend()

    created = ops.setup_layers(be)

    assert created == sorted(layers.LAYERS)
    for name, (color, linetype) in layers.LAYERS.items():
        assert be.specs()[name][:2] == (color, linetype), name


def test_a_project_layers_overlay_wins_and_keeps_the_standards_lineweight(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path, {"layers": {"A-WALL": [3, "DASHED"]}}))
    settings.reset()  # resets the standards too, so the one in force is installed after it
    standards.use(
        standards.Standards(
            [
                _standard(
                    "test",
                    {"A-WALL": _row(2, "CONTINUOUS", 0.7), "A-DOOR": _row(4, "CONTINUOUS", 0.25)},
                )
            ]
        )
    )
    be = FakeBackend()

    ops.setup_layers(be, "test")

    assert be.specs()["A-WALL"] == (3, "DASHED", 0.7), "the project's colour, the standard's width"
    assert be.specs()["A-DOOR"] == (4, "CONTINUOUS", 0.25)


def test_a_project_standards_file_is_read_through_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(
        _project(
            tmp_path,
            {"standards": ["std.json"]},
            standards_payload={
                "standards": [
                    {
                        "name": "gbt50001",
                        "tables": {
                            "layers": {
                                "A-WALL": {
                                    "color": 6,
                                    "linetype": "CONTINUOUS",
                                    "lineweight": 0.5,
                                    "source": "office standard",
                                }
                            }
                        },
                    }
                ]
            },
        )
    )
    settings.reset()

    assert settings.current().standards == [str(tmp_path / "std.json")]
    assert standards.current().get("gbt50001").layers["A-WALL"] == (6, "CONTINUOUS", 0.5)

    be = FakeBackend()
    ops.setup_layers(be)
    assert be.specs()["A-WALL"] == (6, "CONTINUOUS", 0.5)
    assert be.specs()["A-DOOR"] == (4, "CONTINUOUS", None), "the package's row, untouched"


def test_settings_describe_names_the_standards_files():
    described = settings.Settings(standards=["a.json", "b.json"]).describe()

    assert described["standards"] == ["a.json", "b.json"]


def test_settings_reset_forgets_the_standard_as_well():
    standards.use(standards.Standards([_standard("gbt50001", {"A-WALL": _row(6)})]))
    assert standards.in_force("gbt50001").layers["A-WALL"][0] == 6

    settings.reset()

    assert standards.in_force("gbt50001").layers["A-WALL"][0] == 2, "back to the package's row"


# ---------------------------------------------------------------------------
# Phase B: check() — the conformance checker
# ---------------------------------------------------------------------------
#
# The checker is pure over a *snapshot* of the drawing, so every case below is plain data and
# no AutoCAD is involved. Each rule gets its own failing test first: units, the layers in use,
# the text heights (the rule AutoCAD's own CHECKSTANDARDS runs), and the sheet.

GB_SERIES = (2.5, 3.5, 5.0, 7.0, 10.0, 14.0, 20.0)


def _heights(*values) -> dict:
    return {str(value): {"height": value, "source": "test series"} for value in values}


def _checking_standard(**tables) -> standards.Standard:
    return standards.Standard(name="office", label="office", tables=tables)


def _snapshot(**over) -> dict:
    base = {"units": "mm", "layers_in_use": [], "texts": [], "sheet": {"scale": "1:100"}}
    base.update(over)
    return base


def _by_rule(issues: list[dict], rule: str) -> list[dict]:
    return [issue for issue in issues if issue["rule"] == rule]


def _one(issues: list[dict], rule: str) -> dict:
    found = _by_rule(issues, rule)
    assert len(found) == 1, f"expected exactly one {rule!r} issue, got {issues}"
    return found[0]


# --- the shape of a finding -------------------------------------------------


def test_every_issue_carries_exactly_the_four_documented_keys():
    std = _checking_standard(layers={"A-WALL": _row(2)}, text_heights=_heights(*GB_SERIES))
    snapshot = _snapshot(
        units="m",
        layers_in_use=["MY-LAYER"],
        texts=[{"height": 260.0, "layer": "A-ANNO", "content": "注"}],
        sheet={},
    )

    issues = standards.check(std, snapshot)

    assert issues, "this snapshot breaks every rule, so it must produce findings"
    for issue in issues:
        assert set(issue) == {"rule", "severity", "detail", "entities"}
        assert issue["severity"] in ("error", "warning")
        assert isinstance(issue["entities"], list)
        json.dumps(issue), "a finding travels to a client as JSON"


def test_the_declared_rule_codes_are_the_ones_produced():
    std = _checking_standard(layers={"A-WALL": _row(2)}, text_heights=_heights(*GB_SERIES))
    breaking = _snapshot(
        units="m",
        layers_in_use=["MY-LAYER"],
        texts=[{"height": 260.0, "layer": "A-ANNO", "content": "注"}],
        sheet={"paper": "A3"},  # a sheet with no scale
    )

    produced = {issue["rule"] for issue in standards.check(std, breaking)}
    produced |= {issue["rule"] for issue in standards.check(std, _snapshot(sheet={}))}

    assert produced == set(standards.RULES)
    assert standards.RULES == (
        "units_not_mm",
        "layer_not_in_standard",
        "text_height_off_series",
        "no_sheet",
        "sheet_without_scale",
    )


def test_check_tolerates_a_junk_snapshot_and_never_raises():
    std = _checking_standard()

    # Missing keys, a None snapshot, a string where a list belongs: none of it may raise, the
    # rule the journal reader and the catalogue loader keep.
    assert standards.check(std, {}) is not None
    assert standards.check(std, {"layers_in_use": "A-WALL", "texts": None}) is not None
    assert standards.check(std, "not a snapshot") is not None


def test_check_refuses_a_missing_standard_by_name():
    # No standard loaded is a caller mistake, not a finding: there is nothing to check against.
    with pytest.raises(ValueError, match="Standard"):
        standards.check(None, _snapshot())


# --- rule 1: units ----------------------------------------------------------


def test_mm_units_are_clean_and_anything_else_is_an_error():
    std = _checking_standard()

    assert _by_rule(standards.check(std, _snapshot(units="mm")), "units_not_mm") == []

    issue = _one(standards.check(std, _snapshot(units="m")), "units_not_mm")
    assert issue["severity"] == "error"
    assert issue["detail"]["units"] == "m"
    assert issue["detail"]["expected"] == "mm"


def test_mm_is_matched_regardless_of_case_and_padding():
    std = _checking_standard()

    assert _by_rule(standards.check(std, _snapshot(units=" MM ")), "units_not_mm") == []


# --- rule 2: the layers in use ----------------------------------------------


def test_a_layer_outside_the_standard_warns():
    std = _checking_standard(layers={"A-WALL": _row(2), "A-DOOR": _row(4)})

    issue = _one(
        standards.check(std, _snapshot(layers_in_use=["A-WALL", "MY-LAYER", "a-door"])),
        "layer_not_in_standard",
    )

    assert issue["severity"] == "warning"
    assert issue["detail"]["layer"] == "MY-LAYER"
    assert issue["entities"] == ["MY-LAYER"]


def test_the_standards_own_layers_are_not_reported():
    std = _checking_standard(layers={"A-WALL": _row(2)})

    # A project that spells the layer the way AutoCAD shows it still lands on its row.
    assert standards.check(std, _snapshot(layers_in_use=["A-WALL", "a-wall"])) == []


def test_an_unknown_layer_is_reported_once_however_often_it_appears():
    std = _checking_standard(layers={"A-WALL": _row(2)})

    issues = standards.check(std, _snapshot(layers_in_use=["MY-LAYER", "my-layer"]))

    assert len(_by_rule(issues, "layer_not_in_standard")) == 1


# --- rule 3: text heights ---------------------------------------------------


def test_a_text_height_off_the_series_is_an_error_naming_the_nearest():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))

    issue = _one(
        standards.check(
            std,
            _snapshot(texts=[{"height": 260.0, "layer": "A-ROOM-AREA", "content": "会议室"}]),
        ),
        "text_height_off_series",
    )

    assert issue["severity"] == "error"
    assert issue["detail"]["paper_height_mm"] == 2.6
    assert issue["detail"]["nearest_nominal_mm"] == 2.5
    assert issue["entities"] == ["会议室"]


def test_a_text_height_on_the_series_is_clean():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))

    for height in (250.0, 350.0, 500.0):  # 2.5 / 3.5 / 5.0 mm at 1:100
        issues = standards.check(
            std, _snapshot(texts=[{"height": height, "layer": "A-ROOM-NAME", "content": "X"}])
        )
        assert _by_rule(issues, "text_height_off_series") == [], height


def test_the_sheet_scale_sets_the_paper_height():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))
    text = [{"height": 175.0, "layer": "A-ANNO", "content": "注"}]

    clean = standards.check(std, _snapshot(texts=text, sheet={"scale": "1:50"}))
    assert _by_rule(clean, "text_height_off_series") == []  # 175 / 50 = 3.5 mm

    off = standards.check(std, _snapshot(texts=text, sheet={"scale": "1:100"}))
    issue = _one(off, "text_height_off_series")
    assert issue["detail"]["paper_height_mm"] == 1.75


def test_a_blank_text_is_skipped():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))
    texts = [
        {"height": 260.0, "layer": "A-ANNO", "content": "   "},
        {"height": 260.0, "layer": "A-ANNO", "content": ""},
    ]

    assert _by_rule(standards.check(std, _snapshot(texts=texts)), "text_height_off_series") == []


def test_an_unreadable_text_height_is_skipped_not_guessed():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))
    texts = [
        {"height": None, "layer": "A-ANNO", "content": "a"},
        {"height": "tall", "layer": "A-ANNO", "content": "b"},
        {"layer": "A-ANNO", "content": "c"},
        {"height": 0, "layer": "A-ANNO", "content": "d"},
    ]

    assert _by_rule(standards.check(std, _snapshot(texts=texts)), "text_height_off_series") == []


def test_without_a_sheet_the_assumed_scale_is_named_in_the_finding():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))

    issue = _one(
        standards.check(
            std,
            _snapshot(texts=[{"height": 260.0, "layer": "A-ANNO", "content": "注"}], sheet={}),
        ),
        "text_height_off_series",
    )

    assert issue["detail"]["assumed_scale"] == "1:100"
    # The same text at a height the assumed 1:100 does hold is clean, so the assumption is a
    # stated denominator, not a licence to flag everything.
    assert (
        _by_rule(
            standards.check(
                std,
                _snapshot(texts=[{"height": 250.0, "layer": "A-ANNO", "content": "注"}], sheet={}),
            ),
            "text_height_off_series",
        )
        == []
    )


def test_a_standard_with_no_text_height_series_says_nothing_about_heights():
    std = _checking_standard()  # no text_heights table transcribed

    issue = standards.check(
        std, _snapshot(texts=[{"height": 260.0, "layer": "A-ANNO", "content": "注"}])
    )

    assert _by_rule(issue, "text_height_off_series") == []


def test_the_sheets_own_text_is_paper_and_is_not_divided_by_the_scale():
    std = _checking_standard(layers={"A-SHEET-TEXT": _row(7)}, text_heights=_heights(*GB_SERIES))

    # A title-block field is drawn at 1:1 in paper mm: 5.0 is 5.0, not 0.05 — otherwise every
    # sheeted drawing would carry a false error on the standard's own field.
    texts = [{"height": 5.0, "layer": "A-SHEET-TEXT", "content": "比例 1:100"}]

    assert _by_rule(standards.check(std, _snapshot(texts=texts)), "text_height_off_series") == []


def test_a_text_flagged_paper_is_measured_at_one_to_one():
    std = _checking_standard(text_heights=_heights(*GB_SERIES))
    texts = [{"height": 5.0, "layer": "A-ANNO", "content": "x", "paper": True}]

    assert _by_rule(standards.check(std, _snapshot(texts=texts)), "text_height_off_series") == []


# --- rule 4: the sheet ------------------------------------------------------


def test_a_drawing_with_no_sheet_warns():
    std = _checking_standard()

    issue = _one(standards.check(std, _snapshot(sheet={})), "no_sheet")

    assert issue["severity"] == "warning"


def test_a_sheet_stating_its_scale_is_clean():
    std = _checking_standard()
    sheet = {"paper": "A3", "orientation": "landscape", "scale": "1:100"}

    assert _by_rule(standards.check(std, _snapshot(sheet=sheet)), "sheet_without_scale") == []


def test_a_sheet_without_a_scale_is_an_error():
    std = _checking_standard()

    issue = _one(
        standards.check(std, _snapshot(sheet={"paper": "A3", "orientation": "landscape"})),
        "sheet_without_scale",
    )

    assert issue["severity"] == "error"


def test_a_title_block_without_the_scale_field_is_an_error():
    std = _checking_standard()
    sheet = {"scale": "1:100", "title_fields": {"图名": "一层平面", "图号": "建施-01"}}

    issue = _one(standards.check(std, _snapshot(sheet=sheet)), "sheet_without_scale")

    assert issue["severity"] == "error"


def test_a_title_block_stating_the_scale_is_clean():
    std = _checking_standard()
    sheet = {"scale": "1:100", "title_fields": {"图名": "一层平面", "比例": "1:100"}}

    assert _by_rule(standards.check(std, _snapshot(sheet=sheet)), "sheet_without_scale") == []


# --- the whole thing against the shipped data -------------------------------


def test_a_conforming_drawing_reports_no_issues():
    std = standards.current().get("gbt50001")
    snapshot = _snapshot(
        layers_in_use=["A-WALL", "A-ROOM-NAME", "A-DIM"],
        texts=[
            {"height": 250.0, "layer": "A-ROOM-AREA", "content": "办公室"},
            {"height": 350.0, "layer": "A-ROOM-NAME", "content": "生产车间"},
        ],
        sheet={"paper": "A3", "orientation": "landscape", "scale": "1:100"},
    )

    assert standards.check(std, snapshot) == []


def test_the_shipped_standard_flags_a_known_bad_height():
    std = standards.current().get("gbt50001")

    # layers.H_TEXT is 300 mm, which at 1:100 is 3.0 mm — off GB/T 50001's nominal series.
    issue = _one(
        standards.check(
            std, _snapshot(texts=[{"height": 300.0, "layer": "A-ANNO", "content": "注"}])
        ),
        "text_height_off_series",
    )

    assert issue["detail"]["nearest_nominal_mm"] == 2.5
