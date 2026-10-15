# Agent-First Floor Plans Implementation Plan

> **For agentic workers:** Implement this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this server answerable without eyes and usable without AutoCAD. Four capabilities, in the order they unblock each other:

1. **One catalogue, three backends.** Today the element catalogue is plugin-only — `place_element`, `place_along` and `place_grid` all call `_require_plugin()`, and both `dxf` and `com` refuse `insert`. An AI writing a plan headless (the fast, previewable path) therefore cannot use a single door, window or column from the library. A catalogue entry gains a **parametric twin** so the same element name draws everywhere.
2. **A drawing that records what drew it.** The catalogue is ambient today: a package file plus whichever `acadmcp.json` is found by walking up from the working directory. Reopen the same drawing elsewhere and its doors silently mean something else. The journal payload gains a `meta` section carrying the catalogue identity *and its contents*, so the drawing is self-describing and reproducible.
3. **A structured read-back.** The only ways to check a drawing are a screenshot or counting entities. `summary.py` recomputes a design summary — rooms with areas, openings with tags, elements placed, layers, warnings — from the journal's recorded intent. CAD-free, no vision, and it works after a reopen because the journal persists.
4. **Rollback you can aim.** `Journal.rollback_to(index)` exists and has no tool; `drawing_undo(count)` makes an agent count steps. Add `drawing_rollback_to(index)` and make `drawing_history` return machine-readable rows.

**Architecture:** Nothing here moves geometry out of `geom.py` / `ops.py`, and nothing here changes the eager drawing path — the 213 existing tests, `examples/floor_plan_demo.py` and `evaluations/fixtures/floor_plan.dxf` are all behavioural contracts that must stay byte-identical. The additions are: a resolution rule in `catalog.py`, an adapter registry in a new `catalog_ops.py`, a CAD-free `summary.py`, a `meta` section in `journal.py`, and four edits to `server.py`.

**Tech Stack:** Python 3.14 venv, ezdxf, pywin32 (`com`), mcp 2.x (`MCPServer`, aliased to `FastMCP`), pytest, ruff. The plugin is .NET 8 and is touched only by the optional Task 12.

## Global Constraints

- Millimetres everywhere; degrees counter-clockwise from +X. `INSUNITS` = 4.
- Tests never need AutoCAD. Run them with `.venv/Scripts/python.exe -m pytest -q` — **not** `uv run pytest`, which re-syncs and fails while an MCP client holds the venv exe.
- Lint `.venv/Scripts/python.exe -m ruff check . --output-format concise`; line-length 100, E501 ignored; format with `.venv/Scripts/python.exe -m ruff format .`.
- `geom.py`, `journal.py` and `summary.py` import nothing CAD-related. `summary.py` may import `geom` (pure maths) and `layers`; never ezdxf, never a backend.
- `ops.py` stays backend-agnostic: every op takes a `Backend` and does its geometry through `geom`.
- Backends only render. Computing architecture inside a backend is a bug.
- Caller mistakes raise `ValueError` at the boundary, never a bare `KeyError`/`IndexError`. `server.py`'s `_surface_input_errors()` converts `ValueError` into `BackendError` (a `ToolError`), and that message is the client's only recovery signal — it must name the value and the valid set.
- Loading never raises. A broken catalogue entry, settings file or journal is skipped and **counted**, then reported. "Could not check" is not "fine".
- A named variant that does not exist raises. Placing a size nobody asked for is not a fallback.
- COM: every attribute access on a COM object happens inside a zero-argument callable passed to `_call`, e.g. `_call(lambda: obj.Delete())`.
- Comments explain constraints the code cannot show. Match the existing density.
- **Backwards compatibility is the hard constraint of this plan.** An opening with no `element`, a catalogue entry with no `op`, and a backend with no `supports_insert` must all behave exactly as they do today.

## Tool surface after this plan

44 → 46 tools (done): `drawing_summary`, `drawing_rollback_to`. `tests/test_tool_surface.py:26` asserts the exact count and must be updated in Task 8. Every new tool also needs one of the five annotation sets, and `test_annotations_match_behaviour` needs its read-only/destructive sets extended.

## Wave 1 has landed — read this before Tasks 3–12

Tasks 1–2 are done and green (233 tests). **The interfaces below are what exists in the tree; where they differ from Task 1's draft, the tree wins.** Read the files, do not re-derive them from Task 1.

- `Element` gained `op: str`, `width_param: str`, `op_params: dict`, and the properties `kind` (`"block"`/`"op"`/`"both"`), `placement` (`"block"`/`"parametric"`/`"both"`, the same fact in the words a caller reads), `width_key` (`width_param`, or `"width"` for an op entry, or `""`), and `placement_for(can_insert) -> "block"|"op"`.
- **`Element.twin_settings(variant, overrides, width)` is new and is not `settings()`.** An entry that names a block keeps the *block's* vocabulary in `params` and states the twin's under `op_params` (element-level and per-variant), because the two vocabularies differ: the door block calls its width 门尺寸 while `door_symbol` takes `width`. A variant that states nothing for the twin where it has stated something for the block is **refused**, not defaulted — reaching an adapter as its own defaults would be a right-looking element at the wrong size. `"op_params": {}` means "the twin deliberately needs nothing". `Variant.op_params` is `None` when absent and `{}` when stated empty, and the reader relies on that difference.
- `ops.place_element(..., width=None)` routes on `placement_for`; the op path calls `element.twin_settings(variant, params, width)`; a `width` for an entry with no `width_key` is refused **before** the backend decision, because it is a fact about the entry.
- `catalog_ops.py` exists with `CATALOG_OPS` (`column`, `door_symbol`, `window_symbol`, `lift`, `stair`, `shaft`, `equipment`) and `get(name)`. Every adapter is `(be, at, *, width=None, rotation=0.0, layer="", **params)`; `tests/test_catalog_ops.py::test_every_adapter_shares_one_signature` pins it. The four rectangle adapters (`lift`, `stair`, `shaft`, `equipment`) **refuse a non-zero rotation** rather than draw an upright rectangle where a rotated one was asked for.
- `Backend.supports_insert` is a class attribute: `False` on `RecordingBackend`, `True` on `PluginBackend`.
- The shipped `elements.json` now has 10 entries: `column` is new and **op-only** (a 承力柱 is a filled rectangle whose size is its whole identity, so it needs no library and works on every backend), and `door` and `window` carry `op` + `width_param` + `op_params` so one element name draws on all three backends.

Consequences for later tasks: use `twin_settings` anywhere the twin's parameters are wanted, and never hand an adapter a block's parameter names. `test_catalog.py` gained `SHIPPED_KEYS`/`SHIPPED` constants — add an entry to the catalogue and update that list, not a scattered count.

---

### Task 1: `catalog.py` — an entry may be drawn by an op instead of a block

**Files:**
- Modify: `src/acadmcp/catalog.py`
- Test: `tests/test_catalog.py` (append)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Element.op: str` — the name of a parametric builder in `catalog_ops.CATALOG_OPS`
  - `Element.width_param: str` — which parameter carries a measured width (defaults to `"width"` for an op entry)
  - `Element.kind -> "block" | "op" | "both"`
  - `Element.placement_for(can_insert: bool) -> "block" | "op"` — raises `ValueError` with a recovery message when neither is possible
  - `Element.describe()` gains `"op"`, `"width_param"`, `"kind"`, `"placement"`
  - `_read_element` accepts an entry with `op` and no `block`
  - `Catalog.check_against` skips entries with no block (nothing in the library to check)

**Why the resolution rule lives here, not in the tool:** which half of an entry to draw is a property of the entry and the backend's capability together, and both are known before anything is drawn. Deciding it by trying `insert` and catching the refusal does not work — inside a step, `dxf` and `com` *record* the insert and defer the refusal to the commit, so a `try` around `be.insert` sees nothing.

- [x] **Step 1: Write the failing tests** — append to `tests/test_catalog.py`:

```python
def _catalog(tmp_path, entries):
    import json

    path = tmp_path / "elements.json"
    path.write_text(json.dumps({"elements": entries}), encoding="utf-8")
    return catalog.Catalog.load([str(path)])


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


def test_placement_prefers_the_block_when_the_backend_can_insert(tmp_path):
    door = _catalog(tmp_path, [_BOTH]).get("door")
    assert door.placement_for(can_insert=True) == "block"
    assert door.placement_for(can_insert=False) == "op"


def test_placement_falls_back_to_the_op_for_an_op_only_entry(tmp_path):
    assert _catalog(tmp_path, [_OP_ONLY]).get("column").placement_for(can_insert=True) == "op"


def test_placement_refuses_a_block_only_entry_on_a_backend_without_insert(tmp_path):
    """The message has to say how to recover: this is the whole failure of today's
    catalogue being plugin-only."""
    door = _catalog(tmp_path, [{"key": "door", "block": "B", "source": "lib.dwg"}]).get("door")
    with pytest.raises(ValueError) as caught:
        door.placement_for(can_insert=False)
    assert "op" in str(caught.value) and "plugin" in str(caught.value)


