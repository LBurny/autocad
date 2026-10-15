"""The parametric twins: what a catalogue entry draws where library blocks cannot go.

No AutoCAD needed. What is worth testing here is the routing — which half of an entry a
backend draws, and that the same element name draws the same thing either way — plus the
refusals, because the failure this whole mechanism exists to prevent is a plausible-looking
element placed at the wrong size.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog, catalog_ops, ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.catalog import Element, Variant  # noqa: E402


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


def _captured(key: str, block: str) -> Element:
    """A captured element, the shape `element_capture` registers: op-only, with the
    definition's name in its own `params` and no library block at all."""
    return Element(
        key=key,
        label=key,
        category="captured",
        block="",
        source="",
        layer="A-DOOR",
        op="insert_captured",
        width_param="",
        variants={},
        params={"block": block},
    )


@pytest.fixture()
def be():
    return make_backend("dxf")


def _primitives(be) -> list[dict]:
    return [p for step in be.journal.steps for p in step.primitives]


def test_dxf_does_not_claim_to_insert_blocks(be):
    assert be.supports_insert is False


def test_the_plugin_is_the_one_backend_that_does(monkeypatch):
    from acadmcp.backends.plugin import PluginBackend

    assert PluginBackend.supports_insert is True


def test_an_op_entry_places_on_the_headless_backend(be):
    with be.step("place_element"):
        made = ops.place_element(be, _element(), (0, 0), rotation=0.0)

    assert made and be.info()["entities"] > 0


def test_an_op_entry_places_its_tag_beside_it(be):
    """Asserted on the recorded primitives: `dxf.entities()` is a thin listing, and the
    journal is the record of what was actually drawn."""
    with be.step("place_element"):
        ops.place_element(be, _element(), (0, 0), tag="M-1")

    texts = [p["text"] for p in _primitives(be) if p["kind"] == "text"]
    assert texts == ["M-1"]


def test_a_measured_width_overrides_the_variant(be):
    """A wall opening's width is measured, not nominal: it must beat the variant, the same
    way a caller's `params` already do. Asserted on the geometry rather than on the settings
    dict — a width that is merged but never drawn is the failure to catch."""
    with be.step("place_element"):
        ops.place_element(be, _element(), (0, 0), variant="900", width=1500)

    xs = [p for prim in _primitives(be) for p in prim["points"][0::2]]
    assert max(xs) - min(xs) == pytest.approx(1500.0, abs=1.0)


def test_a_width_given_to_an_entry_that_cannot_express_one_is_refused(be):
    """The catalogue is the only place that knows a given block calls its width 门尺寸; an
    entry that does not say gets a refusal rather than a nominal size in a measured hole."""
    block_only = _element(op="", block="门 - 公制", source="lib.dwg", width_param="")

    with pytest.raises(ValueError) as caught:
        ops.place_element(be, block_only, (0, 0), width=1500)

    assert "width_param" in str(caught.value)


def test_a_lift_cannot_be_asked_to_rotate(be):
    """Its op builds an axis-aligned rectangle and its X cross would not follow a rotated
    outline, so this refuses instead of drawing an upright shaft."""
    with pytest.raises(ValueError) as caught:
        ops.place_element(be, _element(op="lift"), (0, 0), rotation=90.0)

    assert "rotat" in str(caught.value)


# ---------------------------------------------------------------------------
# a caller's `params` may not rename the element, and `scale` does what it says
# ---------------------------------------------------------------------------


def _inserts(be) -> list[dict]:
    return [p for p in _primitives(be) if p["kind"] == "insert"]


def test_a_captured_element_cannot_be_renamed_by_params(be):
    """The measured bug: a caller's `params={"block": ...}` overwrote the definition's name,
    so the INSERT that landed was a *different element* while the step and the drawing
    summary still claimed the one asked for — the drawing and the journal disagreeing, which
    is the exact failure the catalogue exists to prevent. The refusal names the fixed block."""
    be.create_block(
        "ACADMCP_CAP_A", [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0.0, 0.0)
    )
    be.create_block(
        "ACADMCP_CAP_B", [{"kind": "line", "points": [0.0, 0.0, 500.0, 500.0]}], (0.0, 0.0)
    )

    with pytest.raises(ValueError) as caught:
        ops.place_element(
            be,
            _captured("cap_a", "ACADMCP_CAP_A"),
            (20000, 0),
            params={"block": "ACADMCP_CAP_B"},
        )

    message = str(caught.value)
    assert "ACADMCP_CAP_A" in message
    assert "params" in message
    assert _primitives(be) == []  # nothing was drawn at all


