"""Element capture: turning verified geometry into a reusable catalogue element.

No AutoCAD needed. What is worth testing here is that a captured block definition is
*document state* — it survives an undo of the step that captured it and it travels inside
the saved drawing — and that every refusal is honest: a duplicate key, a sheet primitive or
a backend that cannot write a definition names its own fault rather than drawing something
that only looks right.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import capture_ops, catalog, geom, ops, sheet  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402
from acadmcp.catalog import Catalog  # noqa: E402

KEY = "rollup_door"
BLOCK = "ACADMCP_ROLLUP_DOOR"


@pytest.fixture(autouse=True)
def fresh_catalog():
    """A catalogue of its own, so a capture never touches the package starter or a project's
    file and a registered key cannot leak between tests."""
    catalog.reset()
    catalog.use(Catalog())
    yield
    catalog.reset()


@pytest.fixture()
def be():
    return make_backend("dxf")


def _door_steps(be) -> list[int]:
    """A wall run and a door in it, as two journalled steps — the shape a capture gathers."""
    with be.step("draw_wall"):
        ops.draw_wall_run(be, [(0, 0), (6000, 0)], 200)
    with be.step("draw_door"):
        ops.draw_door(be, (1000, 0), 900, 200)
    return [1, 2]


def _capture(be, steps, **over):
    """Capture inside a step, the way `element_capture` wraps it: one tool call, one step."""
    args = dict(steps=steps, key=KEY, label="卷帘门", layer="A-DOOR", tag_prefix="JLM")
    args.update(over)
    with be.step("element_capture", params={"key": args["key"]}):
        return capture_ops.capture(be, **args)


# ---------------------------------------------------------------------------
# what a capture makes
# ---------------------------------------------------------------------------


def test_capture_reports_the_definition_it_created(be):
    steps = _door_steps(be)
    out = _capture(be, steps)

    assert out["key"] == KEY
    assert out["block"] == BLOCK
    # The wall run's two faces and two caps, and the door's leaf, swing arc and two jambs.
    assert out["entities"] == len(be.step_primitives(steps)) == 8
    assert BLOCK in be.doc.blocks


def test_the_definition_is_translated_so_the_base_point_is_its_origin(be):
    """The capture chooses the base point, and `at` then means that point: a block whose
    geometry kept its model coordinates would land a hinge-length away from where it was
    asked for."""
    _capture(be, _door_steps(be), base_point=[1000.0, 0.0])

    arcs = [e for e in be.doc.blocks.get(BLOCK) if e.dxftype() == "ARC"]
    assert len(arcs) == 1
    assert (arcs[0].dxf.center.x, arcs[0].dxf.center.y) == (0.0, 0.0)


def test_the_base_point_defaults_to_the_min_corner_of_the_captured_geometry(be):
    steps = _door_steps(be)
    primitives = be.step_primitives(steps)
    points = [
        (flat[i], flat[i + 1])
        for prim in primitives
        for flat in [prim["points"]]
        for i in range(0, len(flat), 2)
    ]
    min_x, min_y, _, _ = geom.bbox(points)

    out = _capture(be, steps)

    assert out["base_point"] == [min_x, min_y]


def test_a_captured_element_places_twice_on_the_entry_s_layer(be):
    _capture(be, _door_steps(be))
    element = catalog.current().get(KEY)
    assert element is not None and element.kind == "op" and element.block == ""

    with be.step("place_element"):
        ops.place_element(be, element, (0, 0))
    with be.step("place_element"):
        ops.place_element(be, element, (7000, 0))

    refs = [e for e in be.msp if e.dxftype() == "INSERT"]
    assert len(refs) == 2
    assert {e.dxf.name for e in refs} == {BLOCK}
    assert {e.dxf.layer for e in refs} == {"A-DOOR"}


def test_the_drawing_carries_the_captured_element_through_a_save_and_reopen(be, tmp_path):
    """A capture has to be a fix that stays fixed, and the only thing that travels is the
    file: the definition lives in the drawing's block table and the entry in the catalogue
    identity the drawing records."""
    _capture(be, _door_steps(be))
    # What drawing_save does: re-stamp the catalogue now that the capture has added to it.
    be.note_catalog(catalog.current())
    out = be.save(str(tmp_path / "captured.dxf"))

    reopened = make_backend("dxf", path=out)
    embedded = reopened.embedded_catalog()
    assert embedded is not None
    entry = embedded.get(KEY)
    assert entry is not None and entry.op == "insert_captured"
    assert entry.params == {"block": BLOCK}
    assert BLOCK in reopened.doc.blocks

    # What drawing_open does: adopt the copy the drawing carries, and place from it.
    catalog.use(embedded)
    with reopened.step("place_element"):
        ops.place_element(reopened, embedded.get(KEY), (0, 0))
    assert [e for e in reopened.msp if e.dxftype() == "INSERT"]


# ---------------------------------------------------------------------------
# document state: the definition is nobody's step to own
# ---------------------------------------------------------------------------


def test_undo_of_the_capture_step_leaves_the_definition_standing(be):
    """Rollback erases only the handles a step recorded. The definition was written
    directly, so a step cannot own it — the same rule the page setup follows — or a later
    step's block reference would dangle."""
    _capture(be, _door_steps(be))
    assert BLOCK in be.doc.blocks

    report = be.undo(1)

    assert [s["op"] for s in report["undone"]] == ["element_capture"]
    assert report["erased"] == 0
    assert BLOCK in be.doc.blocks
    assert be.step_rows()[-1]["state"] == "undone"