def test_the_measured_width_goes_where_the_entry_says(tmp_path):
    door = _catalog(tmp_path, [_BOTH]).get("door")
    assert door.settings("900", {"width": 1500}) == {"墙厚": 150, "门尺寸": 1500}
    assert door.width_key == "门尺寸"


def test_an_op_entry_defaults_the_width_key_to_width(tmp_path):
    column = _catalog(tmp_path, [_OP_ONLY]).get("column")
    assert column.width_key == "width"


def test_check_against_does_not_report_op_only_entries(tmp_path):
    loaded = _catalog(tmp_path, [_OP_ONLY, _BOTH])
    problems = loaded.check_against({"lib.dwg": ["门 - 公制"]})
    assert problems == []
```

Note the last two tests take a new call shape: `settings(variant, overrides)` today takes a raw override dict. Keep that signature and let the *caller* translate a measured width into the entry's own key — Task 2's `place_element` does that translation with `width_key`. Do not put the translation inside `settings`, which must stay a pure three-level merge.

- [x] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_catalog.py -q` — expect FAIL (`Element` has no `op`)

- [x] **Step 3: Implement** in `catalog.py`:

`Element` gains two fields and three helpers:

```python
op: str = ""
#: Which parameter carries a *measured* width — a wall opening's width, not a nominal
#: size. The catalogue is the only place that knows a given block calls it 门尺寸 and
#: another calls it Wall.
width_param: str = ""


@property
def kind(self) -> str:
    if self.block and self.op:
        return "both"
    return "block" if self.block else "op"


@property
def width_key(self) -> str:
    """The parameter a measured width goes under, or "" when there is none.

    An op adapter is written to take `width`; a library block calls it whatever its
    author called it. Defaulting the op case means a catalogue entry only has to state
    `width_param` when it names a block.
    """
    return self.width_param or ("width" if self.op else "")


def placement_for(self, can_insert: bool) -> str:
    """Which half of this entry a backend should draw.

    A backend that can insert takes the library block, and that is the point of the
    catalogue: a block carries the model number a schedule counts. A backend that
    cannot — `dxf` has no library to read, `com` refuses — falls back to the
    parametric twin, so one element name draws on every backend.
    """
    if self.block and can_insert:
        return "block"
    if self.op:
        return "op"
    if self.block:
        raise ValueError(
            f"'{self.key}' names a library block and no parametric twin ('op'), so it "
            "can only be placed where library blocks can be inserted — connect with "
            "acad_connect(mode='plugin'), or add an 'op' to its catalogue entry"
        )
    raise ValueError(f"'{self.key}' has neither a block nor an op to place")
```

`_read_element`: read `op` and `width_param`; require **either** block or op:

```python
    op = str(entry.get("op", "")).strip()
    if not block and not op:
        skipped.append(f"{key}: neither a block nor an op to place")
        return None
```

and pass `op=op, width_param=str(entry.get("width_param", "")).strip()` into the `Element`.

`describe()` gains `"op": self.op, "width_param": self.width_param, "kind": self.kind`.
`check_against` gains, before the `source` test, `if not element.block: continue` — with a comment saying an op-only entry has nothing in a library to check, so it is not "unchecked".

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_catalog.py tests/test_element_tools.py -q` — all PASS (the new tests plus the pre-existing ones, which pin the current schema)

- [x] **Step 5: Lint** `.venv/Scripts/python.exe -m ruff check src/acadmcp/catalog.py tests/test_catalog.py --output-format concise` — clean

---

### Task 2: `catalog_ops.py` — the parametric twins, and `ops.place_element` routing

**Files:**
- Create: `src/acadmcp/catalog_ops.py`
- Modify: `src/acadmcp/ops.py` (`place_element` only)
- Modify: `src/acadmcp/backends/base.py` (`supports_insert` on the protocol)
- Modify: `src/acadmcp/backends/recording.py` (`supports_insert = False` default)
- Modify: `src/acadmcp/backends/plugin.py` (`supports_insert = True`)
- Test: `tests/test_catalog_ops.py` (new)

**Interfaces:**
- Consumes: `Element.kind`, `Element.placement_for`, `Element.width_key` (Task 1)
- Produces:
  - `catalog_ops.CATALOG_OPS: dict[str, Callable]` — the adapter registry
  - Adapter signature, uniform and documented once:
    `adapter(be, at, *, width=None, rotation=0.0, layer="0", **params) -> list[Any]`
  - `catalog_ops.get(name) -> Callable` — raises `ValueError` listing the known names
  - `ops.place_element(..., width: float | None = None)` — routes block or op
  - `Backend.supports_insert: bool` (class attribute; `False` in `RecordingBackend`, `True` on `PluginBackend`)

**The adapter's `at` is the element's own reference point** — a column's centre, a door's hinge, a window's first jamb, a lift's first corner. That is what a caller saying "a door here" means, and it differs per element, which is exactly why the registry exists rather than a bare op name. Each adapter documents its own `at`.

- [x] **Step 1: Write the failing tests** — `tests/test_catalog_ops.py`:

```python
"""The parametric twins: what a catalogue entry draws where library blocks cannot go."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog_ops, ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.catalog import Element, Variant  # noqa: E402


def _element(**over):
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


@pytest.fixture()
def be():
    return make_backend("dxf")


def test_dxf_does_not_claim_to_insert_blocks(be):
    assert be.supports_insert is False


def test_an_op_entry_places_on_the_headless_backend(be):
    made = ops.place_element(be, _element(), (0, 0), rotation=0.0)
    assert made and be.info()["entities"] > 0


def test_an_op_entry_places_its_tag_beside_it(be):
    """Asserted on the recorded primitives: `dxf.entities()` reports neither points nor
    text (Task 4 fixes that), and the journal is the record of what was actually drawn."""
    with be.step("place"):
        ops.place_element(be, _element(), (0, 0), tag="M-1")
    texts = [p["text"] for p in be.journal.steps[0].primitives if p["kind"] == "text"]
    assert texts == ["M-1"]


def test_a_measured_width_overrides_the_variant(be):
    """A wall opening's width is measured, not nominal: it must beat the variant, the
    same way a caller's `params` already do. Asserted on the geometry, not on the
    settings dict — a width that is merged but not drawn is the failure to catch."""
    with be.step("place"):
        ops.place_element(be, _element(), (0, 0), variant="900", width=1500)
    xs = [p for prim in be.journal.steps[0].primitives for p in prim["points"][0::2]]
    assert max(xs) - min(xs) == pytest.approx(1500.0, abs=1.0)


def test_a_width_given_to_an_entry_that_cannot_express_one_is_refused(be):
    """The catalogue is the only place that knows a block calls its width 门尺寸; an
    entry that does not say draws nothing rather than a nominal size in a measured hole."""
    block_only = _element(op="", block="门 - 公制", source="lib.dwg", width_param="")
    with pytest.raises(ValueError) as caught:
        ops.place_element(be, block_only, (0, 0), width=1500)
    assert "width_param" in str(caught.value)


def test_an_unknown_op_names_the_ones_there_are():
    with pytest.raises(ValueError) as caught:
        catalog_ops.get("no_such_thing")
    assert "column" in str(caught.value)


def test_every_adapter_shares_one_signature():
    """The registry is only useful if the catalogue can call any of them the same way."""
    import inspect

    for name, adapter in catalog_ops.CATALOG_OPS.items():
        params = inspect.signature(adapter).parameters
        assert list(params)[:2] == ["be", "at"], name
        assert params["rotation"].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params["layer"].kind is inspect.Parameter.KEYWORD_ONLY, name
```

- [x] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_catalog_ops.py -q` — expect FAIL (`ModuleNotFoundError`)

- [x] **Step 3: Implement** `src/acadmcp/catalog_ops.py`:

```python
"""The parametric twins: what a catalogue entry draws when the backend cannot insert.

An element's *identity* is its catalogue entry — a model number, a layer, a tag prefix, a
set of sizes. How that identity reaches the paper is the backend's business: the plugin
inserts the library block the standard owns, and a backend with no library to read draws
the same door out of line and arc. Both have to be reachable by the same
``place_element("door", ...)``, which is what these adapters are for.

An adapter is a thin normaliser, and its signature is the catalogue's rather than the op's::

    adapter(be, at, *, width=None, rotation=0.0, layer="0", **params) -> list[Any]

``at`` is the element's own reference point — a column's centre, a door's hinge, a lift's
first corner — because that is what a caller placing "a door here" means. The signature is
deliberately uniform (``tests/test_catalog_ops.py`` pins it by introspection) so a catalogue
entry can name any of them and be called one way.

Nothing here computes geometry: each adapter calls the op in `ops.py` that already draws
this thing, which is where the coordinates live.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import geom, ops
from .geom import Point


def column(
    be,
    at: Point,
    *,
    size: float = 400.0,
    depth: float | None = None,
    rotation: float = 0.0,
    hatch: bool = True,
    layer: str = "A-COLUMN",
    width: float | None = None,
    **params,
) -> list[Any]:
    """A column by its centre. `width`, when a wall opening supplies one, is the column's
    plan size — a 承力柱 in a wall is as wide as the wall is thick and no wider."""
    if width is not None:
        size = float(width)
    return ops.draw_column(be, at, size, rotation=rotation, depth=depth, layer=layer, hatch=hatch)