def test_an_unknown_param_is_refused_with_the_vocabulary(be):
    """`**params` used to swallow the key silently: a caller could set a size that reached
    nothing and be told it worked. The refusal names the key and what the twin does take."""
    with pytest.raises(ValueError) as caught:
        ops.place_element(be, _element(), (0, 0), params={"anything": 1})

    message = str(caught.value)
    assert "anything" in message
    assert "width" in message  # the vocabulary it does take
    assert _primitives(be) == []


def test_a_declared_twin_property_still_lands(be):
    """The guard must not break the legitimate path: a key the twin does take is merged and
    drawn — asserted on the geometry, not on the settings dict."""
    element = _element(params={"wall_thickness": 240.0})
    with be.step("place_element"):
        ops.place_element(be, element, (0, 0), params={"wall_thickness": 240.0})

    lengths = [
        ((p["points"][2] - p["points"][0]) ** 2 + (p["points"][3] - p["points"][1]) ** 2) ** 0.5
        for p in _primitives(be)
        if p["kind"] == "line"
    ]
    # `draw_door` draws its two jamb ticks across the wall thickness, so a 240 mm thickness
    # is two 240 mm lines.
    assert [round(length, 3) for length in lengths].count(240.0) == 2


def test_place_along_routes_through_the_param_guard(be):
    """`place_along`/`place_grid` repeat `place_element`, so the guard covers them without a
    second copy of it."""
    with pytest.raises(ValueError) as caught:
        ops.place_along(be, _element(), (0, 0), (6000, 0), count=3, params={"nope": 1})

    assert "nope" in str(caught.value)


def test_a_captured_element_places_at_the_scale_asked_for(be):
    """The other measured bug: `scale` was dropped on the op path, so a captured element's
    INSERT came out at 1.0 while the caller asked for 2 — and the journal agreed with the
    wrong drawing, so a replay was consistently wrong too."""
    be.create_block(
        "ACADMCP_CAP_A", [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0.0, 0.0)
    )
    with be.step("place_element"):
        ops.place_element(be, _captured("cap_a", "ACADMCP_CAP_A"), (0, 0), scale=2.0)

    insert = _inserts(be)[0]
    assert insert["scale_x"] == 2.0
    assert insert["scale_y"] == 2.0


def test_a_captured_element_at_the_default_scale_is_unchanged(be):
    be.create_block(
        "ACADMCP_CAP_A", [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0.0, 0.0)
    )
    with be.step("place_element"):
        ops.place_element(be, _captured("cap_a", "ACADMCP_CAP_A"), (0, 0))

    insert = _inserts(be)[0]
    assert insert["scale_x"] == 1.0
    assert insert["scale_y"] == 1.0


def test_a_parametric_twin_refuses_a_scale(be):
    """A twin is built from its own real-size dimensions, so a uniform scale would draw a
    door of no standard size at all — refused by name rather than drawn unscaled."""
    with pytest.raises(ValueError) as caught:
        ops.place_element(be, _element(), (0, 0), scale=2.0)

    assert "scal" in str(caught.value)
    assert _primitives(be) == []


def test_a_twin_at_the_default_scale_still_places(be):
    with be.step("place_element"):
        made = ops.place_element(be, _element(), (0, 0))

    assert made and _primitives(be)


def test_an_unknown_op_names_the_ones_there_are():
    with pytest.raises(ValueError) as caught:
        catalog_ops.get("no_such_thing")

    assert "column" in str(caught.value)


def test_every_adapter_shares_one_signature():
    """The registry is only useful if a catalogue entry can name any of them and be called
    the same way, which is what `ops.place_element` and the wall-opening path both do."""
    import inspect

    for name, adapter in catalog_ops.CATALOG_OPS.items():
        params = inspect.signature(adapter).parameters
        assert list(params)[:2] == ["be", "at"], name
        assert params["rotation"].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params["layer"].kind is inspect.Parameter.KEYWORD_ONLY, name
        # Every adapter has to tolerate the parameters a catalogue entry carries, because
        # the entry's own `params` and its variant's are splatted straight in.
        assert params["width"].kind is inspect.Parameter.KEYWORD_ONLY, name
        # `scale` is uniform too: it is forwarded by `place_element` to whichever adapter the
        # entry names, so every one has to accept it — the twins refuse a non-1.0 by name,
        # because only a captured block reference has a real scale factor.
        assert params["scale"].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params["scale"].default == 1.0, name