def test_the_capture_step_records_its_intent_so_it_can_be_committed(be):
    """Every drawing tool call is one step and a step that recorded nothing cannot be
    committed, so the capture records the definition it made as a `block` intent — which
    applies as "make sure that definition is there", not as geometry."""
    _capture(be, _door_steps(be))

    row = be.step_rows()[-1]
    assert row["op"] == "element_capture"
    assert row["entities"] == 0  # a definition is not a model entity
    assert [p["kind"] for p in be.journal.steps[-1].primitives] == ["block"]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_capture_refuses_a_key_the_catalogue_already_has(be):
    steps = _door_steps(be)
    _capture(be, steps)

    with pytest.raises(ValueError, match="already"):
        _capture(be, steps)


def test_capture_refuses_sheet_furniture(be):
    """A frame is drawn at 1:1 in paper millimetres; a block is model geometry."""
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 100))
    with be.step("drawing_sheet"):
        be.paper_primitives([{"kind": "line", "points": [(0.0, 0.0), (100.0, 0.0)]}])

    with pytest.raises(ValueError, match="paper"):
        _capture(be, [1], key="frame")


def test_capture_refuses_a_backend_that_cannot_write_a_definition():
    class _Live:
        name = "plugin"
        supports_block_create = False

    with pytest.raises(BackendError, match="block definition"):
        capture_ops.capture(_Live(), steps=[1], key="x")


def test_capture_refuses_an_empty_gathering(be):
    with pytest.raises(ValueError, match="step"):
        capture_ops.capture(be, steps=[], key="x")


def test_capture_refuses_a_step_that_is_not_applied(be):
    _door_steps(be)
    be.undo(1)

    with pytest.raises(ValueError, match="undone"):
        capture_ops.capture(be, steps=[2], key="x")
    with pytest.raises(ValueError, match="no step"):
        capture_ops.capture(be, steps=[99], key="x")


def test_capture_refuses_a_step_listed_twice(be):
    """A repeated index concatenates the step's primitives twice, so the block holds every
    entity twice — invisible in the model, and the fingerprint then refuses the honest
    single-copy capture as a duplicate of it."""
    _door_steps(be)

    with pytest.raises(ValueError, match="step 1 is listed twice"):
        _capture(be, [1, 1], key="doubled")
    with pytest.raises(ValueError, match="step 2 is listed twice"):
        _capture(be, [1, 2, 2], key="doubled")
    assert "ACADMCP_DOUBLED" not in be.doc.blocks
    assert catalog.current().get("doubled") is None


def test_capture_accepts_the_same_steps_in_another_order(be):
    """The refusal is about a repeated index, not about order: which step is gathered first is
    the caller's choice, and the fingerprint is order-free on purpose."""
    steps = _door_steps(be)

    out = _capture(be, list(reversed(steps)), key="reordered")

    assert out["entities"] == len(be.step_primitives(steps)) == 8


# ---------------------------------------------------------------------------
# the contract underneath
# ---------------------------------------------------------------------------


def test_only_the_dxf_driver_writes_definitions(be):
    from acadmcp.backends.plugin import PluginBackend

    assert be.supports_block_create is True
    assert PluginBackend.supports_block_create is False