def door_symbol(
    be,
    at: Point,
    *,
    width: float = 900.0,
    wall_thickness: float = 200.0,
    swing: str = "left",
    open_angle: float = 90.0,
    rotation: float = 0.0,
    layer: str = "A-DOOR",
    **params,
) -> list[Any]:
    """A single-leaf door by its hinge — the same hinge `draw_door` takes."""
    return ops.draw_door(
        be,
        at,
        width,
        wall_thickness,
        rotation=rotation,
        open_angle=open_angle,
        swing=swing,
        layer=layer,
    )


def window_symbol(
    be,
    at: Point,
    *,
    width: float = 1200.0,
    wall_thickness: float = 200.0,
    rotation: float = 0.0,
    layer: str = "A-WINDOW",
    **params,
) -> list[Any]:
    """A window by its first jamb — the same point `draw_window` starts from."""
    return ops.draw_window(be, at, width, wall_thickness, rotation=rotation, layer=layer)


def lift(
    be,
    at: Point,
    *,
    width: float = 2400.0,
    depth: float = 2200.0,
    tag: str | None = None,
    rotation: float = 0.0,
    layer: str = "A-LIFT",
    **params,
) -> list[Any]:
    """A lift shaft by its lower-left corner: `width` along +X, `depth` along +Y."""
    x, y = geom.xy(at)
    return ops.draw_lift(be, (x, y), (x + float(width), y + float(depth)), tag=tag, layer=layer)


def stair(
    be,
    at: Point,
    *,
    width: float = 1200.0,
    depth: float = 3000.0,
    steps: int = 9,
    axis: str = "y",
    rotation: float = 0.0,
    layer: str = "A-STAIR",
    **params,
) -> list[Any]:
    """A flight by its lower-left corner."""
    x, y = geom.xy(at)
    return ops.draw_stair(
        be, (x, y), (x + float(width), y + float(depth)), steps=steps, axis=axis, layer=layer
    )


def shaft(
    be,
    at: Point,
    *,
    width: float = 1500.0,
    depth: float = 1200.0,
    name: str | None = None,
    rotation: float = 0.0,
    layer: str = "A-SHAFT",
    **params,
) -> list[Any]:
    """A service riser by its lower-left corner."""
    x, y = geom.xy(at)
    return ops.draw_shaft(be, (x, y), (x + float(width), y + float(depth)), name, layer=layer)


def equipment(
    be,
    at: Point,
    *,
    width: float = 800.0,
    depth: float = 600.0,
    name: str | None = None,
    rotation: float = 0.0,
    layer: str = "A-EQUIP",
    **params,
) -> list[Any]:
    """A piece of fixed equipment by its lower-left corner."""
    x, y = geom.xy(at)
    return ops.draw_equipment(be, (x, y), (x + float(width), y + float(depth)), name, layer=layer)


#: What a catalogue entry may name in its ``op``. Kept as data, and the single list a
#: catalogue is written against — an entry naming anything else is a refusal that says so.
CATALOG_OPS: dict[str, Callable[..., list[Any]]] = {
    "column": column,
    "door_symbol": door_symbol,
    "window_symbol": window_symbol,
    "lift": lift,
    "stair": stair,
    "shaft": shaft,
    "equipment": equipment,
}


def get(name: str) -> Callable[..., list[Any]]:
    """The adapter an entry names, or a refusal that lists the ones there are."""
    adapter = CATALOG_OPS.get((name or "").strip().lower())
    if adapter is None:
        raise ValueError(
            f"no parametric builder {name!r}; the catalogue may name: "
            + ", ".join(sorted(CATALOG_OPS))
        )
    return adapter
```

Note `inspect` is imported by the test, not by the module: the module needs only `geom`, `ops` and `Callable`.

Then `ops.place_element` becomes the router (replace the body; signature gains `width`):

```python
def place_element(
    be: Backend,
    element: Element,
    at: Point,
    *,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str | None = None,
    tag: str | None = None,
    tag_offset: float | None = None,
    tag_layer: str = "A-ANNO",
    tag_height: float = layers.H_TAG,
    width: float | None = None,
) -> list[Any]:
    """Place one catalogue element, as a library block or as its parametric twin.

    Which one is the backend's capability's to decide (``Element.placement_for``): a
    backend that can insert gets the real block, carrying the definition the standard
    owns, which a colleague's AutoCAD can explode and measure and a schedule can count.
    One that cannot gets the same element drawn out of primitives. An element name means
    the same thing either way, which is what lets a plan be written headless and replayed
    into AutoCAD.

    ``width`` is a *measured* width — a wall opening's — and goes under the parameter the
    entry says carries one. An entry that cannot express it raises rather than drawing a
    nominal size into a measured hole.
    """
    settings = element.settings(variant, params)
    if width is not None:
        key = element.width_key
        if not key:
            raise ValueError(
                f"'{element.key}' has no parameter to put a measured width in; add "
                '"width_param" to its catalogue entry, or place it without a width'
            )
        settings[key] = float(width)

    where = element.placement_for(bool(getattr(be, "supports_insert", False)))
    if where == "block":
        made: list[Any] = [
            be.insert(
                element.block,
                at,
                source=element.source or None,
                rotation=rotation,
                scale=scale,
                properties=settings,
                layer=layer or element.layer,
            )
        ]
    else:
        from . import catalog_ops

        made = list(
            catalog_ops.get(element.op)(
                be, geom.xy(at), rotation=rotation, layer=layer or element.layer, **settings
            )
        )

    if tag:
        made.append(_element_tag(be, tag, at, tag_offset, tag_layer, tag_height))
    return made
```

`scale` is meaningful only for the block path; an op builds at real size. Document that in the docstring rather than silently applying it — a scaled parametric door would be a wrong door.

`base.py`'s `Backend` protocol gains, above `insert`:

```python
    #: Whether this backend can place a library block reference at all. Declared rather
    #: than probed: inside a step the `dxf` and `com` refusals are deferred to the commit,
    #: so a caller cannot find out by trying.
    supports_insert: bool
```

`RecordingBackend` gets `supports_insert = False` as a class attribute; `PluginBackend` overrides it to `True`. `com.py` and `dxf.py` inherit `False` and need no edit.

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_catalog_ops.py tests/test_element_tools.py tests/test_recording.py tests/test_plugin_backend.py -q` — all PASS

- [x] **Step 5: Lint** the four touched files — clean

---

### Task 3: `ops.draw_wall_with_openings` — an opening may name a catalogue element

**Files:**
- Modify: `src/acadmcp/ops.py` (`draw_wall_with_openings` branch only)
- Test: `tests/test_catalog_ops.py` (append)

**Interfaces:**
- Consumes: `ops.place_element(..., width=)` (Task 2), `catalog.current()` / `_entry`-style lookup
- Produces: an opening dict may carry `"element": "<catalogue key>"` and `"variant"`. Result gains `"element_openings": {"placed": n, "skipped": [...]}`.

**Backwards compatibility is absolute here:** an opening with no `element` takes today's path, line for line. `evaluations/fixtures/floor_plan.dxf` and the demo must not move by a millimetre.

