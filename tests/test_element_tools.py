"""The element tools, driven against a plugin client that records instead of drawing.

The drawing itself is exercised against real AutoCAD by ``live_rpc_check.py``. What is
tested here is the wiring between the catalogue, the ops and the step boundary, which is
where a placement can go wrong without AutoCAD's help: a size that never reaches the block,
a tag numbered off the wrong list, or — the one this file was written after — a helper
calling itself instead of the function it wraps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import acadmcp.server as server  # noqa: E402
from acadmcp import catalog  # noqa: E402
from acadmcp.acad_plugin_client import PluginUnavailable  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402
from acadmcp.backends.plugin import PluginBackend  # noqa: E402
from acadmcp.catalog import Catalog, Element, Variant  # noqa: E402


class FakePlugin:
    """Answers the plugin's wire protocol, keeping what the client sent."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self.texts = list(texts or [])
        self.steps: list[dict[str, Any]] = []

    def call(self, method: str, **params: Any) -> Any:
        if method == "apply":
            self.steps.append(params["step"])
            return {
                "result": {
                    "ok": True,
                    "action": "apply",
                    "message": "applied",
                    "step": {
                        "handles": ["1", "2"],
                        "primitives": len(params["step"]["primitives"]),
                    },
                },
                "status": {"count": len(self.steps)},
            }
        if method == "entities":
            return {"entities": [{"text": text} for text in self.texts]}
        raise AssertionError(f"unexpected call: {method}")


@pytest.fixture()
def plugin():
    catalog_client = FakePlugin()
    be = PluginBackend()
    be.client = catalog_client
    server._state.reset(be, "plugin")
    yield catalog_client
    server._state.reset(None, None)


@pytest.fixture()
def fresh_dxf():
    """A headless session, with no AutoCAD anywhere near it."""
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def _op_element() -> Element:
    """An op-only column, the shape a project's catalogue carries for one.

    No library block at all, so the headless backends can draw it, and a tag prefix to
    number from — the numbering is derived from the drawing's own tags, never stored.
    """
    return Element(
        key="column",
        label="柱",
        category="structure",
        block="",
        source="",
        layer="A-COLUMN",
        op="column",
        width_param="",
        variants={"600": Variant(name="600", params={"size": 600})},
        tag_prefix="G",
        default_variant="600",
    )


def _inserts(client: FakePlugin) -> list[dict[str, Any]]:
    return [
        primitive
        for step in client.steps
        for primitive in step["primitives"]
        if primitive["kind"] == "insert"
    ]


def _texts(client: FakePlugin) -> list[str]:
    return [
        primitive["text"]
        for step in client.steps
        for primitive in step["primitives"]
        if primitive["kind"] == "text"
    ]


def test_placing_one_element_sets_the_variant_size_on_the_block(plugin):
    result = server.place_element("door", at=[1000, 2000], variant="800", rotation=90, tag=False)

    assert result["ok"]
    assert len(plugin.steps) == 1
    placed = _inserts(plugin)[0]
    assert placed["block"] == "门 - 公制"
    assert placed["points"] == [1000.0, 2000.0]
    assert placed["rotation"] == 90
    assert placed["properties"] == {"墙厚": 150, "门尺寸": 800}
    assert _texts(plugin) == []


def test_placing_a_run_numbers_from_the_tags_the_drawing_has(plugin):
    plugin.texts = ["M-1", "M-4"]

    result = server.place_element("door", start=[0, 0], end=[6000, 0], count=3)

    assert result["placed"] == 3
    assert result["tags"] == ["M-5", "M-6", "M-7"]
    assert _texts(plugin) == ["M-5", "M-6", "M-7"]
    assert all(placed["properties"]["门尺寸"] == 900 for placed in _inserts(plugin))


def test_a_run_lies_along_its_wall(plugin):
    result = server.place_element("window", start=[0, 0], end=[0, 6000], count=2, variant="1200")

    assert result["rotation"] == 90
    assert all(placed["rotation"] == 90 for placed in _inserts(plugin))
    assert all(placed["properties"]["DoorWidth"] == 1200 for placed in _inserts(plugin))


def test_a_grid_places_one_element_per_intersection(plugin):
    result = server.place_element("stud", xs=[0, 6000], ys=[10, 20, 30])

    assert result["placed"] == 6
    assert len(_inserts(plugin)) == 6


def test_a_variant_that_does_not_exist_is_refused_not_substituted(plugin):
    with pytest.raises(BackendError, match="no variant '950'"):
        server.place_element("door", at=[0, 0], variant="950")

    # Nothing was drawn and no step was left open for the next call to trip over.
    assert plugin.steps == []
    assert server._state.backend.open_step == ""