def test_every_adapter_draws_something(be):
    """A registry entry that draws nothing is a catalogue element that silently vanishes.

    `insert_captured` is the one whose input is a definition the drawing already holds rather
    than a set of dimensions, so it needs one made first — that is what a capture does, and
    the adapter places from it.
    """
    for name in sorted(catalog_ops.CATALOG_OPS):
        fresh = make_backend("dxf")
        params: dict = {}
        if name == "insert_captured":
            fresh.create_block(
                "CAPTURED", [{"kind": "line", "points": [0.0, 0.0, 1000.0, 0.0]}], (0.0, 0.0)
            )
            params["block"] = "CAPTURED"
        with fresh.step("place_element"):
            made = catalog_ops.CATALOG_OPS[name](fresh, (1000.0, 1000.0), layer="0", **params)
        assert made, name


# ---------------------------------------------------------------------------
# an opening in a wall may name a catalogue element
# ---------------------------------------------------------------------------


def _with_catalog(monkeypatch, **elements):
    monkeypatch.setattr(catalog, "current", lambda paths=None: catalog.Catalog(elements))


def test_an_opening_without_an_element_is_unchanged(be):
    """The hard constraint: an opening that names no element takes the drawing path it
    always did — `symbols` is still the built-in line-and-arc door — and reports no element
    opening. The report key is absent only on the closed-ring-with-no-openings return,
    which has no openings to report on."""
    before = ops.draw_wall_with_openings(
        be, [(0, 0), (6000, 0)], [{"start": 1000, "width": 900, "kind": "door"}], 200
    )
    assert before["symbols"] == 4  # leaf, swing arc and two jamb ticks
    assert before["element_openings"] == {"placed": 0, "skipped": []}

    bare = ops.draw_wall_with_openings(
        be, [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], [], 200, closed=True
    )
    assert "element_openings" not in bare


def test_an_opening_may_name_a_catalogue_element(be, monkeypatch):
    _with_catalog(monkeypatch, door=_element())
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "door"}],
        200,
    )
    assert out["element_openings"] == {"placed": 1, "skipped": []}
    # The opening's own width cuts the wall and sizes the symbol: the wall gap is 900.
    assert out["openings"] == 1


def test_an_opening_naming_an_unknown_element_is_reported_not_ignored(be, monkeypatch):
    _with_catalog(monkeypatch)
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "nope"}],
        200,
    )
    assert out["element_openings"]["placed"] == 0
    assert "nope" in out["element_openings"]["skipped"][0]


def test_an_unknown_element_still_leaves_a_gap_in_the_wall(be, monkeypatch):
    """A skipped symbol must not swallow the opening: the wall is cut either way, and the
    run continues past it the way a clamped opening does."""
    _with_catalog(monkeypatch)
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "nope"}],
        200,
    )
    assert out["openings"] == 1 and out["segments"] == 2 and out["symbols"] == 0
    # The two wall faces stop at the hole and pick up again after it.
    faces = sorted(
        (min(p[0] for p in f), max(p[0] for p in f))
        for f in (e.get_points("xy") for e in be.msp.query("LWPOLYLINE"))
    )
    assert faces == [(0.0, 1000.0), (0.0, 1000.0), (1900.0, 6000.0), (1900.0, 6000.0)]


def test_a_named_element_is_sized_by_the_opening_not_by_its_variant(be, monkeypatch):
    """The opening's 1500 is measured and the variant's 900 is nominal. A 900 door drawn
    into a 1500 hole is a door that does not fill it; `place_element(width=)` is the
    override, and this is the assertion that it reaches the geometry."""
    _with_catalog(monkeypatch, door=_element())
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 1500, "kind": "door", "element": "door", "variant": "900"}],
        200,
    )
    assert out["element_openings"]["placed"] == 1
    # `draw_door` draws its swing arc at the leaf radius, so the radius is the width drawn.
    assert [e.dxf.radius for e in be.msp if e.dxftype() == "ARC"] == [1500.0]


def test_the_twin_places_at_the_opening_s_first_jamb(be, monkeypatch):
    """`draw_door` hinges on a jamb and `draw_window` starts at one, so the parametric path
    is handed `point_along(start)`. A door placed at the midpoint would be drawn half a
    leaf to the side of its own hole."""
    _with_catalog(monkeypatch, door=_element())
    ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "door"}],
        200,
    )
    assert [e.dxf.center.x for e in be.msp if e.dxftype() == "ARC"] == [1000.0]