**Where the symbol's reference point goes differs by path**, and this is the one subtlety: the block path places at the opening's **centre** (a door block's insertion point is its centre), while the parametric path passes the **jamb** `point_along(pts, start)`, because `draw_door` hinges on a jamb and `draw_window` starts at one. The element's own adapter documents which it takes.

- [x] **Step 1: Write the failing tests** — append to `tests/test_catalog_ops.py`:

```python
def test_an_opening_without_an_element_is_unchanged(be):
    before = ops.draw_wall_with_openings(
        be, [(0, 0), (6000, 0)], [{"start": 1000, "width": 900, "kind": "door"}], 200
    )
    assert before["symbols"] > 0 and "element_openings" not in before


def test_an_opening_may_name_a_catalogue_element(be, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"door": _element()})
    )
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "door"}],
        200,
    )
    assert out["element_openings"]["placed"] == 1
    # The opening's own width cuts the wall and sizes the symbol: the wall gap is 900.
    assert out["openings"] == 1


def test_an_opening_naming_an_unknown_element_is_reported_not_ignored(be, monkeypatch):
    monkeypatch.setattr(catalog, "current", lambda paths=None: catalog.Catalog({}))
    out = ops.draw_wall_with_openings(
        be,
        [(0, 0), (6000, 0)],
        [{"start": 1000, "width": 900, "kind": "door", "element": "nope"}],
        200,
    )
    assert out["element_openings"]["placed"] == 0
    assert "nope" in out["element_openings"]["skipped"][0]
```

- [x] **Step 2: Run** — expect FAIL

- [x] **Step 3: Implement** — inside the per-opening loop of `draw_wall_with_openings`, before the existing `if kind == "door":` branch:

```python
        element_key = op.get("element")
        if element_key:
            # A named element draws the symbol instead of the built-in one. The wall gap
            # is cut from the opening's own measured width either way, and that width is
            # handed to the element as an override, so a 900 nominal block cannot be
            # placed into a 1500 measured hole.
            symbol = _opening_element_symbol(
                be, element_key, op, pts, start, width, angle, thickness, door_layer, window_layer
            )
            if symbol is None:
                failures.append(_opening_element_refusal(be, element_key, start))
            else:
                symbols += symbol
            cursor = max(cursor, end)
            previous = kind
            continue
```

with these two helpers next to `draw_door`'s neighbours:

```python
def _opening_element_symbol(
    be: Backend,
    key: str,
    op: dict,
    pts: Sequence[Point],
    start: float,
    width: float,
    angle: float,
    thickness: float,
    door_layer: str,
    window_layer: str,
) -> list[Any] | None:
    """The symbol a named element draws in this opening, or None to report a refusal.

    Which point the element is placed at differs by path and that is not an accident: a
    door *block*'s insertion point is its centre, while `draw_door` hinges on a jamb — so
    the block path takes the opening's midpoint and the parametric path its first jamb.
    The adapter's own docstring says which its `at` means.
    """
    from . import catalog as _catalog

    element = _catalog.current().get(key)
    if element is None:
        return None
    try:
        where = element.placement_for(bool(getattr(be, "supports_insert", False)))
    except ValueError:
        return None
    # A window element on a door opening (or the reverse) is a catalogue mistake worth
    # naming: the layer the symbol lands on is the one a schedule counts it from.
    layer = element.layer or (door_layer if op.get("kind", "door") == "door" else window_layer)
    at = (
        geom.point_along(pts, start + width / 2.0)
        if where == "block"
        else geom.point_along(pts, start)
    )
    return ops.place_element(
        be,
        element,
        at,
        variant=op.get("variant"),
        params=op.get("params"),
        rotation=angle,
        layer=layer,
        width=width,
        tag=op.get("tag"),
        tag_offset=op.get("tag_offset"),
    )


def _opening_element_refusal(be: Backend, key: str, start: float) -> str:
    """Why the named element could not be drawn, naming the element and the opening.

    A missing entry and an entry that cannot reach this backend are different mistakes
    with different fixes, so they read differently; both leave the opening a hole in a
    wall and let the run continue, the way a clamped opening does.
    """
    from . import catalog as _catalog

    element = _catalog.current().get(key)
    at = f"opening at {round(start, 1)}"
    if element is None:
        return f"{at}: no element '{key}' in the catalogue"
    try:
        element.placement_for(bool(getattr(be, "supports_insert", False)))
    except ValueError as exc:
        return f"{at}: {exc}"
    return f"{at}: '{key}' could not be drawn here"
```

Then, at the top of the opening loop, `failures: list[str] = []`, and the main return dict gains `"element_openings": {"placed": placed, "skipped": failures}` where `placed` counts the openings that got a symbol. The early `closed and not ops` return is left without the key — that path has no openings by definition, which is what `test_an_opening_without_an_element_is_unchanged` asserts.


- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_catalog_ops.py tests/test_backends.py tests/test_geom.py -q` — all PASS, and `.venv/Scripts/python.exe examples/floor_plan_demo.py` still writes DXF + PNG

- [x] **Step 5: Confirm no existing geometry moved** — with the fingerprint check in "Verification" at the end of this plan, **not** with `git status`. `evaluations/fixtures/floor_plan.dxf` is not byte-reproducible (it embeds a timestamp, random GUIDs and occasionally a `BLOCK_RECORD` ordering that hash randomisation swaps), so it is dirty after every regeneration even when nothing moved. Compare the entity geometry instead: extract `(type, layer, text/points, height, insert)` per entity from three files — the committed fixture, the demo's output on a **pristine `git archive HEAD` tree**, and the demo's output here — and assert the last two are equal. Wave 2 ran this and found them equal, 247 entities each.

---

### Task 4: the placement tools stop requiring the plugin

**Files:**
- Modify: `src/acadmcp/server.py` (`place_element`, `place_along`, `place_grid`, `_placed`)
- Modify: `src/acadmcp/backends/dxf.py` (`entities` reports text and blocks)
- Test: `tests/test_element_tools.py` (append)

**Interfaces:**
- Consumes: `supports_insert`, `ops.place_element` (Tasks 1–2)
- Produces: `place_element`/`place_along`/`place_grid` work on `dxf` and `com`; the plugin's result shape (`ok`/`action`/`message`/`touched`/`step`/`refusal`/`journal`) is unchanged

**Two things this task must fix together, because the second is only reachable once the first lands.** `ops.existing_tags` derives tag numbers from `be.entities()`, and `DxfBackend.entities()` reports only `{"type", "layer"}` — no `text`. So on the headless backend numbering always restarts at `M-1`: place two runs of doors and the second run's tags collide with the first's. That is the "an empty entity list restarts tag numbering at 1" hazard `AGENTS.md` records for `com.py`, still live in `dxf.py` — and it becomes reachable for the catalogue precisely because Task 4 makes the catalogue usable there.

**The one asymmetry to respect:** `PluginBackend.commit_step()` returns the plugin's raw response dict (the DWG journal is the source of truth there), while `RecordingBackend.commit_step()` returns a `StepRecord`. `_placed` handles both, and that branch is the only place the two shapes meet.

- [x] **Step 1: Write the failing tests** — append to `tests/test_element_tools.py`:

```python
def test_place_element_works_on_the_headless_backend(fresh_dxf, monkeypatch):
    """Was `_require_plugin()`: an AI writing a plan headless could not place a single
    door from the catalogue."""
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )
    out = server.place_element("column", at=[0, 0], variant="600")
    assert out["ok"] and out["placed"] == 1
    assert server._state.backend.info()["entities"] > 0


def test_place_element_is_one_undoable_step_on_dxf(fresh_dxf, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )
    server.place_element("column", at=[0, 0])
    assert [s.op for s in server._state.backend.journal.steps] == ["place_element"]
    report = server.drawing_undo()
    assert report["erased"] > 0 and server._state.backend.info()["entities"] == 0


def test_place_along_on_dxf_lays_several_and_numbers_them(fresh_dxf, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )
    out = server.place_along("column", start=[0, 0], end=[6000, 0], count=3)
    assert out["ok"] and out["placed"] == 3


def test_a_second_placement_continues_the_numbering(fresh_dxf, monkeypatch):
    """Numbering is derived from the drawing's own tags, and on dxf it was derived from
    nothing: `entities()` carried no text, so every call restarted at 1 and two runs of
    doors shared tags G-1 and G-2."""
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"column": _op_element()})
    )
    server.place_along("column", start=[0, 0], end=[6000, 0], count=2)
    second = server.place_along("column", start=[0, 3000], end=[6000, 3000], count=2)
    assert second["tags"] == ["G-3", "G-4"]
```

`_op_element()` is the `_element()`-equivalent for an op-only column, built locally in the test module with `tag_prefix="G"`. `fresh_dxf` already exists in this file.

- [x] **Step 2: Run** — expect FAIL (`_require_plugin` refuses on dxf)

- [x] **Step 3: Implement** — first `dxf.py`'s `entities`, which is the numbering fix:

```python
    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        for e in self.msp:
            if layer and e.dxf.get("layer", "0") != layer:
                continue
            row: dict[str, Any] = {"type": e.dxftype(), "layer": e.dxf.get("layer", "0")}
            # Text has to come back, or `ops.existing_tags` sees an empty drawing and
            # restarts tag numbering at 1 on every call. Both TEXT and MTEXT carry their
            # content in group code 1.
            if e.dxftype() in ("TEXT", "MTEXT"):
                row["text"] = e.dxf.get("text", "")
            if e.dxftype() == "INSERT":
                # Reported under both names for parity with the plugin's `entities`: a
                # dynamic block can resolve to an anonymous `*U` record, so neither name
                # is on its own the one that was asked for. ezdxf resolves both to the
                # reference's own name here.
                row["block"] = row["definition"] = e.dxf.get("name", "")
            out.append(row)
            if len(out) >= limit:
                break
        return out
```

Then `server.py`: replace `_require_plugin()` + `_placement` in the three tools with `_placed`, and rewrite `_placed` to be backend-neutral:

```python
def _placed(be: PluginBackend, op: str, label: str, params: dict[str, Any], work: Any) -> dict:
    """Run one placement call as a single step, on whichever backend is active.

    The two journal owners answer differently and that is deliberate: the plugin's
    commit replies with its own record of the step (the DWG is the source of truth
    there), while a local journal commits to a StepRecord. Both are flattened to the
    same keys so a caller does not have to know which backend it is talking to.
    """
    begin = getattr(be, "begin_step", None)
    if begin is None or getattr(be, "open_step", ""):
        summary = work()
        return {"ok": True, **(summary if isinstance(summary, dict) else {})}
    begin(op, label=label, params=params)
    try:
        summary = work()
    except BaseException:
        be.abort_step()
        raise
    committed = be.commit_step()
    if isinstance(committed, dict):
        out = _outcome(committed, "apply")
    else:
        out = {
            "ok": True,
            "action": "commit",
            "message": "",
            "touched": [],
            "step": {"index": committed.index, "op": committed.op, "label": committed.label},
            "refusal": None,
            "journal": dict(be.info().get("journal", {})),
        }
    return {**out, **(summary if isinstance(summary, dict) else {})}