def test_an_element_that_is_not_in_the_catalogue_is_refused_with_the_list(plugin):
    with pytest.raises(BackendError, match="no element 'chair'.*beam_wf"):
        server.place_element("chair", at=[0, 0])


def test_element_types_reports_a_library_it_cannot_read(plugin, monkeypatch):
    # The plugin is stubbed out rather than merely assumed absent. Assuming was the bug:
    # with AutoCAD open and the plugin loaded — the normal state while working on this
    # package — the catalogue really can be checked, `matches_library` comes back True,
    # and this test failed for being run on a machine that had AutoCAD up. What is under
    # test is the answer when the library cannot be read, so that is what is arranged.
    class Dead:
        def call(self, method: str, **params: Any) -> Any:
            raise PluginUnavailable(f"{method}: nothing listening on 127.0.0.1:9880")

    monkeypatch.setattr(server, "_client", Dead)

    listed = server.element_types()

    assert listed["ok"]
    assert listed["count"] == len(Catalog.load().elements)
    assert listed["skipped"] == []
    # "Could not check" must never come back as "fine": a catalogue that has drifted
    # from its library places the wrong block, and silence is how that ships.
    assert listed["matches_library"] is False
    assert listed["problems"]
    assert "could not read" in listed["problems"][0]
    assert "127.0.0.1:9880" in listed["problems"][0]


def test_element_types_reports_autocad_being_unreachable(plugin, monkeypatch):
    def no_plugin() -> Any:
        raise PluginUnavailable("nothing listening")

    monkeypatch.setattr(server, "_client", no_plugin)

    listed = server.element_types()

    # A different branch: nothing to talk to at all, rather than a library that would
    # not open. Both have to say "unchecked" instead of reporting a clean catalogue.
    assert listed["matches_library"] is False
    assert listed["problems"] == ["AutoCAD is not reachable, so the catalogue could not be checked"]


def test_element_types_can_skip_the_library_check(plugin, monkeypatch):
    def no_plugin() -> Any:
        raise PluginUnavailable("nothing listening")

    monkeypatch.setattr(server, "_client", no_plugin)

    listed = server.element_types(check_library=False)

    assert "problems" not in listed and "matches_library" not in listed


def test_a_placement_without_the_plugin_is_refused_rather_than_drawn_elsewhere():
    server._state.reset(None, None)

    with pytest.raises(BackendError, match="acad_connect"):
        server.place_element("door", at=[0, 0])


# --------------------------------------------------------------------------- #
# the same three tools, headless
# --------------------------------------------------------------------------- #


def test_place_element_works_on_the_headless_backend(fresh_dxf, monkeypatch):
    """Was `_require_plugin()`: an AI writing a plan headless could not place a single
    door from the catalogue, which is the fastest path there is — no AutoCAD, previewable."""
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )

    out = server.place_element("column", at=[0, 0], variant="600")

    assert out["ok"] and out["placed"] == 1
    assert server._state.backend.info()["entities"] > 0


def test_place_element_is_one_undoable_step_on_dxf(fresh_dxf, monkeypatch):
    """One tool call is one journal step on every backend, which is what makes a wrong
    coordinate cost one drawing_undo instead of a redraw."""
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )

    server.place_element("column", at=[0, 0])

    assert [s.op for s in server._state.backend.journal.steps] == ["place_element"]
    report = server.drawing_undo()
    assert report["erased"] > 0 and server._state.backend.info()["entities"] == 0


def test_a_run_on_dxf_lays_several_and_numbers_them(fresh_dxf, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )

    out = server.place_element("column", start=[0, 0], end=[6000, 0], count=3)

    assert out["ok"] and out["placed"] == 3
    assert out["tags"] == ["G-1", "G-2", "G-3"]


def test_a_second_placement_continues_the_numbering(fresh_dxf, monkeypatch):
    """Numbering is derived from the drawing's own tags, and on dxf it was derived from
    nothing: `entities()` reported no text, so every call restarted at 1 and two runs of
    doors shared the tags G-1 and G-2."""
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )

    server.place_element("column", start=[0, 0], end=[6000, 0], count=2)
    second = server.place_element("column", start=[0, 3000], end=[6000, 3000], count=2)

    assert second["tags"] == ["G-3", "G-4"]


def test_place_element_reports_the_tag_it_drew(fresh_dxf, monkeypatch):
    """the run and grid placements answer with their `tags`; a single placement answered
    only `placed: 1`, so the caller had to list entities to learn the number the tool
    had just drawn."""
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )

    out = server.place_element("column", at=[0, 0], variant="600")

    assert out["tag"] == "G-1"


def test_place_element_with_tag_false_reports_no_tag(fresh_dxf, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )

    out = server.place_element("column", at=[0, 0], variant="600", tag=False)

    assert out["tag"] is None