class _Inserter:
    """Enough of a backend to make the block path the one chosen: a door block's insertion
    point is its centre, which is the half of the rule under test. `ops.place_element` is
    faked, so nothing else has to exist."""

    supports_insert = True


def test_the_block_path_places_at_the_opening_s_midpoint(monkeypatch):
    seen: dict = {}

    def fake_place(be, element, at, **kw):
        seen.update(at=at, **kw)
        return ["symbol"]

    monkeypatch.setattr(ops, "place_element", fake_place)
    block = _element(block="门 - 公制", source="lib.dwg", width_param="门尺寸")
    _with_catalog(monkeypatch, door=block)

    symbol = ops._opening_element_symbol(
        _Inserter(),
        "door",
        {"start": 1000, "width": 900, "kind": "door"},
        [(0.0, 0.0), (6000.0, 0.0)],
        1000.0,
        900.0,
        0.0,
        "A-DOOR",
        "A-WINDOW",
    )

    assert symbol == ["symbol"]
    assert seen["at"] == (1450.0, 0.0)
    assert seen["width"] == 900.0


def test_the_block_path_is_chosen_only_where_a_block_can_be_inserted(be, monkeypatch):
    """A block-only entry: no `op` to fall back on, and the headless backend has no library
    to insert from, so there is nothing to draw."""
    block_only = _element(op="", block="门 - 公制", source="lib.dwg", width_param="门尺寸")
    _with_catalog(monkeypatch, door=block_only)

    assert (
        ops._opening_element_symbol(
            be, "door", {}, [(0.0, 0.0), (6000.0, 0.0)], 1000.0, 900.0, 0.0, "A-DOOR", "A-WINDOW"
        )
        is None
    )


def test_an_entry_that_cannot_reach_this_backend_is_skipped_not_raised(be, monkeypatch):
    """A block-only entry on the headless backend: the wall keeps its hole and the run goes
    on, and the message says how to recover rather than leaving the client a bare failure."""
    block_only = _element(op="", block="门 - 公制", source="lib.dwg", width_param="门尺寸")
    _with_catalog(monkeypatch, door=block_only)
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "door"}],
        200,
    )
    assert out["element_openings"]["placed"] == 0
    assert "plugin" in out["element_openings"]["skipped"][0]
    assert out["segments"] == 2 and out["openings"] == 1


def test_an_entry_with_nowhere_to_put_a_measured_width_is_skipped_not_raised(be, monkeypatch):
    """The other entry-level fact: a width reaches a block through `width_param`, and an
    entry that does not state one cannot fill a measured hole. Checked before the backend's
    capability is, so the message names the catalogue rather than sending the reader to
    AutoCAD."""
    no_width = _element(op="", block="门 - 公制", source="lib.dwg", width_param="")
    _with_catalog(monkeypatch, door=no_width)
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "door"}],
        200,
    )
    assert out["element_openings"]["placed"] == 0
    assert "width_param" in out["element_openings"]["skipped"][0]


def test_the_two_refusals_read_differently(be, monkeypatch):
    """A missing entry and an entry that cannot reach this backend have different fixes —
    find the name again, or connect the plugin — so one message for both sends the reader
    to the wrong place."""
    block_only = _element(
        key="block_only", op="", block="门 - 公制", source="lib.dwg", width_param="门尺寸"
    )
    _with_catalog(monkeypatch, block_only=block_only)

    missing = ops._opening_element_refusal(be, "nope", 1000.0)
    unreachable = ops._opening_element_refusal(be, "block_only", 1000.0)

    assert missing.startswith("opening at 1000.0")
    assert "nope" in missing and "no element" in missing
    assert "block_only" in unreachable and "plugin" in unreachable
    assert missing != unreachable


def test_the_refusal_names_the_opening_it_belongs_to(be, monkeypatch):
    """A wall run has many openings, so a report that does not say which one is unusable."""
    _with_catalog(monkeypatch)
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [
            {"start": 1000, "width": 900, "kind": "door", "element": "nope"},
            {"start": 4000, "width": 900, "kind": "door", "element": "gone"},
        ],
        200,
    )
    assert out["element_openings"]["skipped"] == [
        "opening at 1000.0: no element 'nope' in the catalogue",
        "opening at 4000.0: no element 'gone' in the catalogue",
    ]