```

`_placement` is unchanged (it keeps converting `ValueError` to `BackendError`, which is what makes a bad variant or a refused width reach the client as a readable message). Change the type hint on `_placed`'s `be` to `Backend`. `_require_plugin` stays — `step_begin`/`step_commit`/`step_control` genuinely need the plugin.

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest -q` — all PASS — the plugin suites are the guard. The plugin suites are the guard that the result shape did not move.

- [x] **Step 5: Lint** `src/acadmcp/server.py tests/test_element_tools.py` — clean

---

### Task 5: `journal.py` — the payload carries a `meta` section, and a version guard

**Files:**
- Modify: `src/acadmcp/journal.py`
- Test: `tests/test_journal.py` (append)

**Interfaces:**
- Produces:
  - `Journal.meta -> dict`, `Journal.note_meta(**kv) -> None` (drops `None` values)
  - `to_dict()` → `{"version": 1, "meta": {...}, "steps": [...]}`
  - `from_dict` refuses a payload whose `version` is newer than this build understands, with a message naming both numbers
  - `Journal(meta={...})` constructor argument

**Why a version guard now:** every `from_dict` failure lands in the dxf reader's `journal_unreadable`, which is reported rather than raised — so a future format silently degrading to "no journal" is exactly the failure this project refuses elsewhere. Refusing a newer version is loud.

- [x] **Step 1: Write the failing tests**

```python
def test_meta_round_trips():
    j = Journal()
    j.note_meta(catalog={"digest": "abc"}, units="mm")
    clone = Journal.from_dict(j.to_dict())
    assert clone.meta["catalog"] == {"digest": "abc"} and clone.meta["units"] == "mm"


def test_note_meta_drops_nothing_values():
    j = Journal()
    j.note_meta(catalog=None, units="mm")
    assert "catalog" not in j.meta and j.meta["units"] == "mm"


def test_a_payload_without_meta_still_loads():
    """Every drawing saved before this change has no meta section."""
    j = Journal.from_dict({"version": 1, "steps": []})
    assert j.meta == {} and j.steps == []


def test_a_newer_journal_version_is_refused_by_number():
    with pytest.raises(ValueError) as caught:
        Journal.from_dict({"version": 2, "steps": []})
    assert "2" in str(caught.value) and "1" in str(caught.value)
```

- [x] **Step 2: Run** — expect FAIL

