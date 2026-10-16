"""Publishing a captured element: the entry *and* its geometry into the project library.

An entry without its geometry is a name that resolves to nothing: the block definition a
capture wrote lives in the drawing that captured it, and a fresh drawing has never seen it.
So publishing writes two things — the catalogue entry and a geometry fragment under
``library/`` — and `insert_captured` learns to import the fragment when the definition is
absent.

No AutoCAD needed. The whole loop is headless: capture in drawing A, publish, then place the
element in a brand-new drawing and check the geometry matches A's.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import capture_ops, catalog, ops, server, settings  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402
from acadmcp.catalog import Catalog, Element  # noqa: E402

KEY = "rollup_door"
BLOCK = "ACADMCP_ROLLUP_DOOR"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """An empty working directory and a catalogue of its own, so a publish never touches the
    package starter or a project's real files and cannot leak between tests."""
    monkeypatch.chdir(tmp_path)
    settings.reset()
    catalog.use(Catalog())
    yield
    catalog.reset()
    settings.reset()


def _capture_door(be, key: str = KEY):
    """A wall run and a door in it, captured as one element — the shape publish works on."""
    with be.step("draw_wall"):
        ops.draw_wall_run(be, [(0, 0), (6000, 0)], 200)
    with be.step("draw_door"):
        ops.draw_door(be, (1000, 0), 900, 200)
    with be.step("element_capture", params={"key": key}):
        return capture_ops.capture(be, steps=[1, 2], key=key, layer="A-DOOR")


def _fragment(be, element) -> dict:
    """The geometry fragment publish needs, gathered the way the tool wrapper gathers it: the
    definition's own journal record plus the fingerprint the capture stamped."""
    found = be.find_block_primitives(element.params["block"])
    assert found is not None
    return {**found, "fingerprint": element.origin["fingerprint"]}


def _draw_another_door(be) -> list[int]:
    """A second element with geometry of its own — a wall and a 700 door on it — and the steps
    it drew, so a test captures two distinct elements into one session."""
    with be.step("draw_wall"):
        ops.draw_wall_run(be, [(0, 5000), (6000, 5000)], 200)
    with be.step("draw_door"):
        ops.draw_door(be, (1000, 5000), 700, 200)
    return [step.index for step in be.journal.steps[-2:]]


# ---------------------------------------------------------------------------
# what a publish writes
# ---------------------------------------------------------------------------


def test_publish_writes_the_entry_and_the_fragment(tmp_path):
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)

    report = catalog.current().publish_to(
        str(tmp_path / "elements.json"), entry, _fragment(be, entry)
    )

    assert report["ok"] is True
    assert Path(report["file"]) == tmp_path / "elements.json"
    assert Path(report["fragment"]).exists()
    assert Path(report["fragment"]).name == f"{KEY}.dxf"
    assert report["key"] == KEY
    assert report["digest"].startswith("sha256:")

    payload = json.loads((tmp_path / "elements.json").read_text(encoding="utf-8"))
    assert [e["key"] for e in payload["elements"]] == [KEY]
    assert payload["elements"][0]["origin"]["geometry_file"] == f"library/{KEY}.dxf"


def test_publish_creates_both_files_in_the_working_directory():
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)

    report = catalog.current().publish_to("", entry, _fragment(be, entry))

    project = Path.cwd()
    assert (project / "elements.json").is_file()
    assert (project / "acadmcp.json").is_file()
    assert json.loads((project / "acadmcp.json").read_text(encoding="utf-8")) == {
        "catalogs": ["elements.json"]
    }
    assert set(report["created"]) == {
        str(project / "elements.json"),
        str(project / "acadmcp.json"),
    }


def test_publish_appends_and_preserves_the_comment(tmp_path):
    target = tmp_path / "elements.json"
    target.write_text(
        json.dumps(
            {
                "_comment": "our office library",
                "elements": [{"key": "existing", "block": "B", "source": "x.dwg"}],
            }
        ),
        encoding="utf-8",
    )
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)

    catalog.current().publish_to(str(target), entry, _fragment(be, entry))

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["_comment"] == "our office library"
    # Appended, never re-sorted: the order a person wrote stands, ours goes last.
    assert [e["key"] for e in payload["elements"]] == ["existing", KEY]


