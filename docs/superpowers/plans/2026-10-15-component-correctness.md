# Component Correctness: Critique Framework + Element Capture — Implementation Plan

> **For agentic workers:** Implement your assigned workstream of this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make recurring component errors (the misdrawn door that stays fixed only for one session) structurally impossible: (A) a component-level critique framework that checks the drawing against its own journal, plus a `drawing_finalize` hard gate; (B) an `element_capture` closure that turns verified-good geometry into a reusable catalogue element — the fix becomes code and data, not chat history.

**Architecture:** Two independent workstreams sharing no files; the orchestrator wires the three new tools into `server.py`/`terms.py` at integration. A adds a pure `critique.py` (reads `step_rows`, the same shape `summary.py` consumes; imports `geom`/`layers` only) plus one `geom` helper. B adds capture machinery: `create_block` on the dxf backend (block definitions are document state, like the page setup — never journalled, never undone), an `insert` primitive so captured blocks place on dxf, runtime `Catalog.register` (the existing stamp-on-save then carries new entries inside the drawing), and `capture_ops.py` orchestrating.

**Tech Stack:** mcp 2.3.0; ezdxf (`doc.blocks.new`, `msp.add_blockref`); the journal's `step_rows()` (`index`, `op`, `label`, `params`, `state`).

## Global Constraints

- Run tests ONLY with `.venv/Scripts/python.exe -m pytest ...` from the repo root — never `uv run pytest`.
- `critique.py` imports nothing CAD-related and no backend; `geom` and `layers` only. `capture_ops.py` follows the `ops.py` rule: backend-agnostic, geometry via `geom`.
- Rows whose params cannot be read are **counted and reported, never raised** — the rule `summary.py`, the catalogue loader and the journal reader already follow.
- `AGENTS.md`, `README.md`, `scripts/check_doc_numbers.py`, `src/acadmcp/server.py`, `src/acadmcp/terms.py`, `src/acadmcp/prompts.py` are **owned by the orchestrator** — agents must not touch them.
- Uncommitted changes exist in the tree; edits additive only, never a revert.
- New public behaviour gets a test first (RED), then implementation (GREEN).

---

## Workstream A: the critique framework

**Files:**
- Modify: `src/acadmcp/geom.py` (add one helper)
- Create: `src/acadmcp/critique.py`
- Create: `tests/test_critique.py`

**Interfaces (produced for the orchestrator):**
- `geom.dist_point_segment(p, a, b) -> float`
- `critique.Issue` — dataclass `code: str, severity: str, message: str, step: int | None, detail: dict` with `to_dict()`.
- `critique.CHECKS: dict[str, Callable[[list[dict], dict[str, str]], list[Issue]]]` — check code → function(rows, tag_prefixes).
- `critique.run(rows: list[dict], tag_prefixes: dict[str, str], focus: str = "") -> dict` — returns `{"issues": [issue.to_dict()...], "counts": {"error": n, "warning": n, "info": n}, "rows_checked": n, "unreadable": n, "focus": focus}`. Unknown `focus` raises `ValueError` naming the valid codes.

- [ ] **Step 1: RED** — write `tests/test_critique.py` (below), watch it fail (module missing).

- [ ] **Step 2: geom helper** — append to `geom.py` (with the others, after `dist`):

```python
def dist_point_segment(p: Point, a: Point, b: Point) -> float:
    """Distance from p to the segment ab — not to the infinite line."""
    ax, ay = xy(a)
    bx, by = xy(b)
    px, py = xy(p)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return dist(p, (ax + t * dx, ay + t * dy))
```

- [ ] **Step 3: `critique.py`** — module docstring must state: pure (geom/layers only, no CAD); reads the journal's own account, never XDATA (the rival's XDATA-gated critique reports "0 issues" on drawings it did not stamp — a false negative this design cannot produce, because the journal is present on every drawing this server made, on every backend). Contents:

```python
@dataclass
class Issue:
    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    step: int | None
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]: ...
```

