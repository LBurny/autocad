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
    assert loaded.keys() == [
        "beam_i",
        "beam_wf",
        "channel",
        "door",
        "light",
        "stud",
        "weld_symbol",
        "window",
        "window_elevation",
    ]


def test_every_entry_names_a_library_and_a_block():
    loaded = Catalog.load()

    for key, element in loaded.elements.items():
        assert element.source.endswith(".dwg"), key
        assert element.block, key
        assert "{library}" not in element.source, key
        assert element.layer, key


def test_a_library_path_can_be_pointed_somewhere_else(tmp_path, monkeypatch):
    monkeypatch.setenv("ACADMCP_LIBRARY", r"D:\院标\块库")
    loaded = Catalog.load()

    assert all(e.source.startswith(r"D:\院标\块库") for e in loaded.elements.values())


# ---------------------------------------------------------------------------
# loading what is there, and reporting what is not
# ---------------------------------------------------------------------------


def test_a_missing_file_is_reported_rather_than_raised():
    loaded = Catalog.load(["Z:/nowhere/elements.json"])

    assert len(loaded.elements) == 9
    assert any("no such catalogue file" in note for note in loaded.skipped)


def test_a_file_that_is_not_json_is_reported(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    loaded = Catalog.load([path])

    assert len(loaded.elements) == 9
    assert any("broken.json" in note for note in loaded.skipped)


def test_a_payload_that_is_not_a_list_is_reported(tmp_path):
    loaded = Catalog.load([_write(tmp_path, {"elements": "nope"})])

    assert len(loaded.elements) == 9
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
                {"key": "noblock"},  # nothing to place
                {"key": "novariants", "block": "B", "variants": "nope"},
            ],
        },
    )

    loaded = Catalog.load([path])

    assert "good" in loaded.elements
    assert loaded.elements["good"].source == r"D:\lib\a.dwg"
    assert "novariants" in loaded.elements and loaded.elements["novariants"].variants == {}
    assert len(loaded.skipped) == 4
    assert any("no block to place" in note for note in loaded.skipped)


def test_a_later_file_replaces_an_entry_of_the_same_key(tmp_path):
    path = _write(
        tmp_path,
        {"elements": [{"key": "door", "block": "我们的门", "source": "x.dwg", "layer": "A-DOOR"}]},
    )

    loaded = Catalog.load([path])

    assert loaded.elements["door"].block == "我们的门"
    assert len(loaded.elements) == 9


def test_a_bare_variant_value_becomes_its_parameter(tmp_path):
    path = _write(
        tmp_path, {"elements": [{"key": "k", "block": "B", "variants": {"900": 900}}]}
    )

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

    def __init__(self, texts: list[str] | None = None) -> None:
        self.inserts: list[dict] = []
        self.texts: list[dict] = []
        self.existing = list(texts or [])

    def insert(self, block, at, **kwargs):
        self.inserts.append({"block": block, "at": at, **kwargs})
        return len(self.inserts) - 1

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
    assert len(merged.elements) == 9  # and everything else inherited


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