def test_publish_writes_the_geometry_fragment_as_a_block_definition(tmp_path):
    be = make_backend("dxf")
    captured = _capture_door(be)
    entry = catalog.current().get(KEY)

    report = catalog.current().publish_to(
        str(tmp_path / "elements.json"), entry, _fragment(be, entry)
    )

    name, primitives, base_point = capture_ops.import_fragment(report["fragment"])
    assert name == BLOCK
    assert capture_ops.fingerprint_primitives(primitives) == entry.origin["fingerprint"]
    # The base point the capture chose — a door with no explicit one gets its geometry's min
    # corner — travels with the fragment, so a placement means the same point it did in A.
    assert base_point == captured["base_point"]


# ---------------------------------------------------------------------------
# the refusals, each naming what is missing
# ---------------------------------------------------------------------------


def test_publish_refuses_a_key_the_target_file_already_has(tmp_path):
    target = tmp_path / "elements.json"
    target.write_text(
        json.dumps({"elements": [{"key": KEY, "block": "B", "source": "x.dwg"}]}), encoding="utf-8"
    )
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)

    with pytest.raises(ValueError, match="already has an entry"):
        catalog.current().publish_to(str(target), entry, _fragment(be, entry))


def test_publish_refuses_a_key_not_in_the_catalogue_in_force(tmp_path):
    ghost = Element(
        key="ghost",
        label="ghost",
        category="captured",
        block="",
        source="",
        layer="",
        op="insert_captured",
        params={"block": "B"},
    )

    with pytest.raises(ValueError, match="not in the catalogue in force"):
        catalog.current().publish_to(str(tmp_path / "elements.json"), ghost, {})


def test_publish_refuses_when_the_drawing_has_no_definition_record(tmp_path):
    """The definition a capture wrote lives in the drawing that captured it; publishing from a
    drawing that never saw it has nothing to write, and says so."""
    catalog.current().register(
        {"key": "ghost", "op": "insert_captured", "params": {"block": "ACADMCP_GHOST"}}
    )
    entry = catalog.current().get("ghost")

    with pytest.raises(ValueError, match="drawing that captured it"):
        catalog.current().publish_to(str(tmp_path / "elements.json"), entry, None)


def test_publish_says_the_capture_step_is_undone(tmp_path):
    """Undo the capture and the finder stops seeing the record, so this arrives as the same
    missing fragment as a drawing that never had the definition. The caller knows which it was
    — the drawing still records the capture, undone — and the recovery is different."""
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)
    assert _fragment(be, entry)  # the record is there while the capture step stands

    be.undo(1)

    assert be.find_block_primitives(entry.params["block"]) is None
    with pytest.raises(ValueError, match="capture step that made 'rollup_door' is undone"):
        catalog.current().publish_to(
            str(tmp_path / "elements.json"), entry, None, capture_undone=True
        )


def test_publish_does_not_claim_an_undo_it_cannot_see(tmp_path):
    """Without the caller saying an undo is what took the definition away, the message must not
    invent one: another drawing's capture is not this drawing's undone step."""
    catalog.current().register(
        {"key": "elsewhere", "op": "insert_captured", "params": {"block": "ACADMCP_ELSEWHERE"}}
    )
    entry = catalog.current().get("elsewhere")

    with pytest.raises(ValueError) as caught:
        catalog.current().publish_to(str(tmp_path / "elements.json"), entry, None)

    assert "drawing that captured it" in str(caught.value)
    assert "undone" not in str(caught.value)


def test_publishing_the_same_key_twice_is_refused(tmp_path):
    be = make_backend("dxf")
    _capture_door(be)
    target = str(tmp_path / "elements.json")
    catalog.current().publish_to(
        target, catalog.current().get(KEY), _fragment(be, catalog.current().get(KEY))
    )

    # The file now holds the key, so the in-force catalogue is reloaded from it.
    catalog.use(Catalog.load([target]))
    entry = catalog.current().get(KEY)

    with pytest.raises(ValueError, match="already has an entry"):
        catalog.current().publish_to(target, entry, _fragment(be, entry))


# ---------------------------------------------------------------------------
# what a publish must not take away
# ---------------------------------------------------------------------------