def test_a_backend_without_definitions_refuses_by_name():
    from acadmcp.backends.recording import RecordingBackend

    with pytest.raises(BackendError, match="block definition"):
        RecordingBackend().create_block("X", [{"kind": "line", "points": [0, 0, 1, 0]}], (0, 0))


def test_dxf_insert_refuses_an_absent_definition(be):
    with pytest.raises(BackendError, match="capture"), be.step("place_element"):
        be.insert("NO_SUCH_BLOCK", (0, 0))


def test_dxf_insert_refuses_parameters_it_cannot_set(be):
    """ezdxf has no dynamic-block API, so a size given here would be dropped without a word."""
    be.create_block(BLOCK, [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0, 0))

    with pytest.raises(BackendError, match="parameters"), be.step("place_element"):
        be.insert(BLOCK, (0, 0), properties={"门尺寸": 900})


def test_replaying_a_library_insert_is_refused_not_crashed(be):
    """`drawing_replay` reports the steps a target cannot apply; a library insert arriving on
    dxf has to come back as a refusal, not as an ezdxf error out of a missing definition."""
    from acadmcp.journal import StepRecord

    record = StepRecord(
        index=1,
        op="place_element",
        label="",
        params={},
        primitives=[
            {
                "kind": "insert",
                "points": [0.0, 0.0],
                "block": "门 - 公制",
                "layer": "A-DOOR",
                "rotation": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            }
        ],
    )

    with pytest.raises(BackendError, match="no such block definition"):
        be.replay_step(record)


def test_dxf_create_block_refuses_a_name_it_already_holds(be):
    be.create_block(BLOCK, [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0, 0))

    with pytest.raises(BackendError, match="already"):
        be.create_block(BLOCK, [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0, 0))


# ---------------------------------------------------------------------------
# runtime registration
# ---------------------------------------------------------------------------


def test_register_refuses_an_entry_that_could_not_be_drawn():
    with pytest.raises(ValueError, match="neither"):
        Catalog().register({"key": "broken"})


def test_register_refuses_a_duplicate_key():
    loaded = Catalog()
    loaded.register({"key": "thing", "op": "column"})

    with pytest.raises(ValueError, match="already"):
        loaded.register({"key": "thing", "op": "column"})


def test_a_registered_entry_is_part_of_the_identity():
    loaded = Catalog()
    loaded.register({"key": "thing", "op": "insert_captured", "params": {"block": "B"}})

    assert loaded.get("thing") is not None
    assert loaded.identity()["count"] == 1


# ---------------------------------------------------------------------------
# the geometry fingerprint
# ---------------------------------------------------------------------------


def test_the_fingerprint_rounds_to_a_tenth_of_a_millimetre_and_ignores_order():
    one = [{"kind": "line", "layer": "A-WALL", "points": [0.0, 0.0, 6000.0, 0.0]}]
    nudge = [{"kind": "line", "layer": "A-WALL", "points": [0.0004, 0.04, 6000.03, 0.0]}]

    assert capture_ops.fingerprint_primitives(one) == capture_ops.fingerprint_primitives(nudge)
    # Order-free: the same primitives gathered in another order hash the same, because how a
    # caller split the capture across steps is its choice, not part of the geometry.
    two = one + [{"kind": "circle", "layer": "A-WALL", "points": [0.0, 0.0], "radius": 5.0}]
    assert capture_ops.fingerprint_primitives(two) == capture_ops.fingerprint_primitives(
        list(reversed(two))
    )
    assert capture_ops.fingerprint_primitives(one).startswith("sha256:")


def test_the_fingerprint_separates_different_geometry():
    one = [{"kind": "line", "layer": "A-WALL", "points": [0.0, 0.0, 6000.0, 0.0]}]
    moved = [{"kind": "line", "layer": "A-WALL", "points": [0.0, 0.0, 6001.0, 0.0]}]
    other_layer = [{"kind": "line", "layer": "A-DOOR", "points": [0.0, 0.0, 6000.0, 0.0]}]

    assert capture_ops.fingerprint_primitives(one) != capture_ops.fingerprint_primitives(moved)
    assert capture_ops.fingerprint_primitives(one) != capture_ops.fingerprint_primitives(
        other_layer
    )