- [x] **Step 3: Implement** — `_meta` dict, `meta` property returning a copy (the steps list has the same discipline), `note_meta` filtering `None`, `to_dict` emitting `meta` only when non-empty (so a drawing with no meta keeps today's exact payload), and at the top of `from_dict`:

```python
    version = int(data.get("version", 1)) if isinstance(data, dict) else 1
    if version > 1:
        raise ValueError(
            f"journal version {version} is newer than this build writes (1); "
            "reading it could lose steps, so it is reported instead"
        )
```

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_journal.py tests/test_dxf_journal.py tests/test_recording.py -q` — all PASS

---

### Task 6: the drawing records the catalogue that drew it

**Files:**
- Modify: `src/acadmcp/catalog.py` (`identity()`, `adopt()`)
- Modify: `src/acadmcp/settings.py` (expose the resolved catalogue chain for the report)
- Modify: `src/acadmcp/backends/dxf.py` (stamp meta on create/save, adopt on open)
- Modify: `src/acadmcp/backends/com.py` (sidecar file beside the saved drawing)
- Modify: `src/acadmcp/server.py` (`drawing_new`/`drawing_open`/`drawing_save` stamp and adopt)
- Test: `tests/test_catalog_identity.py` (new), `tests/test_dxf_journal.py` (append)

**Interfaces:**
- Produces:
  - `Catalog.identity() -> dict` — `{"library": str, "files": [...], "digest": "sha256:...", "count": n, "elements": [...]}`; `elements` is the same shape `describe()` emits, so `Catalog.adopt()` can rebuild from it by reusing `_read_element`
  - `Catalog.adopt(payload) -> Catalog` — rebuild from an identity; tolerant, counting what it skips
  - the identity travels inside `Journal.meta["catalog"]`, so it persists wherever the journal does
  - `ComBackend` writes `<path>.acadmcp.json` beside a saved drawing and reads it on `drawing_open`
  - `drawing_status` gains `"catalog": {...}` (the loaded one) and `"drawing_catalog": {...}` (what the drawing recorded)

**Where the embedded catalogue ranks:** a project's own files win, then the copy embedded in the drawing, then the package's starter file. The local standard is the authority — a drawing from another office must not silently redraw its doors with someone else's blocks — but the difference has to be *reported*, and with no local catalogue the embedded copy is better than nothing.

- [x] **Step 1: Write the failing tests**

```python
"""The drawing carries the catalogue that drew it."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


def test_identity_survives_adopt(tmp_path):
    loaded = catalog.Catalog.load()
    again = catalog.Catalog.adopt(loaded.identity())
    assert again.keys() == loaded.keys()
    assert again.skipped == []


def test_the_digest_changes_when_an_entry_does():
    first = catalog.Catalog({"door": _element(layer="A-DOOR")}).identity()
    second = catalog.Catalog({"door": _element(layer="A-OTHER")}).identity()
    assert first["digest"] != second["digest"]


def test_a_saved_drawing_records_the_catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        catalog, "current", lambda paths=None: catalog.Catalog({"door": _element()})
    )
    be = make_backend("dxf")
    be.note_catalog(catalog.current())
    be.save(str(tmp_path / "plan.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    assert reopened.drawing_catalog()["catalog"]["count"] == 1
    assert reopened.drawing_catalog()["catalog"]["elements"][0]["key"] == "door"


def test_a_drawing_saved_without_a_catalogue_records_nothing(tmp_path):
    be = make_backend("dxf")
    be.save(str(tmp_path / "plain.dxf"))
    reopened = make_backend("dxf", path=str(tmp_path / "plain.dxf"))
    assert reopened.drawing_catalog()["catalog"] is None


def test_an_adopted_catalogue_keeps_the_entry_placeable(tmp_path):
    """The point of the embedded copy: reopening elsewhere still resolves the element."""
    be = make_backend("dxf")
    be.note_catalog(catalog.Catalog({"column": _op_element()}))
    be.save(str(tmp_path / "plan.dxf"))
    reopened = make_backend("dxf", path=str(tmp_path / "plan.dxf"))
    embedded = reopened.embedded_catalog()
    assert embedded.get("column").op == "column"
```

Define `_element()` and `_op_element()` locally in this module, the same shape `tests/test_catalog_ops.py` builds in Task 2 — this package's test modules each set up their own fixtures (every one does its own `sys.path.insert`), so copy them rather than importing across modules.

- [x] **Step 2: Run** — expect FAIL

- [x] **Step 3: Implement**

`catalog.py`:

```python
def identity(self, library: str = "") -> dict[str, Any]:
    """The catalogue as the drawing will remember it.

    The whole thing, not a reference to it: a catalogue is 7 kB and a drawing that
    records only a file path is a drawing that resolves to something else the moment
    it is opened on another machine. `elements` reuses `describe()`'s shape so
    `adopt` can rebuild through the same reader that built it.
    """
    payload = self.describe()
    payload["library"] = library or (
        next(iter(self.libraries.values()), "") if self.libraries else ""
    )
    payload["digest"] = self.digest()
    return payload


def digest(self) -> str:
    """A hash of the entries, so a difference is detectable without a diff."""
    rows = [self.elements[k].describe() for k in self.keys()]
    blob = json.dumps(rows, sort_keys=True, ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


@classmethod
def adopt(cls, payload: dict[str, Any]) -> Catalog:
    """Rebuild a catalogue from an identity embedded in a drawing.

    Flat entries, and tolerant, for the same reason loading is: a drawing whose
    embedded catalogue is damaged must still open. What could not be read is counted
    in `skipped` and reported.
    """
    catalog = cls()
    entries: list[Any] = []
    for items in (payload.get("categories") or {}).values():
        entries.extend(items)
    catalog.add_entries(entries, library=str(payload.get("library", "")), where="<drawing>")
    return catalog
```

Refactor `add_file`'s per-entry loop into `add_entries(entries, library, where)` so `add_file` and `adopt` share it — one reader, so an adopted entry cannot mean something a file entry does not.

`RecordingBackend` gains two methods, since it is where the local journal lives:

```python
def note_catalog(self, loaded: Catalog) -> None:
    """Record which catalogue this drawing is being drawn with.

    Stamped when the drawing starts and again on save, so the drawing says what it
    was drawn with even if the catalogue file is later edited. The library root rides
    along as a plain path in the payload — not as a `{library}` token — because the
    copy in the drawing has to be usable as it stands on the machine that opens it.
    """
    self.journal.note_meta(catalog=loaded.identity(settings.current().library))


def drawing_catalog(self) -> dict[str, Any]:
    """What the drawing recorded, and whether the catalogue loaded now agrees.

    A disagreement is reported, never acted on: the local standard is the authority,
    and redrawing somebody else's doors with this office's blocks is the failure this
    whole mechanism exists to prevent.
    """
    recorded = self.journal.meta.get("catalog")
    loaded = catalog.current()
    out: dict[str, Any] = {
        "catalog": recorded,
        "loaded": {"count": len(loaded.elements), "digest": loaded.digest()},
    }
    if recorded and recorded.get("digest") != out["loaded"]["digest"]:
        out["warning"] = (
            "this drawing was drawn with a different catalogue ("
            f"{recorded.get('count')} elements, {recorded.get('library') or 'no library'}); "
            "the one loaded now wins, but the elements may not be the ones it was drawn with"
        )
    return out
```

`DxfBackend` needs no persistence work beyond passing meta through — it already writes `journal.to_dict()` into the XRecord on save and reads it on open. Add `embedded_catalog()` returning the adopted `Catalog` or `None`, and have `drawing_open` prefer, in order: the project's files, the embedded copy, the package default.

`ComBackend`: `save()` writes `Path(target).with_suffix(Path(target).suffix + ".acadmcp.json")` containing `journal.to_dict()` when there are steps or meta; `__init__`/`drawing_open` reads it when present. Say in the tool result that a sidecar was written — a file appearing next to the user's drawing unannounced is not acceptable. Document the reason in `AGENTS.md`: COM cannot write an XRecord through ActiveX, and the plugin backend is the durable route; the sidecar is what makes a COM session's rollback survive a save without asking the user to run the plugin.

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest -q` — all PASS, including the pre-existing `test_dxf_journal.py` round-trip tests

---

### Task 7: `summary.py` — read the plan as data, no vision

**Files:**
- Create: `src/acadmcp/summary.py`
- Test: `tests/test_summary.py` (new)

**Interfaces:**
- Consumes: `geom`, `layers`; step rows shaped `{"index", "op", "label", "params", "state"}` — the shape both `Journal.steps` and the plugin's `step_list` produce (the plugin's rows carry `params` as a JSON object; `describe_steps` already relies on `index`/`state`/`op`/`label`)
- Produces: `summarise(rows: Sequence[dict], *, catalog: dict | None = None) -> dict`

**This is the agent's new semantic view, and it must be derived, never remembered.** Areas are recomputed from the recorded boundary with `geom.inner_boundary` + `geom.area`, exactly as `draw_room` computes them, so the summary cannot drift from the geometry — if it ever disagrees with `draw_room`'s reply, the summary is wrong, not the drawing.

Shape:

```python
{
    "steps": {"total": 4, "applied": 3, "undone": 1, "next_index": 5},
    "rooms": [
        {
            "step": 2,
            "label": "车间",
            "name": "车间",
            "area_m2": 42.56,
            "boundary": [[0, 0], [8000, 0], [8000, 6000], [0, 6000]],
            "thickness": 200,
        }
    ],
    "openings": [
        {"step": 1, "kind": "door", "tag": "M1521", "width": 1521, "swing": "left", "element": None}
    ],
    "elements": [
        {
            "step": 3,
            "type": "door",
            "variant": "900",
            "at": [[0, 0]],
            "rotation": 0.0,
            "label": "门 900",
        }
    ],
    "structure": {"columns": 6, "stairs": 1, "lifts": 1, "shafts": 2, "equipment": 0},
    "annotation": {"dimensions": 8, "texts": 4, "leaders": 1, "axis_grids": 1},
    "layers": ["A-ANNO", "A-WALL", ...],
    "catalog": {"count": 9, "digest": "sha256:...", "library": "..."},
    "warnings": ["step 4 is undone: the drawing is mid-edit"],
}
```

Undone steps contribute to `steps` and to `warnings`, never to `rooms`/`openings`/`elements` — the drawing does not contain them.

- [x] **Step 1: Write the failing tests**

```python
"""The design summary, recomputed from the journal's recorded intent."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import summary  # noqa: E402


def _step(index, op, params, state="applied", label=""):
    return {"index": index, "op": op, "label": label, "params": params, "state": state}


ROOM = _step(
    1,
    "draw_room",
    {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "name": "会议室", "thickness": 200},
    label="会议室",
)
WALL = _step(
    2,
    "draw_wall_with_openings",
    {
        "points": [[0, 0], [6000, 0]],
        "thickness": 240,
        "openings": [{"start": 1000, "width": 900, "kind": "door", "tag": "M1521"}],
    },
)
DOOR = _step(3, "place_element", {"type": "door", "at": [1000, 0], "variant": "900"})


def test_rooms_carry_the_area_the_room_tool_would_report():
    out = summary.summarise([ROOM])
    assert out["rooms"][0]["name"] == "会议室"
    # 4000x3000 ring, 200 walls -> 3.8 x 2.8
    assert out["rooms"][0]["area_m2"] == 3.8 * 2.8


def test_openings_are_listed_with_their_tag_and_width():
    out = summary.summarise([WALL])
    assert out["openings"] == [
        {
            "step": 2,
            "kind": "door",
            "tag": "M1521",
            "width": 900.0,
            "swing": "left",
            "element": None,
        }
    ]


def test_placed_elements_are_listed_once_per_call():
    out = summary.summarise([DOOR])
    assert out["elements"][0]["type"] == "door" and out["elements"][0]["variant"] == "900"


def test_a_repeating_placement_reports_its_count():
    out = summary.summarise(
        [_step(1, "place_along", {"type": "column", "start": [0, 0], "end": [6000, 0], "count": 3})]
    )
    assert out["elements"][0]["count"] == 3
    assert out["structure"]["columns"] == 3


def test_an_undone_step_is_counted_but_not_in_the_plan():
    out = summary.summarise([ROOM, _step(2, "draw_wall", {"points": [[0, 0], [1, 0]]}, "undone")])
    assert out["steps"] == {"total": 2, "applied": 1, "undone": 1, "next_index": 3}
    assert len(out["rooms"]) == 1
    assert any("undone" in w for w in out["warnings"])


def test_a_room_with_no_name_is_still_reported():
    out = summary.summarise(
        [_step(1, "draw_room", {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]})]
    )
    assert out["rooms"][0]["name"] == "" and out["rooms"][0]["area_m2"] > 0


def test_layers_are_collected_from_the_steps():
    out = summary.summarise(
        [_step(1, "draw_line", {"p1": [0, 0], "p2": [1, 0], "layer": "A-WALL"})]
    )
    assert "A-WALL" in out["layers"]


def test_an_unreadable_step_is_warned_about_not_raised():
    out = summary.summarise([{"op": "draw_room"}])
    assert out["steps"]["total"] == 1
    assert out["warnings"]
```

- [x] **Step 2: Run** — expect FAIL

- [x] **Step 3: Implement** `src/acadmcp/summary.py`:

```python
"""What the drawing contains, read back as data instead of looked at.

An AI client had two ways to check its own work: a screenshot, or counting entities on a
layer. Neither answers "how many doors, and what sizes". This module answers it from the
journal — the steps already record the op and the arguments that produced it — so the
answer needs no vision, works on every backend, and survives a reopen because the journal
persists.

Everything here is *derived, never remembered*. A room's area is recomputed from its
recorded boundary with the same `geom.inner_boundary` call `draw_room` makes, so the
summary cannot drift from the geometry: if the two ever disagree, this module is wrong.

CAD-free by construction: it may import `geom` and `layers` and nothing else.
"""
```

One function per op family, a dispatch dict, and a tolerant main entry:

```python
def summarise(rows: Sequence[dict], *, catalog: dict | None = None) -> dict[str, Any]:
    """The plan as data: rooms, openings, elements, counts, layers, warnings."""
```

Handling rules to write down in the module docstring, because they are decisions:

- An unreadable row (no `op`) is counted in `steps.total` and warned about, never raised.
- A row whose `params` is missing or malformed yields a warning naming the step number.
- `place_element` / `place_along` / `place_grid` contribute `elements`; the last two also contribute their count to `structure` by looking the element's category up in the catalogue when it is known, and to `elements[].count` always.
- `structure` counts `draw_stair` / `draw_lift` / `draw_shaft` / `draw_equipment` and catalogue elements whose category is `structure`.
- `annotation` counts `add_dimension` / `add_text` / `add_leader_note` / `add_axis_grid` / `add_hatch`.
- `layers` is the set of every `layer` value recorded in any step's params, sorted.

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_summary.py -q` — PASS