def test_publish_keeps_the_captures_the_session_has_not_written_anywhere(tmp_path):
    """Publishing forgets the settings cache so the next load reads the file just written —
    but the catalogue in force is the *session's*, and it holds captures no file has. Dropping
    it lost every one of them, and the entry just published too whenever the target was not in
    the search path — while the tool still reported ok."""
    be = make_backend("dxf")
    _capture_door(be)
    steps = _draw_another_door(be)
    with be.step("element_capture", params={"key": "other_door"}):
        capture_ops.capture(be, steps=steps, key="other_door", layer="A-DOOR")
    entry = catalog.current().get(KEY)

    report = catalog.current().publish_to(
        str(tmp_path / "elements.json"), entry, _fragment(be, entry)
    )

    assert report["ok"] is True
    loaded = catalog.current()
    assert loaded.get(KEY) is not None
    assert loaded.get("other_door") is not None
    # The published key now resolves to the file's own copy — the one that travels — in place
    # of its runtime twin, so one key holds one entry and not two.
    assert loaded.source_file(KEY) == Path(report["file"])
    assert loaded.get(KEY).origin["geometry_file"] == f"library/{KEY}.dxf"
    assert not loaded.get("other_door").origin.get("geometry_file")


def test_publishing_into_the_project_file_replaces_the_runtime_copy(tmp_path):
    """The other half of the same rule: when the target *is* in the search path the file's
    entry and the session's are the same key, so the file's wins outright."""
    (tmp_path / "acadmcp.json").write_text(
        json.dumps({"catalog": ["elements.json"]}), encoding="utf-8"
    )
    (tmp_path / "elements.json").write_text(
        json.dumps({"_comment": "ours", "elements": []}), encoding="utf-8"
    )
    settings.reset()
    catalog.use(Catalog.load([tmp_path / "elements.json"]))
    be = make_backend("dxf")
    _capture_door(be)

    report = catalog.current().publish_to(
        "", catalog.current().get(KEY), _fragment(be, catalog.current().get(KEY))
    )

    assert report["file"] == str(tmp_path / "elements.json")
    payload = json.loads((tmp_path / "elements.json").read_text(encoding="utf-8"))
    assert [e["key"] for e in payload["elements"]] == [KEY]  # one entry, not two
    loaded = catalog.current()
    assert [key for key in loaded.elements if key == KEY] == [KEY]
    assert loaded.source_file(KEY) == Path(report["file"])
    # A fresh load of that file agrees, so nothing was only ever in memory.
    assert Catalog.load([report["file"]]).get(KEY).origin["geometry_file"] == f"library/{KEY}.dxf"


# ---------------------------------------------------------------------------
# what the file loads back as
# ---------------------------------------------------------------------------


def test_the_published_file_loads_back_with_its_origin(tmp_path):
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)

    report = catalog.current().publish_to(
        str(tmp_path / "elements.json"), entry, _fragment(be, entry)
    )

    reloaded = Catalog.load([report["file"]])
    again = reloaded.get(KEY)
    assert again is not None
    assert again.op == "insert_captured"
    assert again.origin["fingerprint"] == entry.origin["fingerprint"]
    assert again.origin["steps"] == [1, 2]
    assert again.origin["geometry_file"] == f"library/{KEY}.dxf"
    assert reloaded.source_file(KEY) == Path(report["file"])


# ---------------------------------------------------------------------------
# the loop: capture in A, publish, place in a fresh drawing
# ---------------------------------------------------------------------------


def _block_geometry(block) -> Counter:
    """A Counter of a block definition's entities, as (dxftype, layer, rounded points).

    A comparison of the two drawings' *blocks*, not a primitive mapping: it reads the entities
    each definition holds, so it proves the geometry that landed in the fresh drawing is the
    geometry drawing A captured — which is the whole claim of publishing.
    """
    rows = []
    for entity in block:
        points = []
        for key in ("start", "end", "center", "insert"):
            if entity.dxf.hasattr(key):
                value = entity.dxf.get(key)
                points.append((round(float(value[0]), 1), round(float(value[1]), 1)))
        rows.append((entity.dxftype(), entity.dxf.get("layer", "0"), tuple(points)))
    return Counter(rows)