def test_the_fingerprint_carries_every_geometric_field():
    """Points and text were not the whole of a primitive: two concentric circles that differ
    only in radius used to fingerprint alike, so a genuinely different element was refused as
    a duplicate of the first."""
    base = {
        "kind": "arc",
        "layer": "A-WALL",
        "points": [0.0, 0.0],
        "radius": 500.0,
        "start_angle": 0.0,
        "end_angle": 90.0,
    }
    distinct = {
        "radius": 800.0,
        "start_angle": 10.0,
        "end_angle": 180.0,
        "rotation": 45.0,
        "linetype": "DASHED",
        "text": "M-1",
        "height": 350.0,
        "width": 900.0,
        "align": "center",
        "pattern": "ANSI31",
        "scale": 40.0,
        "angle": 45.0,
        "closed": True,
        "solid": True,
        "offset": 500.0,
        "text_height": 250.0,
        "scale_x": 2.0,
        "scale_y": 3.0,
        "block": "门 - 公制",
    }
    for field, value in distinct.items():
        other = {**base, field: value}
        assert capture_ops.fingerprint_primitives([base]) != capture_ops.fingerprint_primitives(
            [other]
        ), field

    # A field this kind does not carry is neutral, not a different shape: the wire omits a
    # key rather than nulling it, and the two must hash the same.
    assert capture_ops.fingerprint_primitives([base]) == capture_ops.fingerprint_primitives(
        [{**base, "closed": False, "pattern": "", "scale": 0.0}]
    )


def test_two_concentric_circles_of_different_radii_are_two_elements(be):
    """The whole failure the radius was dropped for: a 500 and an 800 circle were 'the same
    geometry', so the second was refused as a duplicate and the element was unreachable."""
    with be.step("draw_circle"):
        be.circle((0, 0), 500)
    with be.step("draw_circle"):
        be.circle((0, 0), 800)

    small = _capture(be, [1], key="ring_small")
    large = _capture(be, [2], key="ring_large")

    assert small["fingerprint"] != large["fingerprint"]
    assert catalog.current().get("ring_small") is not None
    assert catalog.current().get("ring_large") is not None


# ---------------------------------------------------------------------------
# provenance stamped at capture time
# ---------------------------------------------------------------------------


def test_a_capture_stamps_its_origin(be):
    steps = _door_steps(be)
    _capture(be, steps)

    origin = catalog.current().get(KEY).origin

    assert origin["steps"] == [1, 2]
    # A dxf drawing that has never been saved has no path, and "" is the honest answer.
    assert origin["source_drawing"] == ""
    assert origin["captured_at"]
    assert origin["fingerprint"] == capture_ops.fingerprint_primitives(be.step_primitives(steps))


def test_capture_refuses_geometry_the_library_already_holds(be):
    """The same geometry under another key is the same element, not a new component. The
    existing key is named, so the caller can reuse it or rename only if it really differs."""
    _capture(be, _door_steps(be), key="first")

    with pytest.raises(ValueError, match="already holds this geometry as 'first'"):
        _capture(be, _door_steps(be), key="second")


def test_the_duplicate_refusal_names_the_fingerprint_it_matched(be):
    """A successful capture replies with its fingerprint, so the refusal has to say which
    geometry was collided with — the same string in the same shape — or the caller cannot see
    whether the two are really the same element."""
    first = _capture(be, _door_steps(be), key="first")

    with pytest.raises(ValueError) as caught:
        _capture(be, _door_steps(be), key="second")

    assert first["fingerprint"].startswith("sha256:")
    assert first["fingerprint"] in str(caught.value)


def test_capture_refuses_a_placement_step(be):
    """A placed element is an insert primitive: capturing it nests a block inside a block, and
    the fragment a publish writes is a fresh document with no child definitions, so the element
    would place here and never accumulate. Refused at capture time, naming the recovery."""
    _capture(be, _door_steps(be))
    with be.step("place_element", params={"type": KEY}):
        be.insert(BLOCK, (5000.0, 5000.0))

    with pytest.raises(ValueError, match="cannot travel in a geometry fragment"):
        _capture(be, [4], key="nested_placement")


def test_capture_refuses_a_capture_step(be):
    """A capture records a block-intent primitive — the same nesting one step removed."""
    _capture(be, _door_steps(be))

    with pytest.raises(ValueError, match="cannot travel in a geometry fragment"):
        _capture(be, [3], key="capture_of_capture")