`_rows(rows, ops)` helper: applied rows only (anything but state `"undone"`/`"rejected"` counts as applied — mirror summary.py's state handling), filtered to the named ops; unreadable rows (params not a dict) counted via a module-level tally passed through — simplest: `run()` pre-filters rows into `(applied, unreadable_count)` and hands each check only applied rows.

The six checks (each `fn(rows, tag_prefixes) -> list[Issue]`):

1. `door_without_opening` — for every `draw_door` row: wall centrelines are the `points` of applied `draw_wall` and `draw_wall_with_openings` rows, plus `boundary` of `draw_room` rows whose params do not say `walls=False`. Tolerance 1.0 mm via `geom.dist_point_segment` per segment. Hinge near no centreline → **error** "door is not on any recorded wall". Hinge on a wall but no recorded opening overlapping it → **warning** "door drawn on an uncut wall; the opening belongs in draw_wall_with_openings". Overlap test: project the hinge onto its nearest centreline segment to get an along-run distance `d` (accumulate segment lengths); an opening `{start, width}` in that run's params overlaps when `start - tol <= d <= start + width + tol` with `tol = max(width, door width) / 2` (hinge sits at the opening's jamb, so measure the door's mid-point too: `d + door_width/2` must fall inside the interval within tol — keep it simple: overlap iff `|d + w/2 - (start + ow/2)| <= (w + ow)/2`).
2. `opening_fits_run` — every opening in a `draw_wall_with_openings` row: `L = geom.polyline_length(points)` (+ closing segment when the ring is closed per `geom.is_closed`); `start < 0` or `start + width > L + 1e-6` → **error** naming the tag/start.
3. `tag_format` — explicit tags only: opening `"tag"`s, `draw_door`/`draw_window` `tag` params. Expected pattern: catalogue prefix for the kind (`tag_prefixes` maps `"door" -> "M"` etc.) + exactly 4 digits; kinds with no catalogue entry fall back to `^[A-Z]+\d{4}$`. Mismatch → **warning** naming the tag.
4. `tag_duplicates` — the same explicit tag recorded on two applied rows → **warning** listing both step indices.
5. `dimension_like_text` — `add_text` rows whose `text` matches `re.compile(r"(Ø|⌀|∅|%%c|%%C|R\d|±|%%p|\d+\s*[×x]\s*\d+|\d+\.\d+\s*mm)", re.IGNORECASE)` after removing standard-part designations `re.compile(r"\b(?:ISO|DIN|GB)\s*\d+(?:-\d+)?\s*-?\s*M\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?", re.IGNORECASE)` (a designation names a part, it does not measure one — idea borrowed from the rival's validator, with GB added) → **warning** "text masquerades as a dimension; use add_dimension".
6. `sheet_viewport_fit` — if an applied `drawing_sheet` row exists: plan extent = `geom.bbox` over every recorded `points`/`boundary`/`at`/`center`/`hinge`/`pos` coordinate in applied rows (ignore sheet rows' own paper geometry — those carry `paper` intent; simplest: skip rows whose op is `drawing_sheet`); compare against the sheet's viewport model size from `sheet.py` (`resolve_scale`, `SHEETS`, the viewport/view-height helpers the tool itself uses — read `sheet.py` and reuse its own functions so the check cannot drift from the tool). Plan exceeds viewport → **warning** naming `sheet.largest_scale_that_fits(...)`'s answer.

`run()` and the `CHECKS` dict keyed by the codes above. `focus` selects one check by code.

- [ ] **Step 4: tests** — `tests/test_critique.py` builds row dicts **by hand** (rows are plain data; no backend needed) plus one end-to-end pass through a dxf backend for realism:
  - door on a recorded opening → no issue; door on an uncut wall → warning; floating door → error;
  - opening past the run end → error; negative start → error;
  - tag `"M921"` → warning; tag `"M0921"` → clean; two `"M0921"` → duplicate warning;
  - `add_text` "Ø78" → warning; "ISO 4014 - M12x60" → clean;
  - sheet fit: 40000 mm plan on A3 at 1:100 → warning naming a larger-fit scale; at 1:200 → clean;
  - `run(focus="door_without_opening")` runs only that check; `focus="bogus"` raises ValueError;
  - an unreadable row (params = None) is counted in `unreadable`, never raised;
  - an undone row is invisible to every check.
  Also add `geom.dist_point_segment` cases to `tests/test_geom.py` (point on segment, past the end, degenerate zero-length segment).

- [ ] **Step 5: GREEN** — implement until `tests/test_critique.py` and the new geom cases pass. Run: `.venv/Scripts/python.exe -m pytest tests/test_critique.py tests/test_geom.py -q`.

- [ ] **Step 6: ruff** — `uv run --no-sync ruff check src/acadmcp/critique.py tests/test_critique.py src/acadmcp/geom.py && uv run --no-sync ruff format` same files.

---

## Workstream B: element capture

**Files:**
- Modify: `src/acadmcp/backends/base.py` (contract + capability attribute)
- Modify: `src/acadmcp/backends/recording.py` (step-primitives accessor)
- Modify: `src/acadmcp/backends/dxf.py` (`create_block`, `insert`, the `insert` primitive)
- Modify: `src/acadmcp/catalog.py` (`Catalog.register`)
- Modify: `src/acadmcp/catalog_ops.py` (the `insert_captured` adapter)
- Create: `src/acadmcp/capture_ops.py`
- Create: `tests/test_capture.py`

**Interfaces (produced for the orchestrator):**
- `capture_ops.capture(be, *, steps: list[int], key: str, label: str = "", tag_prefix: str = "", layer: str = "", category: str = "captured", base_point: list[float] | None = None) -> dict` — returns `{"block": name, "key": key, "entities": n, "base_point": [x, y]}`.
- `DxfBackend.create_block(name: str, primitives: list[dict], base_point) -> dict`; `DxfBackend.insert(...)` per the base signature; `Catalog.register(payload: dict) -> Element`.

- [ ] **Step 1: RED** — `tests/test_capture.py` (below), watch it fail.

- [ ] **Step 2: contract** — in `base.py`, beside `supports_insert`: `supports_block_create: bool = False` class attribute with the same "declared, not probed" comment; add `create_block(self, name: str, primitives: list[dict], base_point: Point) -> Any` to the contract with a docstring stating: a block definition is **document state** — it is not journalled, rollback never erases it (the same rule as the page setup and the plugin's `EnsureBlock`); only the dxf backend implements it this wave; the others raise `BackendError` naming the capability. Mirror exactly how `insert`'s default refusal is expressed (find where the non-plugin `insert` refusal lives and put `create_block`'s beside it).

- [ ] **Step 3: dxf implementation** —
  - `DxfBackend.create_block`: refuse with `BackendError` if any primitive carries `paper` (sheet furniture never belongs in a block); `bl = self.doc.blocks.new(name)`; for each primitive translate its `points` by `-base_point` and apply via the existing destination swap `self._apply_on(bl, translated)` (a `BlockLayout` speaks the same `add_line`/`add_lwpolyline`/`add_text` API the `_draw_*` methods use). Return `{"block": name, "entities": len(primitives)}`.
  - `DxfBackend.insert(block, at, *, source=None, rotation=0.0, scale=1.0, scale_x=None, scale_y=None, properties=None, layer="0")`: if `block not in self.doc.blocks` → `BackendError("the dxf backend cannot import block definitions; capture the element first (element_capture) or place it on the plugin backend")` — honest refusal, never a silent twin. Otherwise `self._record("insert", [at], block=block, rotation=rotation, scale_x=scale_x or scale, scale_y=scale_y or scale, layer=layer)`.
  - `_apply_primitive_here` gains an `"insert"` branch: `ent = self.msp.add_blockref(prim["block"], pts[0])`; set `ent.dxf.rotation`, `ent.dxf.layer`, `ent.dxf.xscale`, `ent.dxf.yscale`; return it. (`insert` primitives carry no `paper`; the sheet path never sees one.)
  - Erase/undo work unchanged — the blockref is an ordinary entity with a handle; the definition survives undo deliberately.

- [ ] **Step 4: catalogue runtime registration** — `Catalog.register(payload: dict) -> Element` in `catalog.py`: parse strictly (reuse the loader's per-entry parsing; a broken payload raises `ValueError` with the reason — direct caller action, so no tolerate-and-count here), refuse a duplicate `key`, add to `self.elements`, return the `Element`. The existing stamp-on-save (`recording.py`'s `note_meta(catalog=loaded.identity(...))`) then carries the new entry inside the drawing — verify with a save/reopen test rather than new code.

- [ ] **Step 5: the adapter** — `catalog_ops.CATALOG_OPS["insert_captured"]`: uniform signature `adapter(be, at, *, width=None, rotation=0.0, layer="", **params)`; body: `be.insert(params["block"], at, rotation=rotation, layer=layer or "0")`. `params["block"]` comes from the entry's element-level `op_params`. Update `tests/test_catalog_ops.py`'s introspection pin only if it hard-codes the adapter set.

- [ ] **Step 6: `capture_ops.capture`** —
  - Primitives: add `RecordingBackend.step_primitives(self, indices: list[int]) -> list[dict]` returning the recorded primitives of the named applied steps (KeyError → re-raise as `ValueError` naming the index; an undone step → `ValueError`). Plugin step rows already carry `primitives` — the accessor normalises both shapes so capture works on any recording backend.
  - `capture()`: refuse an empty union, a `steps` list containing a `paper`-marked primitive, or a `key` already in the catalogue (`ValueError`s). Default `base_point` = min corner of `geom.bbox` over all gathered points. `name = f"ACADMCP_{key.upper()}"`; call `be.create_block(name, prims, base_point)` — call it **directly**, not through recorded draw methods: the definition must not be journalled. Then `catalog.current().register({...})` with an **op-only** entry (no `block`/`source` keys — the definition lives in the drawing, not a library): `{"key": key, "label": label or key, "category": category, "layer": layer, "tag_prefix": tag_prefix, "op": "insert_captured", "op_params": {"block": name}}` — mirror the `column` entry's shape in `data/elements.json` for whatever fields it marks required.

- [ ] **Step 7: tests** — `tests/test_capture.py` on the dxf backend (follow `tests/test_backends.py` fixture patterns): draw a wall run + door via the ops in a couple of steps; capture those steps as `key="rollup_door"`; assert the block definition exists in the saved document, `place_element(type="rollup_door", at=[...])` twice lands two blockrefs on the entry's layer; save → `drawing_open` → the embedded catalogue resolves the key (the drawing travels with what it drew); undo of the capture step leaves the definition standing (document state); capture of a `paper` primitive or a duplicate key raises ValueError; capture on a backend without `supports_block_create` raises BackendError naming the capability.

- [ ] **Step 8: GREEN + ruff** — `.venv/Scripts/python.exe -m pytest tests/test_capture.py -q` green; ruff check + format the touched files.

---

## Integration (orchestrator, after A+B land)

- [ ] `server.py`: three thin tools. `drawing_critique(focus: str = "")` — `_READONLY`; rows from `be.step_rows()`, `tag_prefixes` from `catalog.current()`; returns `critique.run(...)`. `drawing_finalize()` — `_READONLY` hard gate: compose the same calls `drawing_verify`/`drawing_summary`/`drawing_critique` make (call the underlying functions, not the tools); any error-severity issue or a verdict other than `matches` → `BackendError` listing the findings; else `{"ok": True, ...}` with verdict, room count and issue counts. `element_capture(...)` — `_MUTATING`, wraps `capture_ops.capture` in `_auto_step` (the step records intent; the definition stays document state).
- [ ] `terms.py`: `drawing_critique` cad `("CHECKSTANDARDS",)` phrases `("critique", "check standards", "图纸核查", "构件检查", "查错")`; `drawing_finalize` cad `()` phrases `("finalize", "收官", "验收", "完成检查")`; `element_capture` cad `("BLOCK", "WBLOCK")` phrases `("make block", "capture element", "收编构件", "做成块", "创建块")`.
- [ ] `prompts.py`: floor_plan's VERIFY gains `drawing_critique` then `drawing_finalize` as the closing gate; drawing_review gains `drawing_critique` as check 2.5. Re-run the prompt anti-drift tests.
- [ ] `_INSTRUCTIONS`: one line — `drawing_critique()` checks components against the journal; `drawing_finalize()` is the done-gate that refuses while errors exist.
- [ ] Doc numbers: AGENTS.md `(49 tools`→52, `# 431 tests`→new count, `terms.py` "At 49 tools", `server.py` docstring "vocabulary of the 49 tools", README "49-tool" — then `scripts/check_doc_numbers.py` must say ok.
- [ ] AGENTS.md layout: `critique.py`, `capture_ops.py`, `test_critique.py`, `test_capture.py` lines.
- [ ] Full gates: `.venv/Scripts/python.exe -m pytest -q`, `check_doc_numbers.py`, `uv run --no-sync ruff check .`, sandbox wire script calling `element_capture` + `place_element` + `drawing_critique` + `drawing_finalize` end-to-end.

## Self-review notes

- A and B share no files; server/terms/prompts conflicts are eliminated by the orchestrator owning them.
- Every interface the orchestrator consumes is pinned in the workstream headers.
- Deliberately cut from this wave (recorded so nobody re-adds silently): the 0-100 score (YAGNI until the repair loop exists), repair handlers (next wave, needs settled Issue codes), `create_block` on plugin/com (plugin needs C#; com needs Blocks.Add+CopyObjects — both later), project-file export of captured entries.