def test_the_whole_loop_places_a_published_element_in_a_fresh_drawing(tmp_path):
    """The accumulation the whole phase exists for, proven end to end: drawing A captures a
    door and publishes it; a brand-new drawing that has never seen the block places it by key,
    and the geometry that lands is A's."""
    source = make_backend("dxf")
    _capture_door(source)
    entry = catalog.current().get(KEY)
    fingerprint = entry.origin["fingerprint"]

    report = catalog.current().publish_to(
        str(tmp_path / "elements.json"), entry, _fragment(source, entry)
    )

    # A fresh session: the published catalogue in force, a drawing that holds no block yet.
    catalog.use(Catalog.load([report["file"]]))
    fresh_drawing = make_backend("dxf")
    assert BLOCK not in fresh_drawing.doc.blocks

    with fresh_drawing.step("place_element"):
        ops.place_element(fresh_drawing, catalog.current().get(KEY), (0, 0))

    refs = [e for e in fresh_drawing.msp if e.dxftype() == "INSERT"]
    assert len(refs) == 1 and refs[0].dxf.name == BLOCK
    # The definition was imported from the fragment, and it is A's geometry.
    assert BLOCK in fresh_drawing.doc.blocks
    assert _block_geometry(fresh_drawing.doc.blocks.get(BLOCK)) == _block_geometry(
        source.doc.blocks.get(BLOCK)
    )
    _, primitives, _ = capture_ops.import_fragment(report["fragment"])
    assert capture_ops.fingerprint_primitives(primitives) == fingerprint


# ---------------------------------------------------------------------------
# the tool surface
# ---------------------------------------------------------------------------


@pytest.fixture
def _tool_session():
    """A fresh dxf drawing behind the tool surface, dropped again afterwards."""
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_element_publish_closes_the_loop_over_the_tool_surface():
    """Tool level: capture, publish, then a fresh drawing places the element by key.

    This is the whole point of publishing — the accumulation is only real if the tools carry
    it, not just the module underneath. `catalog.reset()` stands in for the fresh process a
    project file is written for: the next load reads `./elements.json` publish created, the
    entry arrives with its geometry fragment, and `place_element` imports the definition.
    """
    server.draw_wall(points=[[0, 0], [6000, 0]], thickness=200)
    server.draw_door(hinge=[1000, 0], width=900)
    server.element_capture(steps=[1, 2], key="loop_door", layer="A-DOOR")

    report = server.element_publish(key="loop_door")

    assert report["ok"] is True
    assert Path(report["file"]).is_file()
    assert Path(report["fragment"]).is_file()

    catalog.reset()  # a new process, reading the project file publish just wrote
    server.drawing_new(backend="dxf")
    placed = server.place_element(type="loop_door", at=[1000, 1000], tag=False)

    assert placed["ok"] is True
    be = server._state.backend
    assert [e.dxf.name for e in be.msp if e.dxftype() == "INSERT"] == ["ACADMCP_LOOP_DOOR"]
    assert server.drawing_verify()["verdict"] == "matches"


def test_element_publish_refuses_a_key_that_is_not_in_the_catalogue(_tool_session):
    with pytest.raises(BackendError, match="no element"):
        server.element_publish(key="never_captured")


def test_element_publish_refuses_a_key_this_drawing_never_captured(_tool_session):
    """The entry exists but the definition does not live here: refused, naming what is missing."""
    catalog.current().register(
        {
            "key": "elsewhere",
            "label": "elsewhere",
            "op": "insert_captured",
            "params": {"block": "ACADMCP_ELSEWHERE"},
        }
    )

    with pytest.raises(ValueError, match="lives in the drawing that captured it"):
        server.element_publish(key="elsewhere")


def test_element_publish_names_the_undo_when_the_capture_was_undone(_tool_session):
    """Tool level: the undone capture and the never-captured definition both arrive at
    publish_to as no fragment, and the recoveries differ — the wire has to say which."""
    server.draw_wall(points=[[0, 0], [6000, 0]], thickness=200)
    server.draw_door(hinge=[1000, 0], width=900)
    server.element_capture(steps=[1, 2], key="undone_door", layer="A-DOOR")
    server.drawing_undo()

    with pytest.raises(ValueError) as caught:
        server.element_publish(key="undone_door")

    assert "undone" in str(caught.value)
    assert "drawing_redo" in str(caught.value)