- [x] **Step 5: Lint** — clean, and confirm `summary.py` imports no backend: `.venv/Scripts/python.exe -c "import acadmcp.summary"` must not import ezdxf (checked by the module's own imports, reviewed by eye)

---

### Task 8: `drawing_summary`, and the tool surface tells the truth about backends

**Files:**
- Modify: `src/acadmcp/server.py`
- Modify: `src/acadmcp/backends/base.py` (`step_rows()` on the protocol)
- Modify: `src/acadmcp/backends/recording.py`, `src/acadmcp/backends/plugin.py` (implement it)
- Test: `tests/test_tool_surface.py` (update the count), `tests/test_server_summary.py` (new)

**Interfaces:**
- Produces:
  - `Backend.step_rows() -> list[dict]` — the journal as rows: `RecordingBackend` maps its `StepRecord`s, `PluginBackend` asks the DWG over RPC
  - tool `drawing_summary() -> dict` (read-only) — `{"ok": True, **summarise(be.step_rows(), catalog=...)}}`
  - `element_types` reports, per element, `"placement"`: `"block"` / `"parametric"` / `"both"` and, when the active backend cannot draw it, why

- [x] **Step 1: Write the failing tests**

```python
"""The summary tool, and the surface that tells an agent where an element can go."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_session():
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_summary_reports_the_rooms_that_were_drawn():
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    out = server.drawing_summary()
    assert out["ok"] and out["rooms"][0]["name"] == "会议室"
    assert out["rooms"][0]["area_m2"] == pytest.approx(3.8 * 2.8, abs=0.01)


def test_summary_survives_save_and_reopen(tmp_path):
    """The reason this is derived from the journal rather than from entities."""
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    target = str(tmp_path / "plan.dxf")
    server.drawing_save(target)
    server.drawing_open(target)
    assert server.drawing_summary()["rooms"][0]["name"] == "会议室"


def test_summary_says_which_catalogue_drew_it(monkeypatch):
    server.draw_line([0, 0], [1000, 0])
    out = server.drawing_summary()
    assert out["catalog"] is not None and "digest" in out["catalog"]


def test_element_types_says_where_an_element_can_be_placed(monkeypatch):
    out = server.element_types(check_library=False)
    door = out["categories"]["opening"][0]
    assert door["placement"] in ("block", "parametric", "both")
```

- [x] **Step 2: Run** — expect FAIL (`drawing_summary` missing)

- [x] **Step 3: Implement**

`drawing_summary` follows `drawing_history`'s shape and its reason for existing:

```python
@mcp.tool(annotations=_READONLY)
def drawing_summary() -> dict[str, Any]:
    """What the drawing contains, as data: rooms with areas, openings, elements, layers.

    Derived from the journal, which records each step's op and arguments, so it needs no
    screen and survives a reopen on the headless backend. Read this before a screenshot:
    it answers "how many doors, what sizes, what is the floor area" exactly, and a
    screenshot cannot answer it at all.
    """
```

`RecordingBackend.step_rows()`:

```python
def step_rows(self) -> list[dict[str, Any]]:
    """The journal as rows, the shape the plugin reports its steps in."""
    return [
        {
            "index": s.index,
            "op": s.op,
            "label": s.label,
            "params": s.params,
            "state": s.state,
            "entities": len(s.handles),
        }
        for s in self.journal.steps
    ]
```

`PluginBackend.step_rows()` returns `self.client.call("get_steps", offset=0, limit=10000)["steps"]`.

**The catalogue has to be stamped where a drawing begins, or `test_summary_says_which_catalogue_drew_it` has nothing to report.** Add `be.note_catalog(catalog.current())` (Task 6) to the two places a drawing starts: `_Session.require()`'s scratch-document branch, and `drawing_new`/`drawing_open` after `make_backend`. It is a no-op on a backend without `note_catalog`, so guard with `getattr` — the plugin records its catalogue in the DWG only once Task 12 lands, and until then it reports the loaded one with a note saying so.

`element_types` adds `"placement"` per element from `Element.kind`, plus — when AutoCAD is not reachable — a line saying so, since `placement` there is a property of the entry and not of the active backend.

- [x] **Step 4: Update `tests/test_tool_surface.py`**: the count becomes 46; `drawing_summary` joins `read_only`; `drawing_rollback_to` joins `destructive`.

- [x] **Step 5: Run** `.venv/Scripts/python.exe -m pytest -q` — all PASS

---

### Task 9: `drawing_rollback_to` — aim the undo, don't count it

**Files:**
- Modify: `src/acadmcp/server.py` (`drawing_rollback_to`, `drawing_history`)
- Modify: `src/acadmcp/backends/recording.py` (`rollback_to`)
- Modify: `src/acadmcp/backends/plugin.py` (`rollback_to` delegating to the DWG)
- Test: `tests/test_server_journal.py` (append)

**Interfaces:**
- Produces:
  - `RecordingBackend.rollback_to(index) -> dict` — erase every applied step after `index`, newest first, reusing `_erase`
  - `PluginBackend.rollback_to(index) -> dict` — `session.rollback_to(index)`, because the DWG journal is the authority there
  - tool `drawing_rollback_to(index) -> dict` (destructive)
  - `drawing_history` gains `"steps"`: the rows from `step_rows()`, so an agent can pick an index

- [x] **Step 1: Write the failing tests**

```python
def test_rollback_to_leaves_the_named_step_standing():
    server.draw_line([0, 0], [1000, 0])
    server.draw_circle([500, 500], 100)
    server.draw_line([0, 0], [0, 1000])
    report = server.drawing_rollback_to(1)
    assert report["ok"] and [s["index"] for s in report["undone"]] == [3, 2]
    assert server._state.backend.info()["entities"] == 1


def test_rollback_to_zero_empties_the_drawing():
    server.draw_line([0, 0], [1000, 0])
    server.drawing_rollback_to(0)
    assert server._state.backend.info()["entities"] == 0


def test_rollback_to_an_index_past_the_end_changes_nothing():
    server.draw_line([0, 0], [1000, 0])
    report = server.drawing_rollback_to(99)
    assert report["undone"] == [] and server._state.backend.info()["entities"] == 1


def test_history_carries_machine_readable_rows():
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    rows = server.drawing_history()["steps"]
    assert rows[0]["index"] == 1 and rows[0]["op"] == "draw_room"
    assert rows[0]["params"]["name"] == "会议室"
```

- [x] **Step 2: Run** — expect FAIL

- [x] **Step 3: Implement** — `RecordingBackend`:

```python
    def rollback_to(self, index: int) -> dict[str, Any]:
        """Erase everything drawn after step `index`, newest first.

        `drawing_undo(count)` makes a caller count steps; this lets it name the step it
        wants to be standing. Index 0 is before the first step, which is what empties a
        drawing.
        """
        doomed = self.journal.rollback_to(int(index))
        erased = missing = 0
        for step in doomed:
            e, m = self._erase(step.handles)
            erased += e
            missing += m
        return {
            "undone": [{"index": s.index, "op": s.op, "label": s.label} for s in doomed],
            "erased": erased,
            "missing": missing,
        }
```

`PluginBackend.rollback_to` calls `StepSession.rollback_to(index)` and shapes the same keys. The tool mirrors `drawing_undo`'s body, raising `BackendError` when the backend has no `rollback_to` — and note in its docstring that on the plugin an *accepted* step blocks a rollback unless `step_control(force=True)` is used, so the refusal is reported rather than swallowed.

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest -q` — all PASS

---

### Task 10: stop the silent wrong answer — magnitude guards

**Files:**
- Modify: `src/acadmcp/geom.py` (`plausible`)
- Modify: `src/acadmcp/ops.py` (`draw_room`, `add_room_label`, `draw_column`, `draw_wall_with_openings`)
- Test: `tests/test_input_validation.py` (append)

**Interfaces:**
- Produces: `geom.plausible(value: float, what: str, low: float, high: float) -> float` — returns the value, or raises `ValueError` naming the value, the range, and the metre/millimetre suspicion

**What this is for:** a client that passes metres draws a 4 mm room and gets `ok: true`. Nothing in this server catches that today, and an AI that cannot see the drawing will not notice. The bounds are deliberately **wide** — they catch a unit slip and nothing else. A guard that refuses a legitimate 300 mm partition is worse than no guard.

- [x] **Step 1: Write the failing tests**

```python
def test_a_room_given_in_metres_is_refused_with_the_reason():
    with pytest.raises(BackendError) as caught:
        server.draw_room([[0, 0], [4, 0], [4, 3], [0, 3]], name="会议室", thickness=0.2)
    text = str(caught.value)
    assert "4" in text and "millimetre" in text


def test_a_millimetre_room_still_draws():
    out = server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    assert out["ok"] and out["area_m2"] > 0


def test_a_column_smaller_than_a_stud_is_refused():
    with pytest.raises(BackendError) as caught:
        server.draw_column([0, 0], 0.6)
    assert "millimetre" in str(caught.value)


def test_a_wall_too_thin_to_draw_is_refused():
    with pytest.raises(BackendError):
        server.draw_wall([[0, 0], [5000, 0]], thickness=0.2)
```

- [x] **Step 2: Run** — expect FAIL (today all four quietly draw)

- [x] **Step 3: Implement** — `geom.plausible`:

```python
def plausible(value: float, what: str, low: float, high: float) -> float:
    """A millimetre dimension, or a refusal that names the likely slip.

    Everything here is millimetres, and a client that passes metres draws a 4 mm room and
    is told it succeeded. The bounds are deliberately wide: they are here to catch a unit
    slip, not to police a design, and a guard that refuses a 300 mm partition would be
    worse than no guard at all.
    """
    number = float(value)
    if low <= number <= high:
        return number
    raise ValueError(
        f"{what} is {number} mm, outside the {low:g}-{high:g} mm this package draws in; "
        "everything is millimetres — if this was metres, multiply by 1000"
    )
```

Apply, with the bounds written as named constants next to their use:

- `draw_wall_run` / `draw_wall_with_openings`: `thickness` in 20–2000.
- `draw_room` / `add_room_label`: `thickness` in 20–2000, and the ring's **inner** width from `geom.inner_boundary` — after the existing `ring_self_intersects` check — in 500–200000. The inner span is the right thing to measure: it is what a room actually has to contain, and it is the number a unit slip destroys.
- `draw_column`: `size` in 50–3000.
- `add_dimension`: leave as is; a coincident pair already raises.

- [x] **Step 4: Run** `.venv/Scripts/python.exe -m pytest -q` — all PASS. If the demo or an existing test trips a bound, the bound is too tight: widen it, do not adjust the caller.

---

### Task 11: make the surface self-explanatory — `drawing_help`, `AGENTS.md`, `README.md`

**Files:**
- Modify: `src/acadmcp/server.py` (`drawing_help`)
- Modify: `AGENTS.md`, `README.md`

- [x] **Step 1: `drawing_help` gains the two decisions an agent currently has to guess.** Add to the returned dict:

```python
        "choosing_a_tool": {
            "catalogue_or_parametric": (
                "A catalogue element is a specific named artefact — a model number, a "
                "schedule row, a standard section — and it is placed with place_element. "
                "Everything whose identity is just numbers you already have (a wall, a "
                "room, a column by size, a lift shaft by its corners) is drawn by its own "
                "tool. place_element also draws parametric twins where the library cannot "
                "be inserted, so one element name works on every backend."
            ),
            "checking_your_work": (
                "drawing_summary first — rooms, areas, openings and counts as data. "
                "drawing_history is what was drawn, one line per step. "
                "drawing_screenshot last, and pass `window` to frame a detail."
            ),
            "recovering": (
                "Every tool call is one journal step: drawing_undo(count) or "
                "drawing_rollback_to(index) takes back exactly what was drawn, and "
                "drawing_history gives the indices to aim at."
            ),
        },
```

and to `conventions`: `"units": "millimetres everywhere — a metres-valued call is refused, not drawn"`.

- [x] **Step 2: `AGENTS.md`** — architecture rules gain one line each for `catalog_ops.py` (adapters, not geometry), `summary.py` (derived, never remembered; CAD-free), and the catalogue's `op`/`width_param`. Then Gotchas:

```markdown
**A catalogue entry has two ways to reach the paper, and the backend picks.** `placement_for`
takes the library block when the backend can insert one (the plugin does; `dxf` and `com`
refuse) and the parametric twin otherwise. Declared, not probed — inside a step the
refusals are deferred to the commit, so a caller cannot find out by trying. This is why
`place_element` works headless now; before it, `_require_plugin()` gated all three
placement tools and an AI writing a plan in DXF could not place one door.

**The drawing carries the catalogue that drew it.** `Journal.meta["catalog"]` holds the
identity *and the entries*, because a drawing that records only a file path resolves to
something else on the next machine. The DXF keeps it in the same `ACADMCP` XRecord as the
journal; COM writes a `<drawing>.acadmcp.json` sidecar beside the save, because ActiveX
exposes no way to write an XRecord and the plugin is the durable route. Precedence on
open: the project's own files, then the embedded copy, then the package's starter.
`drawing_summary` reports which one is in force, and a mismatch is a warning rather than
a silent redraw.

**`summary.py` derives, it never remembers.** Room areas are recomputed from the recorded
boundary with the same `geom.inner_boundary` call `draw_room` makes. A summary that
disagrees with `draw_room`'s own reply is the summary's bug. It reads the journal, so it
works after a reopen and on every backend — and nothing in it may import a backend.
```

- [x] **Step 3: `README.md`** — tool surface gains `drawing_summary` and `drawing_rollback_to`; the backend comparison table gains a row for the catalogue, saying which backends can insert blocks and that the rest draw parametric twins; the "Library elements" paragraph says one element name works on all three backends.

- [x] **Step 4: Full verification** — `.venv/Scripts/python.exe -m pytest -q`; `.venv/Scripts/python.exe -m ruff check . --output-format concise`; `.venv/Scripts/python.exe -m ruff format .`; `.venv/Scripts/python.exe examples/floor_plan_demo.py` (DXF + PNG still produced); `git status evaluations/fixtures/floor_plan.dxf` clean.

- [x] **Step 5: Live check, once, by hand** — `.venv/Scripts/python.exe tests/sandbox_driver.py <script>` with a script that draws a small plan on `dxf`, calls `drawing_summary`, rolls back to step 2, saves, reopens and calls `drawing_summary` again. Report the printed git HEAD and dirty-file count with the result, per `AGENTS.md`.

---

### Task 12 (optional, needs AutoCAD): the plugin records the catalogue in the DWG

**Files:**
- Modify: `plugin/src/AcadMcp.Core/StepRecord.cs` (a `JournalMeta` record), `plugin/src/AcadMcp.Core/Journal.cs`, `plugin/src/AcadMcp.Plugin/Rpc/` (a `note_meta` method), `src/acadmcp/backends/plugin.py`

**Cost, stated plainly:** a loaded .NET assembly cannot be unloaded from AutoCAD, so this costs an AutoCAD restart to try, and `plugin/scripts/selftest.sh --engine` must be re-run. Do it only if durable catalogue identity on the plugin path is worth that — Task 6 already covers `dxf` and `com`, and `drawing_summary` on the plugin reports the loaded catalogue with a note saying the DWG did not record one.

## Execution Waves (subagents)

Wave 1 must land first: Tasks 1–2 define `Element.placement_for`, `width_key` and `CATALOG_OPS`, and every later task pins against them.

1. **Wave 1** (one agent): Tasks 1–2 — `catalog.py`, `catalog_ops.py`, `ops.place_element`, `supports_insert` on the protocol and the two backends.
2. **Wave 2** (three parallel agents, disjoint files):
   - Task 3 — `ops.py`'s opening branch (and its tests in `test_catalog_ops.py`).
   - Task 4 — `server.py`'s placement tools.
   - Tasks 5–6 — `journal.py`, `catalog.py`'s `identity`/`adopt`, `dxf.py`, `com.py`, and the `drawing_new`/`drawing_open`/`drawing_save` wiring.

   Note: Task 6 touches `catalog.py`, which Task 1 also edits. Task 1 has landed by then, so this is a serialised edit, not a conflict — but the Task 5–6 agent must re-read `catalog.py` rather than assume Task 1's shape.
3. **Wave 3** (two parallel agents): Task 7 (`summary.py`, new file) ∥ Task 10 (`geom.py` + `ops.py` guards). Task 10 touches `ops.py` after Task 3, so it waits for Wave 2.
4. **Wave 4** (one agent): Tasks 8–9 (`server.py`: summary, rollback, history, `step_rows`) then Task 11 (docs) and the full green run with the sandbox check.
5. Task 12 is standalone and may be skipped.

## Verification, once, at the end

```bash
.venv/Scripts/python.exe -m pytest -q                       # 312, all PASS
.venv/Scripts/python.exe -m ruff check . --output-format concise
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe examples/floor_plan_demo.py        # DXF + PNG still produced
```

Then the check that proves backwards compatibility, which is a geometry comparison rather than a `git status`:

```bash
rm -rf /tmp/pristine && mkdir -p /tmp/pristine
git archive HEAD | tar -x -C /tmp/pristine
cd /tmp/pristine && PYTHONPATH=/tmp/pristine/src <repo>/.venv/Scripts/python.exe examples/floor_plan_demo.py
```

and fingerprint `(dxftype, layer, text, height, insert/points)` for every entity, as a `Counter`, on three files: the committed fixture, the pristine tree's output, and this tree's output. **The last two must be equal.** That is the assertion that says no existing geometry moved — and it is the only one that works, because the fixture is not byte-reproducible (timestamp, random GUIDs, and a `BLOCK_RECORD` ordering that hash randomisation sometimes swaps), so it is dirty after every regeneration even when nothing moved.

Wave 2 ran this check: pristine-HEAD output and this tree's output were equal at 247 entities each, while the committed fixture differed in exactly two `A-ROOM-AREA` text heights (250.0 → 240.0). That is a **pre-existing staleness, not a regression**: the fixture was last written in `e3d76f1` while `layers.fit_text_height` — which shrinks a room's area text to fit a 1200 mm room, 1200 × 0.9 ÷ (6 × 0.75) = 240 — landed in `7da48c5`, one commit later. The fixture has been refreshed from HEAD's own output; the entity count is unchanged at 247, so the answers in `evaluation.xml` (which reference entity counts, never a text height or an area value) still hold.
