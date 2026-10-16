"""The element catalogue: loading, checking, numbering and repeating.

No AutoCAD needed. The catalogue is a plain file, and what it does with a file that is
wrong is the part worth testing: it has to keep the entries it could read, say which ones
it could not, and never take the drawing down with it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog, geom, ops, settings  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.catalog import Catalog, Element, Variant  # noqa: E402

#: What the package ships. Named once, so a test that counts entries says what it is
#: counting instead of carrying a number to keep in step by hand.
SHIPPED_KEYS = [
    "beam_i",
    "beam_wf",
    "channel",
    "column",
    "door",
    "level_mark",
    "light",
    "north_arrow",
    "section_mark",
    "stud",
    "weld_symbol",
    "window",
    "window_elevation",
]
SHIPPED = len(SHIPPED_KEYS)


@pytest.fixture(autouse=True)
def _fresh_catalogue():
    """Every test gets the catalogue and settings as shipped, not what another one left."""
    catalog.reset()
    settings.reset()
    yield
    catalog.reset()
    settings.reset()


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "elements.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# the shipped catalogue
# ---------------------------------------------------------------------------


def test_the_shipped_catalogue_loads_with_nothing_skipped():
    loaded = Catalog.load()

    assert loaded.skipped == []
    assert loaded.keys() == SHIPPED_KEYS


def test_every_shipped_entry_can_be_placed_by_something():
    """Every entry reaches the paper one way or another: a library block, or an op where no
    library block stands for the thing. An entry with neither cannot be drawn at all."""
    loaded = Catalog.load()

    for key, element in loaded.elements.items():
        assert element.kind in ("block", "op", "both"), key
        assert element.layer, key
        if element.block:
            assert element.source.endswith(".dwg"), key
            assert "{library}" not in element.source, key
        else:
            # An op-only entry has no library to name, and saying it does would send a
            # reader looking for a block that does not exist.
            assert element.op, key


def test_a_library_path_can_be_pointed_somewhere_else(tmp_path, monkeypatch):
    monkeypatch.setenv("ACADMCP_LIBRARY", r"D:\院标\块库")
    loaded = Catalog.load()

    from_library = [e for e in loaded.elements.values() if e.block]
    assert from_library and all(e.source.startswith(r"D:\院标\块库") for e in from_library)


# ---------------------------------------------------------------------------
# loading what is there, and reporting what is not
# ---------------------------------------------------------------------------


def test_a_missing_file_is_reported_rather_than_raised():
    loaded = Catalog.load(["Z:/nowhere/elements.json"])

    assert len(loaded.elements) == SHIPPED
    assert any("no such catalogue file" in note for note in loaded.skipped)


def test_a_file_that_is_not_json_is_reported(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    loaded = Catalog.load([path])

    assert len(loaded.elements) == SHIPPED
    assert any("broken.json" in note for note in loaded.skipped)


def test_a_payload_that_is_not_a_list_is_reported(tmp_path):
    loaded = Catalog.load([_write(tmp_path, {"elements": "nope"})])

    assert len(loaded.elements) == SHIPPED
    assert any("expected a list" in note for note in loaded.skipped)


def test_a_bad_entry_is_skipped_and_the_good_ones_survive(tmp_path):
    path = _write(
        tmp_path,
        {
            "library": r"D:\lib",
            "elements": [
                {"key": "good", "block": "B", "source": "{library}\\a.dwg"},
                "not an object",
                {"block": "B"},  # no key
                {"key": "noblock"},  # neither a block nor an op, so nothing to place
                {"key": "novariants", "block": "B", "variants": "nope"},
            ],
        },
    )

    loaded = Catalog.load([path])

    assert "good" in loaded.elements
    assert loaded.elements["good"].source == r"D:\lib\a.dwg"
    assert "novariants" in loaded.elements and loaded.elements["novariants"].variants == {}
    assert len(loaded.skipped) == 4
    assert any("neither a block nor an op" in note for note in loaded.skipped)


def test_a_later_file_replaces_an_entry_of_the_same_key(tmp_path):
    path = _write(
        tmp_path,
        {"elements": [{"key": "door", "block": "我们的门", "source": "x.dwg", "layer": "A-DOOR"}]},
    )

    loaded = Catalog.load([path])

    assert loaded.elements["door"].block == "我们的门"
    assert len(loaded.elements) == SHIPPED


def test_a_bare_variant_value_becomes_its_parameter(tmp_path):
    path = _write(tmp_path, {"elements": [{"key": "k", "block": "B", "variants": {"900": 900}}]})

    variant = Catalog.load([path]).elements["k"].variants["900"]

    assert variant.params == {"size": 900}


# ---------------------------------------------------------------------------
# what a caller gets out of an entry
# ---------------------------------------------------------------------------


def test_settings_go_element_then_variant_then_caller():
    element = Element(
        key="k",
        label="k",
        category="c",
        block="B",
        source="",
        layer="0",
        params={"wall": 200, "size": 1},
        variants={"big": Variant(name="big", params={"size": 400})},
    )

    # No default is named, so the first variant listed is the one that gets placed: an
    # entry that offers sizes must not leave the block at whatever its author left it.
    assert element.settings() == {"wall": 200, "size": 400}
    assert element.settings("big") == {"wall": 200, "size": 400}
    assert element.settings("big", {"size": 900, "extra": True}) == {
        "wall": 200,
        "size": 900,
        "extra": True,
    }


def test_a_variant_that_does_not_exist_is_refused_with_the_list():
    element = Catalog.load().elements["door"]

    with pytest.raises(ValueError, match="no variant '950'.*600, 700, 800, 900, 1000"):
        element.settings("950")


def test_an_element_with_no_variants_places_unparameterised():
    element = Element(key="k", label="k", category="c", block="B", source="", layer="0")

    assert element.variant() is None
    assert element.settings() == {}


def test_the_named_variant_wins_over_the_default():
    element = Catalog.load().elements["door"]

    assert element.settings() == element.settings(element.default_variant)
    assert element.settings("600")["门尺寸"] == 600
    assert element.settings("600")["墙厚"] == 150  # the element's own parameter survives


# ---------------------------------------------------------------------------
# the check that catches a catalogue drifting from its library
# ---------------------------------------------------------------------------


def test_an_element_whose_block_the_library_has_not_got_is_reported():
    loaded = Catalog.load()
    # Every library read, and every block in it, except the door's — the one thing that has
    # gone missing. Nothing else may be reported, or the check is noise.
    known = {
        source: [e.block for e in loaded.elements.values() if e.source == source]
        for source in loaded.sources()
    }
    door = loaded.elements["door"]
    known[door.source] = [name for name in known[door.source] if name != door.block]

    problems = loaded.check_against(known)

    assert problems == [f"door: '{door.source}' has no block '{door.block}'"]


def test_a_library_that_could_not_be_read_is_reported_as_unchecked():
    problems = Catalog.load().check_against({})

    assert problems and all("unchecked" in note for note in problems)


def test_a_catalogue_with_nothing_to_check_passes():
    loaded = Catalog()
    loaded.elements["k"] = Element(
        key="k", label="k", category="c", block="B", source="", layer="0"
    )

    assert loaded.check_against({}) == []


# ---------------------------------------------------------------------------
# numbering, derived from the drawing
# ---------------------------------------------------------------------------


def test_next_tag_follows_the_highest_number_in_use():
    assert catalog.next_tag("M", ["M-1", "M-2", "M-7", "C-3"]) == "M-8"


def test_next_tag_ignores_a_tag_it_cannot_read():
    # A hand-written "M-?" is not a number, and must not restart the count at 1.
    assert catalog.next_tag("M", ["M-9", "M-?"]) == "M-10"


def test_next_tag_starts_from_one_when_there_are_none():
    assert catalog.next_tag("C", []) == "C-1"
    assert catalog.next_tag("", ["M-1"]) == ""


def test_next_tag_does_not_match_a_longer_prefix():
    # "MS-1" must not be read as an "M" tag with the number "S-1", nor push the count up.
    assert catalog.next_tag("M", ["MS-40", "M-2"]) == "M-3"


# ---------------------------------------------------------------------------
# repetition
# ---------------------------------------------------------------------------


def test_a_count_spreads_evenly_including_both_ends():
    assert catalog.positions_along((0, 0), (6000, 0), count=3) == [
        (0.0, 0.0),
        (3000.0, 0.0),
        (6000.0, 0.0),
    ]


def test_a_spacing_steps_along_and_stops_at_the_end():
    points = catalog.positions_along((0, 0), (5000, 0), spacing=2000)

    assert points == [(0.0, 0.0), (2000.0, 0.0), (4000.0, 0.0)]


def test_a_spacing_that_divides_exactly_still_gets_its_last_point():
    assert catalog.positions_along((0, 0), (4000, 0), spacing=2000)[-1] == (4000.0, 0.0)


def test_an_inset_pulls_the_ends_in():
    points = catalog.positions_along((0, 0), (6000, 0), count=2, inset=500)

    assert points == [(500.0, 0.0), (5500.0, 0.0)]


def test_along_a_diagonal_run():
    points = catalog.positions_along((0, 0), (3000, 4000), count=2)

    assert points == [(0.0, 0.0), (3000.0, 4000.0)]


def test_a_count_of_one_lands_in_the_middle():
    assert catalog.positions_along((0, 0), (6000, 0), count=1) == [(3000.0, 0.0)]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"count": 3, "spacing": 2000}, "not both"),
        ({}, "count or a spacing"),
        ({"count": 0}, "at least 1"),
        ({"spacing": 0}, "more than 0"),
    ],
)
def test_an_impossible_run_is_refused_with_a_reason(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        catalog.positions_along((0, 0), (6000, 0), **kwargs)


def test_a_run_with_no_length_is_refused():
    with pytest.raises(ValueError, match="no length"):
        catalog.positions_along((0, 0), (0, 0), count=2)


def test_a_grid_is_every_combination():
    assert catalog.positions_grid([0, 6000], [0, 4000]) == [
        (0.0, 0.0),
        (6000.0, 0.0),
        (0.0, 4000.0),
        (6000.0, 4000.0),
    ]


# ---------------------------------------------------------------------------
# placing, against a backend that records instead of drawing
# ---------------------------------------------------------------------------


class Recording:
    """A backend that keeps what it was asked for, so the ops can be read back."""

    name = "recording"
    #: It records `insert` calls, so it is one of the backends a catalogue entry reaches as
    #: its library block rather than as its parametric twin.
    supports_insert = True

    def __init__(self, texts: list[str] | None = None) -> None:
        self.inserts: list[dict] = []
        self.texts: list[dict] = []
        self.existing = list(texts or [])

    def insert(self, block, at, **kwargs):
        self.inserts.append({"block": block, "at": at, **kwargs})
        return len(self.inserts) - 1

    def text_scale(self) -> float:
        return 100.0

    def text(self, pos, content, height, *, layer="0", rotation=0.0, align="left"):
        self.texts.append({"text": content, "at": pos, "layer": layer})
        return len(self.texts) - 1

    def entities(self, layer=None, limit=200):
        return [{"text": text} for text in self.existing]


def _door() -> Element:
    return Catalog.load().elements["door"]


def test_placing_an_element_sets_the_variant_on_the_block():
    be = Recording()
    element = _door()

    ops.place_element(be, element, (1000, 2000), variant="800", rotation=90, tag="M-1")

    assert len(be.inserts) == 1
    placed = be.inserts[0]
    assert placed["block"] == element.block
    assert placed["source"] == element.source
    assert placed["at"] == (1000, 2000)
    assert placed["rotation"] == 90
    assert placed["properties"] == {"墙厚": 150, "门尺寸": 800}
    assert placed["layer"] == element.layer
    assert be.texts[0]["text"] == "M-1"


def test_placing_without_a_tag_draws_no_text():
    be = Recording()

    ops.place_element(be, _door(), (0, 0))

    assert be.texts == []


def test_placing_a_run_numbers_the_tags_from_the_drawing():
    be = Recording(texts=["M-1", "M-4", "C-9"])

    summary = ops.place_along(be, _door(), (0, 0), (6000, 0), count=3)

    assert summary["placed"] == 3
    assert summary["tags"] == ["M-5", "M-6", "M-7"]
    assert [text["text"] for text in be.texts] == ["M-5", "M-6", "M-7"]
    assert all(placed["properties"]["门尺寸"] == 900 for placed in be.inserts)


def test_a_run_takes_its_rotation_from_the_run():
    be = Recording()

    summary = ops.place_along(be, _door(), (0, 0), (0, 6000), count=2, tag=False)

    # Straight up the sheet is 90 degrees, so the doors lie in a wall running up it.
    assert summary["rotation"] == 90
    assert all(placed["rotation"] == 90 for placed in be.inserts)


def test_a_grid_places_one_per_intersection():
    be = Recording(texts=["G-1", "G-7"])

    summary = ops.place_grid(be, Catalog.load().elements["stud"], [0, 6000, 12000], [0, 6000])

    assert summary["placed"] == 6
    assert len(be.inserts) == 6
    assert summary["tags"] == ["G-8", "G-9", "G-10", "G-11", "G-12", "G-13"]
    assert (12000.0, 6000.0) in summary["points"]


def test_an_element_with_no_tag_prefix_is_not_numbered():
    be = Recording()
    weld = Catalog.load().elements["weld_symbol"]

    summary = ops.place_along(be, weld, (0, 0), (3000, 0), count=2)

    assert summary["tags"] == []
    assert be.texts == []


def test_existing_tags_reads_the_texts_in_the_drawing():
    be = Recording(texts=["M-1"])

    assert ops.existing_tags(be) == ["M-1"]


def test_the_run_direction_is_measured_not_assumed():
    assert ops._run_direction((0, 0), (1000, 1000)) == pytest.approx(45.0)
    assert ops._run_direction((1000, 0), (0, 0)) == pytest.approx(180.0)
    assert geom.xy((1, 2)) == (1.0, 2.0)


# ---------------------------------------------------------------------------
# project settings
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "acadmcp.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_settings_are_found_by_walking_up_from_the_drawing(tmp_path, monkeypatch):
    _project(tmp_path, {"library": r"D:\院标"})
    deep = tmp_path / "drawings" / "floor-2"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    found = settings.find()

    assert found == tmp_path / "acadmcp.json"
    assert settings.current().library == r"D:\院标"


def test_no_project_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # Nothing to find is the ordinary case.
    assert settings.find() is None
    assert not settings.current().found
    assert settings.current().skipped == []

    # A path that was named but is not there is worth saying out loud: it means a typo or a
    # file that moved, not a drawing with no project.
    named = settings.load(tmp_path / "acadmcp.json")
    assert not named.found
    assert "no such settings file" in named.skipped[0]


def test_a_broken_settings_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "acadmcp.json"
    path.write_text("{oops", encoding="utf-8")

    loaded = settings.load(path)

    assert loaded.found and loaded.skipped and "acadmcp.json" in loaded.skipped[0]


def test_the_environment_beats_the_file_for_the_library_path(tmp_path, monkeypatch):
    path = _project(tmp_path, {"library": r"D:\院标"}) / "acadmcp.json"
    monkeypatch.setenv("ACADMCP_LIBRARY", r"E:\块库")

    assert settings.load(path).library == r"E:\块库"


def test_a_projects_catalogue_and_layers_are_merged_over_the_standard(tmp_path, monkeypatch):
    project = _project(
        tmp_path,
        {
            "catalog": ["our-elements.json"],
            "layers": {"A-DOOR": {"color": 1, "linetype": "DASHED"}, "我们的柱": [3, "CONTINUOUS"]},
        },
    )
    (project / "our-elements.json").write_text(
        json.dumps({"elements": [{"key": "door", "block": "我们的门", "source": "ours.dwg"}]}),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    loaded = settings.current()
    assert loaded.layers["A-DOOR"] == (1, "DASHED")
    assert loaded.layers["我们的柱"] == (3, "CONTINUOUS")
    # A relative catalogue path is relative to the settings file, not to the caller's cwd.
    assert loaded.catalogs == [str(project / "our-elements.json")]

    merged = catalog.current()
    assert merged.elements["door"].block == "我们的门"  # the project's, not the package's
    assert len(merged.elements) == SHIPPED  # and everything else inherited


def test_a_bad_layer_entry_is_reported(tmp_path):
    path = _project(tmp_path, {"layers": {"A-WALL": "red"}}) / "acadmcp.json"

    loaded = settings.load(path)

    assert loaded.layers == {}
    assert any("A-WALL" in note for note in loaded.skipped)


def test_a_project_layer_reaches_the_drawing(tmp_path, monkeypatch):
    project = _project(tmp_path, {"layers": {"我们的柱": [3, "CONTINUOUS"]}})
    monkeypatch.chdir(project)
    settings.reset()
    be = make_backend("dxf")

    created = ops.setup_layers(be)

    # The project's layer, and the standard set it did not replace.
    assert "我们的柱" in created and "A-WALL" in created
    assert "我们的柱" in be.info()["layers"]


# ---------------------------------------------------------------------------
# an entry may be drawn by an op where library blocks cannot go
# ---------------------------------------------------------------------------

_BOTH = {
    "key": "door",
    "label": "门",
    "block": "门 - 公制",
    "source": "lib.dwg",
    "layer": "A-DOOR",
    "width_param": "门尺寸",
    "op": "door_symbol",
    "params": {"墙厚": 150},
    "variants": {"900": {"params": {"门尺寸": 900}}},
}

_OP_ONLY = {
    "key": "column",
    "label": "柱",
    "layer": "A-COLUMN",
    "op": "column",
    "variants": {"600": {"params": {"size": 600}}},
}


def _catalog(tmp_path: Path, entries: list) -> Catalog:
    """A catalogue holding only these entries, so what a test sees is what it wrote."""
    path = tmp_path / "elements.json"
    path.write_text(json.dumps({"elements": entries}), encoding="utf-8")
    loaded = Catalog()
    loaded.add_file(path)
    return loaded


def test_an_entry_may_name_an_op_instead_of_a_block(tmp_path):
    loaded = _catalog(tmp_path, [_OP_ONLY])

    assert loaded.skipped == []
    element = loaded.get("column")
    assert element.op == "column" and element.block == "" and element.kind == "op"


def test_an_entry_with_neither_block_nor_op_is_skipped_and_counted(tmp_path):
    loaded = _catalog(tmp_path, [{"key": "ghost", "label": "?"}])

    assert loaded.get("ghost") is None
    assert len(loaded.skipped) == 1 and "neither a block nor an op" in loaded.skipped[0]


def test_kind_reports_block_op_or_both(tmp_path):
    assert _catalog(tmp_path, [_BOTH]).get("door").kind == "both"
    assert _catalog(tmp_path, [_OP_ONLY]).get("column").kind == "op"
    assert _catalog(tmp_path, [_OP_ONLY]).get("column").placement == "parametric"
    assert _catalog(tmp_path, [_BOTH]).get("door").placement == "both"


def test_placement_prefers_the_block_when_the_backend_can_insert(tmp_path):
    door = _catalog(tmp_path, [_BOTH]).get("door")

    assert door.placement_for(can_insert=True) == "block"
    assert door.placement_for(can_insert=False) == "op"


def test_placement_falls_back_to_the_op_for_an_op_only_entry(tmp_path):
    column = _catalog(tmp_path, [_OP_ONLY]).get("column")

    assert column.placement_for(can_insert=True) == "op"


def test_placement_refuses_a_block_only_entry_on_a_backend_without_insert(tmp_path):
    """The message has to say how to recover: this is the whole failure of the catalogue
    having been plugin-only."""
    block_only = {"key": "door", "block": "B", "source": "lib.dwg"}
    door = _catalog(tmp_path, [block_only]).get("door")

    with pytest.raises(ValueError) as caught:
        door.placement_for(can_insert=False)

    assert "op" in str(caught.value) and "plugin" in str(caught.value)


def test_the_width_key_is_the_entrys_own_or_width_for_an_op(tmp_path):
    # An op adapter is written to take `width`, so an op entry need only say which
    # parameter carries one when it disagrees; a block entry must say, because only the
    # catalogue knows that this block calls it 门尺寸.
    assert _catalog(tmp_path, [_BOTH]).get("door").width_key == "门尺寸"


def test_an_op_entry_defaults_the_width_key_to_width(tmp_path):
    assert _catalog(tmp_path, [_OP_ONLY]).get("column").width_key == "width"


def test_an_entry_that_cannot_express_a_width_reports_no_key(tmp_path):
    block_only = {"key": "door", "block": "B", "source": "lib.dwg"}
    assert _catalog(tmp_path, [block_only]).get("door").width_key == ""


def test_check_against_does_not_report_op_only_entries(tmp_path):
    """Nothing in a library stands for an entry drawn parametrically, so there is nothing
    to compare — and reporting it as 'unchecked' would be noise in every project."""
    loaded = _catalog(tmp_path, [_OP_ONLY, _BOTH])

    assert loaded.check_against({"lib.dwg": ["门 - 公制"]}) == []


# ---------------------------------------------------------------------------
# provenance: origin rides describe() but never digest()
# ---------------------------------------------------------------------------


def _origin_element(**over) -> Element:
    """A captured entry, the shape whose `origin` carries where it came from."""
    base = dict(
        key="captured",
        label="captured",
        category="captured",
        block="",
        source="",
        layer="A-DOOR",
        op="insert_captured",
        params={"block": "ACADMCP_CAPTURED"},
    )
    base.update(over)
    return Element(**base)


def test_origin_is_carried_by_describe():
    element = _origin_element(
        origin={"source_drawing": "a.dxf", "steps": [1, 2], "fingerprint": "sha256:f"}
    )

    assert element.describe()["origin"]["steps"] == [1, 2]
    assert element.describe()["origin"]["source_drawing"] == "a.dxf"


def test_a_file_authored_entry_has_an_empty_origin(tmp_path):
    """A hand-written entry names no source drawing, so its origin is `{}` — not absent, so a
    reader never has to tell "no provenance" from "provenance that could not be read"."""
    loaded = _catalog(tmp_path, [{"key": "k", "block": "B"}])

    assert loaded.get("k").origin == {}
    assert loaded.get("k").describe()["origin"] == {}


def test_origin_survives_a_describe_adopt_round_trip():
    """The whole point of the field: the entry embedded in a drawing keeps its provenance, so
    a machine opening the file elsewhere can trace where the element came from."""
    loaded = Catalog()
    loaded.register(
        {
            "key": "x",
            "op": "insert_captured",
            "params": {"block": "ACADMCP_X"},
            "origin": {
                "source_drawing": "a.dxf",
                "steps": [3, 4],
                "captured_at": "2026-10-15T01:00:00",
                "fingerprint": "sha256:abc",
            },
        }
    )

    adopted = Catalog.adopt(loaded.identity())

    assert adopted.get("x").origin["steps"] == [3, 4]
    assert adopted.get("x").origin["fingerprint"] == "sha256:abc"


def test_origin_survives_a_file_load_round_trip(tmp_path):
    path = _write(
        tmp_path,
        {
            "elements": [
                {
                    "key": "k",
                    "block": "B",
                    "origin": {
                        "source_drawing": "a.dxf",
                        "steps": [7],
                        "captured_at": "t",
                        "fingerprint": "sha256:zzz",
                    },
                }
            ]
        },
    )

    loaded = Catalog.load([path])

    assert loaded.get("k").origin["source_drawing"] == "a.dxf"
    assert loaded.get("k").origin["steps"] == [7]


def test_digest_ignores_origin():
    """Provenance is not placement semantics: a stamped entry and its file twin must hash
    alike, or every drawing drawn before the field existed reports a spurious
    `catalog_warning` the moment it is reopened."""
    one = Catalog({"x": _origin_element(origin={"fingerprint": "a"})})
    two = Catalog({"x": _origin_element(origin={"fingerprint": "b", "steps": [1, 2]})})

    assert one.digest() == two.digest()
    assert one.digest() == Catalog({"x": _origin_element()}).digest()
    # ...and the digest still moves when the placement semantics do.
    assert one.digest() != Catalog({"x": _origin_element(layer="A-OTHER")}).digest()