def test_a_capture_the_project_catalogue_lacks_survives_a_reopen(tmp_path, _tool_session):
    """Two processes: capture in a project that has a catalogue of its own, never publish, save,
    reopen. The project's files win the precedence — and the drawing's own capture is still
    placeable, because the entries it carries that the project has not got are adopted on open.
    Without that they were dropped while their block definition stayed in the file, so the
    element could be neither placed nor captured again."""
    (tmp_path / "acadmcp.json").write_text(
        json.dumps({"catalog": ["elements.json"]}), encoding="utf-8"
    )
    (tmp_path / "elements.json").write_text(
        json.dumps({"elements": [{"key": "project_door", "block": "P", "source": "ours.dwg"}]}),
        encoding="utf-8",
    )
    settings.reset()
    catalog.reset()
    server.drawing_new(backend="dxf")
    server.draw_wall(points=[[0, 0], [6000, 0]], thickness=200)
    server.draw_door(hinge=[1000, 0], width=900)
    server.element_capture(steps=[1, 2], key="orphan_door", layer="A-DOOR")
    path = str(tmp_path / "plan.dxf")
    server.drawing_save(path=path)

    # A second process: nothing loaded, and the file reopened inside the project.
    catalog.reset()
    server._state.backend = None
    report = server.drawing_open(path)

    assert report["catalog_source"] == "project"
    assert catalog.current().get("project_door") is not None  # the local standard still wins
    assert catalog.current().get("orphan_door") is not None
    placed = server.place_element(type="orphan_door", at=[0, 0], tag=False)
    assert placed["ok"] is True
    be = server._state.backend
    assert [e.dxf.name for e in be.msp if e.dxftype() == "INSERT"] == ["ACADMCP_ORPHAN_DOOR"]


# ---------------------------------------------------------------------------
# element_types says whether a captured entry can leave its drawing
# ---------------------------------------------------------------------------


def _element_row(report, key):
    return next(row for rows in report["categories"].values() for row in rows if row["key"] == key)


def test_element_types_reports_a_captured_entry_is_not_placeable_elsewhere():
    be = make_backend("dxf")
    _capture_door(be)
    row = _element_row(server.element_types(check_library=False), KEY)
    assert row["fragment_ready"] is False


def test_element_types_reports_the_fragment_once_published(tmp_path):
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)
    catalog.current().publish_to(str(tmp_path / "elements.json"), entry, _fragment(be, entry))
    row = _element_row(server.element_types(check_library=False), KEY)
    assert row["fragment_ready"] is True


def test_an_entry_with_no_fragment_carries_no_fragment_flag():
    catalog.current().register(
        {"key": "plain_col", "category": "structure", "op": "column", "params": {}}
    )
    row = _element_row(server.element_types(check_library=False), "plain_col")
    assert "fragment_ready" not in row


def test_a_drawing_adopted_entry_resolves_its_fragment_beside_the_drawing(tmp_path):
    """Publish beside the drawing, reopen the drawing, then place from the adopted
    catalogue — the capture → publish → reopen → place flow the cc-journal sandbox
    exercised. The embedded entry has no catalogue *file* behind it, but the publish
    convention (library/ beside the elements.json beside the drawing) lets the
    drawing's own directory stand in as the resolution base."""
    be = make_backend("dxf")
    _capture_door(be)
    entry = catalog.current().get(KEY)
    catalog.current().publish_to(str(tmp_path / "elements.json"), entry, _fragment(be, entry))
    be.note_catalog(catalog.current())
    be.save(str(tmp_path / "a.dxf"))

    # A fresh session reloads from files alone, and nothing names tmp_path/elements.json
    # (publish with an explicit path creates no acadmcp.json), so the entry now reaches
    # the session only through the drawing's embedded copy.
    catalog.reset()
    settings.reset()
    reopened = make_backend("dxf", path=str(tmp_path / "a.dxf"))
    embedded = reopened.embedded_catalog()
    assert embedded is not None and embedded.get(KEY) is not None
    catalog.use(embedded)

    assert embedded.fragment_ready(embedded.get(KEY)) is True

    fresh = make_backend("dxf")
    with fresh.step("place_element"):
        ops.place_element(fresh, embedded.get(KEY), (500.0, 500.0))
    assert [e for e in fresh.msp if e.dxftype() == "INSERT"]
