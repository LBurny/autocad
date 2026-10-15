# Standards, Templates and an Accumulating Library — Implementation Plan

> **For agentic workers:** Implement this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking. Phases are independently shippable; each ends green on the full gate.

**Goal:** Give the package the two things the 2026-10-15 ecosystem survey found missing everywhere — standards and templates as *checkable data*, and a component library that *accumulates across drawings* — without disturbing the drawing engine.

**Architecture:** Two pillars, five phases. Pillar one: a pure `standards.py` data module (GB/T tables with sources, refused-by-name lookups) → a read-only conformance checker → committed template artefacts built by the server's own backend with a fingerprint reproducibility gate. Pillar two: provenance + fingerprint on catalogue entries → an explicit, file-visible publish step that writes the project catalogue → a read-only suggest tool that mines the journal for repetition. Both pillars reuse the existing rules: pure modules import no CAD, every tool is one journal step, loading never raises, refusal names the capability.

**Tech Stack:** Python 3.14, ezdxf, mcp 2.x SDK, pytest, ruff; one small C# touch in the plugin (`DrawingApi.EnsureLayer`).

## Global Constraints

- `uv run pytest -q` green (488 tests today, growing per phase); `uv run ruff check . --output-format concise` clean; `uv run python scripts/check_doc_numbers.py` green — adding tools changes the counts quoted in README.md, AGENTS.md and `evaluations/`, so the docs move in the same task as the tool.
- Millimetres everywhere; degrees counter-clockwise from +X; journal payload version **stays 1** (every change below is additive through tolerant readers).
- Every new tool gets: one of the five annotation sets in `server.py`, a `terms.py` record (`tests/test_terms.py` gates it), and a mention in `prompts.py` only where a workflow genuinely changes.
- A backend protocol change lands on all three drivers (`base.py`, `dxf.py`, `com.py`, `plugin.py`) in the same task.
- **GB/T numbers are transcribed, never recalled.** Every table row carries `source` (standard + table number). A row nobody has verified ships **empty and its lookup refuses by name** — U-C4N's discipline, adopted verbatim. No interpolation, ever.
- No test requires AutoCAD. The plugin C# change is verified by `dotnet test` plus `./scripts/selftest.sh --engine` when AutoCAD is free, never by the pytest suite.
- Byte-identical reproducibility does not work for DXF (timestamps, GUIDs, `BLOCK_RECORD` ordering — the eval fixture lesson). The template gate compares a **geometry fingerprint**, not bytes.
- Comments explain constraints the code cannot show; match the existing density. README stays lean — measured gotchas go to AGENTS.md.

## Decisions (recommended defaults; flagged where the office may disagree)

- **D1 — Standards are data, loaded like the catalogue.** New `data/standards.json` (package default) merged under project files named in `acadmcp.json`'s new `standards` list — the same precedence `catalog.py` already runs. Scope is the repo's domain: GB/T 50001 (layers, line-width groups, text-height series, scales), GB/T 14689 (sheet), GB/T 10609.1 (title block). Mechanical fastener tables are **out of scope** — that is U-C4N's home turf, not a factory plan's.
- **D2 — Lineweight joins the layer spec.** GB/T 50001's `b / 0.7b / 0.5b / 0.35b` groups are what separates a standard from a layer list. `Backend.ensure_layer` grows an optional `lineweight` (mm) on all three drivers; `layers.LAYERS` itself is untouched (its tuple shape is load-bearing in `settings.py` and half the suite).
- **D3 — The checker reports, it does not raise.** `drawing_check_standard` mirrors `drawing_critique`'s shape (issues with severity, counted, never fatal). It stays advisory: `drawing_finalize` does **not** gain a new dependency this wave.
- **D4 — Templates are committed artefacts built by the server's own tools.** `templates/*.dxf` headless; `.dwt` twins are a script option that needs live AutoCAD, skipped with a printed note otherwise (U-C4N's split). A template names the standard it encodes, and the build fails if the artefact fails the Phase-B checker.
- **D5 — Publish is a separate, explicit, visible action.** `element_capture` stays drawing-local. `element_publish(key)` writes the project catalogue file — creating `acadmcp.json` + `elements.json` in the working directory when neither exists, and saying so in the reply. Never a capture side-effect; ADD-only (mem0's lesson: nothing overwrites).
- **D6 — Provenance rides `describe()` but not `digest()`.** `origin` (source drawing, journal steps, captured-at, fingerprint) is embedded with the entry so any machine opening the file can trace it — but placement semantics don't include provenance, so `Catalog.digest()` excludes it, or every pre-upgrade drawing reports a spurious `catalog_warning` on reopen.
- **D7 — Suggest is read-only and journal-scoped.** It mines the *current* drawing's step rows. Cross-drawing mining needs a project-level usage log — later wave, noted, not built.

## File Structure

| File | Responsibility |
|---|---|
| `src/acadmcp/data/standards.json` (new) | GB/T tables: layer set with lineweights, line-width groups, text-height series, title-block spec. Every row sourced; unverified rows empty. |
| `src/acadmcp/standards.py` (new) | Pure loader + registry + refused-by-name lookup + `check(standard, snapshot)`. Imports `layers`/`sheet` only. |
| `src/acadmcp/settings.py` | Read the `standards` file list (mirrors `catalogs`). |
| `src/acadmcp/backends/{base,recording,dxf,com,plugin}.py` | `ensure_layer(..., lineweight=None)`. |
| `plugin/src/AcadMcp.Plugin/Rpc/DrawingApi.cs` | Accept the lineweight field; set `LayerTableRecord.LineWeight`. |
| `src/acadmcp/templates.py` (new) | `TemplateSpec` registry, `resolve_template` (refuses by name), `template_rows`. |
| `src/acadmcp/data/templates.json` (new) | The two shipped templates: `gb_a3_arch`, `gb_a4_arch`. |
| `templates/` (new dir) | Committed `.dxf` artefacts, built not hand-drawn. |
| `scripts/build_templates.py` (new) | Builds artefacts via the dxf backend; `--check` fingerprint gate; `--dwt` optional live-AutoCAD twin. |
| `src/acadmcp/catalog.py` | `Element.origin`, `describe()`/`_read_element` round-trip, `digest()` excludes `origin`, `publish_to()` writer. |
| `src/acadmcp/capture_ops.py` | Provenance + geometry fingerprint at capture time; duplicate-fingerprint refusal; fragment export/import. |
| `src/acadmcp/catalog_ops.py` | `insert_captured` imports a published fragment when the drawing lacks the definition. |
| `src/acadmcp/suggest.py` (new) | Pure repetition mining over `step_rows()`. |
| `src/acadmcp/server.py` | `drawing_check_standard`, `template_list`, `element_publish`, `catalog_suggest`; `drawing_new(template=)`. |
| `src/acadmcp/terms.py` | Vocabulary for the four new tools. |
| `tests/test_standards.py`, `test_templates.py`, `test_publish.py`, `test_suggest.py` (new) | Per-phase suites. |

Execution order with subagent waves (the user's preference): **Wave 1 = Phase A + Phase D in parallel** (disjoint file sets). **Wave 2 = Phase B** (needs A). **Wave 3 = Phase C + Phase E in parallel** (C needs A+B; E touches only `suggest.py` + `server.py` — land it after C's `server.py` edit or rebase).

---

## Phase A — Standards as data

### Task A1: `standards.json` + the loader

**Files:**
- Create: `src/acadmcp/data/standards.json`
- Create: `src/acadmcp/standards.py`
- Test: `tests/test_standards.py`

**Interfaces:**
- Produces: `Standards.load(paths) -> Standards`, `Standards.current()`, `standards.get(name) -> Standard | None`, `Standard.lookup(table, key) -> row` (raises `ValueError` naming the key and the table's coverage), `Standard.layers -> dict[str, LayerSpec]` where `LayerSpec = tuple[int, str, float | None]` (colour, linetype, lineweight-mm or None), `Standard.text_heights -> frozenset[float]`, `Standard.skipped: list[str]`. Consumed by A3, B, C.

- [x] **Step 1: failing test** — `tests/test_standards.py`:

```python
def test_lookup_refuses_outside_the_table_by_name():
    std = standards.current().get("gbt50001")
    with pytest.raises(ValueError, match="no row for '0.33'.*coverage"):
        std.lookup("text_heights", "0.33")

def test_empty_table_refuses_everything():
    std = standards.current().get("gbt50001")
    with pytest.raises(ValueError, match="not transcribed"):
        std.lookup("line_width_groups", "1:20")  # row shipped empty pending transcription
```

- [x] **Step 2: run it** — `.venv/Scripts/python.exe -m pytest tests/test_standards.py -q` → FAIL (`standards` missing).

- [x] **Step 3: implement** `standards.py` (catalog-shaped: `load`/`current`/`reset`, tolerant `add_file` counting `skipped`, refused-by-name lookup):

```python
class Standard:
    def __init__(self, name, tables, skipped): ...
    def lookup(self, table: str, key: str) -> dict:
        rows = self.tables.get(table)
        if rows is None:
            raise ValueError(f"standard '{self.name}' has no table '{table}'")
        if not rows:
            raise ValueError(f"{self.name}/{table}: not transcribed yet — refused by name rather than interpolated")
        row = rows.get(key)
        if row is None:
            cov = rows.get("_coverage", {})
            raise ValueError(f"{self.name}/{table} has no row for '{key}'; coverage is {cov}")
        return row
```

`data/standards.json`: one `"gbt50001"` standard. Populate only what the tree already measures: layers (from `layers.LAYERS` + lineweights **transcribed from GB/T 50001's line-width groups — each row `"source": "GB/T 50001 表…"`; any width not yet transcribed ships as an empty table), text heights `2.5/3.5/5/7/10/14/20` with source, scales as `"see sheet.SCALES"` (a pointer, not a copy — never two sources for one list), title block `{"width": 180, "height": 56, "rows": 4, "source": "GB/T 10609.1"}` matching `sheet.py`'s constants. A test asserts the JSON numbers equal `sheet.TITLE_BLOCK_WIDTH` etc., so the two cannot drift.

- [x] **Step 4: pass** — same command → PASS.

### Task A2: Lineweight through the layer stack

**Files:**
- Modify: `src/acadmcp/backends/base.py:55`, `dxf.py:359`, `com.py:376`, `plugin.py:96`, `recording.py` (primitive passthrough)
- Modify: `plugin/src/AcadMcp.Plugin/Rpc/DrawingApi.cs` (+ its protocol DTO)
- Test: `tests/test_standards.py` (dxf layer table read-back), plugin covered by `dotnet test` + selftest

**Interfaces:**
- Produces: `Backend.ensure_layer(name, color=None, linetype=None, lineweight: float | None = None)`. dxf writes `lineweight=int(mm*100)` on the layer table entry; com sets `Lineweight`; plugin forwards the field over RPC. Consumed by A3.

- [x] **Step 1: failing test** — dxf: after `ensure_layer("A-WALL", 2, "CONTINUOUS", lineweight=0.7)`, `doc.layers.get("A-WALL").dxf.lineweight == 70`.
- [x] **Step 2: fail** — pytest: unexpected kwarg.
- [x] **Step 3: implement** — protocol + dxf + com + recording passthrough. Plugin: add `"lineweight"` to the RPC payload and set `ltr.LineWeight = (LineWeight)(int)(mm * 100)` in `DrawingApi.EnsureLayer`; older plugins ignore an unknown field (JSON payload is tolerant), so no version dance.
- [x] **Step 4: pass** — pytest green; `cd plugin && dotnet test` green; `./scripts/selftest.sh --engine` when AutoCAD is free.

### Task A3: The standard drives layer setup, and settings name the project standard

**Files:**
- Modify: `src/acadmcp/ops.py:38` (`setup_layers`)
- Modify: `src/acadmcp/settings.py` (`standards` list, mirrors `catalogs`)
- Modify: `src/acadmcp/server.py` (`drawing_status` reports the standard in force)
- Test: `tests/test_standards.py`

**Interfaces:**
- Consumes: A1's `Standards.current()`, A2's `lineweight`.
- Produces: `Settings.standards: list[str]`; `setup_layers` applies the in-force standard's layer table (colour, linetype, lineweight) overlaid by `settings.layers`; `drawing_status()["standard"]` = name + digest + skipped.

- [x] **Step 1: failing test** — `setup_layers` on a recording backend with a standard whose `A-WALL` row carries `lineweight 0.7` records `ensure_layer(..., lineweight=0.7)`; a project `standards` file replaces a row, package rows survive elsewhere (the catalogue's merge rule, restated).
- [x] **Step 2-4:** red → green → `drawing_status` gains `"standard"`; run full suite (layer-touching tests may need the new kwarg in fakes).

---

## Phase B — The conformance checker (needs A)

### Task B1: `check()` — the pure half

**Files:**
- Modify: `src/acadmcp/standards.py`
- Test: `tests/test_standards.py`

**Interfaces:**
- Produces: `check(standard: Standard, snapshot: dict) -> list[Issue]` where `Issue = {"rule", "severity", "detail", "entities"}`. `snapshot` = `{"units": str, "layers_in_use": list[str], "texts": [{"height", "layer", "content"}], "sheet": dict}`. Pure: no CAD imports, same rule as `critique.py`.

Checks (this wave): units are mm; every layer in use exists in the standard's table (unknown = warning — project layers are legitimate); every text height is in the standard's series (off-series = error — that is the check AutoCAD's CHECKSTANDARDS actually runs); a sheet exists and its title block states 比例 (reuse the journal's `drawing_sheet` params, the way `critique.py` already reads steps). Layer-table property conformance (colour/linetype/lineweight of the *definition*) needs a backend `layer_table()` read the plugin RPC does not have yet — **deferred, named in the module docstring**, not faked from entity rows.

- [x] **Steps 1-4:** failing tests for each rule (unknown layer warns, `height=260` errors against the series, missing 比例 errors) → implement → green.

### Task B2: `drawing_check_standard` tool

**Files:**
- Modify: `src/acadmcp/server.py` (tool, `_READONLY` annotations), `src/acadmcp/terms.py`, `README.md` + `AGENTS.md` + `evaluations/` (tool count bumps by one here and again in each later phase — the running total lands at 56, see the final gate; update whatever the gate flags in the task that adds the tool)
- Test: `tests/test_standards.py` (tool-level via a recording backend), `tests/test_tool_surface.py` picks it up automatically

- [x] **Step 1: failing test** — tool on a drawing with a 260 mm text returns `issues` containing the text-height rule, `ok: True`, verdict counts.
- [x] **Step 2-4:** implement thin wrapper (gather snapshot from `be.entities()`, `be.info()`, journal sheet params; call `check`); terms entry `Term(cad=("CHECKSTANDARDS", "STANDARDS"), phrases=("标准检查", "图层规范", "制图标准"))`; run suite + `check_doc_numbers.py`.

---

## Phase C — Template artefacts (needs A + B)

### Task C1: The template registry

**Files:**
- Create: `src/acadmcp/templates.py`, `src/acadmcp/data/templates.json`
- Test: `tests/test_templates.py`

**Interfaces:**
- Produces: `TemplateSpec(key, label, standard, paper, orientation, scale, title_fields: dict, artefact: str)`, `resolve_template(key) -> TemplateSpec` (refuses by name listing what exists), `template_rows() -> list[dict]`. Consumed by C3, C4.

Two shipped templates: `gb_a3_arch` (gbt50001, A3 landscape, 1:100) and `gb_a4_arch` (gbt50001, A4 portrait, 1:50). Both named in Chinese for the office: label `"A3 建筑平面（GB/T）"`.

- [x] Steps: failing test (unknown key raises listing both names) → implement → green.

### Task C2: `scripts/build_templates.py` + the fingerprint gate

**Files:**
- Create: `scripts/build_templates.py`
- Create: `templates/gb_a3_arch.dxf`, `templates/gb_a4_arch.dxf` (built artefacts, committed)
- Test: `tests/test_templates.py` runs `--check`

- [x] **Step 1: failing test** — `test_templates.py::test_artefacts_rebuild_identically` shells `python scripts/build_templates.py --check` and expects exit 0.
- [x] **Step 2: fail** — script absent.
- [x] **Step 3: implement.** For each spec: `make_backend("dxf", create=True)` → `ops.setup_layers` under the spec's standard → `drawing_sheet`-equivalent (`be.set_page_setup` + `ops.draw_sheet` with `title_fields`) → `be.save(templates/<key>.dxf)`. `--check` rebuilds into a temp dir and compares a **geometry fingerprint** — the eval-fixture lesson: `(dxftype, layer, text, height, points)` per entity as a `collections.Counter`, compared as sets; never bytes, never `git status`. The build also runs Phase-B `check()` against the artefact and fails on error-severity issues (the template↔standard binding, D4).
- [x] **Step 4: pass** — build, check green, suite green.

### Task C3: `drawing_new(template=...)` + `template_list`

**Files:**
- Modify: `src/acadmcp/server.py` (`drawing_new` gains `template: str = ""`; new `_READONLY` tool `template_list`), `terms.py`, prompts (`floor_plan` step 1 mentions `template="gb_a3_arch"`)
- Test: `tests/test_templates.py`

**Interfaces:**
- Consumes: C1's `resolve_template`, A3's standard-driven `setup_layers`.
- Produces: `drawing_new(backend="dxf", template="gb_a3_arch")` — after `_stamp_catalog`, applies the spec: standard layers, page setup, sheet frame with `title_fields`. `template=""` is today's behaviour, untouched. `template_list()` returns `template_rows()`.

- [x] Steps: failing tool-level test (new drawing from `gb_a3_arch` carries A-SHEET lines + the standard's layers + `sheet` in `info()`) → implement → green → terms entries (`template_list`: `cad=("NEW",)`, phrases `("模板", "样板", "图框模板")`) → doc numbers.

### Task C4: `.dwt` twins (optional, live AutoCAD only)

**Files:**
- Modify: `scripts/build_templates.py` (`--dwt` flag)

- [x] `--dwt` opens each built `.dxf` in a scratch AutoCAD doc via win32com (`Documents.Open`), `SaveAs(path, <template format const>)` — verify the constant against `tools/AcadMcp.ApiProbe` output before hardcoding (AGENTS.md measured 25 = ac2018_dxf; the template constant is its neighbour, unverified) — then closes the doc. Without AutoCAD the flag prints `skipped: no AutoCAD` and exits 0. Not gated by pytest.

---

## Phase D — The library grows (parallel with A/B/C)

### Task D1: Provenance on `Element`, and its fingerprint

**Files:**
- Modify: `src/acadmcp/catalog.py` (`Element.origin`, `describe`, `_read_element`, `digest`)
- Modify: `src/acadmcp/capture_ops.py` (`fingerprint_primitives`, and the origin stamp)
- Test: `tests/test_catalog.py`, `tests/test_capture.py`

**Interfaces:**
- Produces: `Element.origin: dict` = `{"source_drawing": str, "steps": list[int], "captured_at": str, "fingerprint": str}`, all optional, `{}` for file-authored entries. `describe()` includes it; `digest()` hashes `describe()` **minus** `origin` (D6). Also produces `fingerprint_primitives(prims: list[dict]) -> str` in `capture_ops` — sha256 over a canonicalisation: per primitive `(kind, layer, sorted-rounded-points (0.1 mm), text, height)`, order-free (set semantics, since capture order across steps is stable but step splitting is a caller choice). Both consumed by D2 and D3.

- [x] **Step 1: failing tests** — the silent-drop guard, which is the whole point:

```python
def test_origin_survives_a_describe_adopt_round_trip():
    cat = Catalog()
    cat.register({... "op": "insert_captured", "params": {"block": "ACADMCP_X"},
                  "origin": {"source_drawing": "a.dxf", "steps": [3, 4],
                             "captured_at": "2026-10-15T01:00:00", "fingerprint": "sha256:…"}})
    adopted = Catalog.adopt(cat.identity())
    assert adopted.get("x").origin["steps"] == [3, 4]

def test_digest_ignores_origin():
    ...  # same entries, two origins, one digest
```

- [x] **Step 2: fail** (origin dropped by `_read_element`).
- [x] **Step 3: implement** — `Element` field, both readers, digest exclusion with a comment ("provenance is not placement semantics; a stamped entry and its file twin must hash alike"), `fingerprint_primitives`, and `capture_ops.capture` stamping `source_drawing` from `be.info()`, `steps`, UTC ISO time and the fingerprint.
- [x] **Step 4: pass** — full suite (identity/digest tests exist; they must stay green unchanged).

### Task D2: Duplicate-geometry refusal

**Files:**
- Modify: `src/acadmcp/capture_ops.py` (the refusal; the fingerprint itself landed in D1)
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: D1's `fingerprint_primitives` and `origin.fingerprint`.

- [x] **Failing test:** capturing geometry whose fingerprint already sits on another entry raises `ValueError` naming the existing key ("the library already holds this geometry as 'door_900' — capture refused; rename only if it is genuinely a different element").
- [x] Implement + green. Register path keeps its duplicate-**key** refusal; this adds the duplicate-**geometry** one.

### Task D3: `element_publish` — entry **and geometry** into the project library

An entry without its geometry is a name that resolves to nothing: the captured block
definition lives in the drawing it was captured in, and a fresh drawing cannot place it.
Publish therefore writes two things — the catalogue entry and a geometry fragment — and
`insert_captured` learns to import the fragment when the definition is absent. (Found at
planning time by reading `insert_captured` + the dxf `insert` refusal; without this the
end-to-end sandbox in D4 fails by construction.)

**Files:**
- Modify: `src/acadmcp/catalog.py` (`Catalog.publish_to(path, element, fragment) -> dict`; track each entry's source file so `geometry_file` resolves)
- Modify: `src/acadmcp/capture_ops.py` (`export_fragment(path, name, primitives, base_point, provenance)` and `import_fragment(path) -> (name, primitives, base_point)`)
- Modify: `src/acadmcp/catalog_ops.py` (`insert_captured` imports the fragment when the dxf drawing lacks the definition)
- Modify: `src/acadmcp/backends/recording.py` (`find_block_primitives(name) -> dict | None` — scan the journal's `block` primitives; the capture step already records the full definition there)
- Modify: `src/acadmcp/server.py` (integration wave — `_MUTATING` tool), `terms.py`
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: D1's origin, `settings.current()`.
- Produces:
  - `element_publish(key, path="") -> dict`. Target resolution: explicit `path`, else the project's first catalogue file in settings, else **create** `./elements.json` + `./acadmcp.json` (`{"catalogs": ["elements.json"]}`) and report both as created (D5).
  - The **geometry fragment** goes to `<catalogue_dir>/library/<key>.dxf`: a headless `DxfBackend` doc holding one block definition (`create_block`), whose journal meta carries `exported_element = {key, block, primitives, base_point, fingerprint}` — primitives' canonical form is the journal vocabulary, so no entity→primitive reverse mapping is ever written. `origin.geometry_file` records the path **relative to the catalogue file**.
  - Publish refuses, naming what's missing: key unknown in the in-force catalogue; key already in the target file (ADD-only); the `block` record absent from the current drawing's journal ("the definition lives in the drawing that captured it — open that drawing and publish from there"). Writes are atomic (tmp + `os.replace`), `ensure_ascii=False`, `indent=2`, entry appended never re-sorted, `_comment` preserved. After writing: `settings.reset()`. Reply: `{ok, file, fragment, created: [...], key, digest}`.
  - `insert_captured` (dxf backend only): when `block not in doc.blocks` and the entry's `origin.geometry_file` resolves (relative to the catalogue file the entry came from; an adopted, drawing-embedded entry has no file context and refuses by name) → `import_fragment` → `create_block` → insert. com/plugin name the capability, as usual.

- [x] **Failing tests:** publish creates both files + the fragment in an empty tmp cwd; publish into an existing file appends and preserves `_comment`; double-publish refuses by name; the published file loads back through `Catalog.load` (origin included — D1's round-trip across a file); **the loop test**: publish from drawing A, then a fresh `DxfBackend` places `place_element(key)` and the geometry matches A's block (fingerprint equal); publish from a drawing that never captured the key refuses naming the missing definition.
- [x] Implement + green + terms entry (`cad=("WBLOCK",)`, phrases `("发布元件", "入库", "存进组件库")`) at integration.

### Task D4: Sandbox proof

**Files:**
- Create: `tests/sandbox_library_check.py` (the `sandbox_driver.py` pattern)

- [x] A sandbox run that draws, verifies, captures, publishes, starts a **fresh server**, and places the published element in a new drawing — the accumulation loop proven end to end over the wire, per the repo's sandbox rule. Prints the git HEAD line like its siblings.

---

## Phase E — Suggest (the observe half)

### Task E1: `suggest.py`

**Files:**
- Create: `src/acadmcp/suggest.py`
- Test: `tests/test_suggest.py`

**Interfaces:**
- Produces: `suggestions(step_rows: list[dict], min_count: int = 3) -> list[dict]`. Pure, no CAD imports. Groups by `(op, canonical-params)` — params canonicalised by sorting keys and rounding floats to 0.1 mm; `steps`, `label` excluded. Each group ≥ `min_count` yields `{"op", "count", "params", "steps": [...], "hint": "these N steps drew the same thing — element_capture(steps=[...], key=…) would make it a library element"}`.

- [x] Failing tests (repeated `draw_door` group surfaces; two doors of different widths do **not** group; undone steps excluded) → implement → green.

### Task E2: `catalog_suggest` tool

**Files:**
- Modify: `src/acadmcp/server.py` (`_READONLY`), `terms.py`
- Test: `tests/test_suggest.py` tool-level

- [x] Tool wrapper over `be.step_rows()`; terms entry (`cad=()`, phrases `("沉淀建议", "重复", "建议入库")`); suite + doc numbers green.

---

## Final gate (every phase, and at the end)

- [x] `.venv/Scripts/python.exe -m pytest -q` — green, count updated in AGENTS.md/README where quoted
- [x] `uv run ruff check . --output-format concise` and `uv run ruff format .`
- [x] `uv run python scripts/check_doc_numbers.py` — tool count 52 → 56 across README/AGENTS/`evaluations/`
- [ ] `./scripts/selftest.sh --engine` once after Phase A2 (the plugin lineweight) when AutoCAD is free
- [ ] Update `MEMORY.md` pointers (the two-pillars memory lists these gaps; close them out)

## Out of scope this upgrade (named, so nobody "discovers" them missing)

- Mechanical fastener/material tables (U-C4N's ground; our domain is the factory plan).
- `.dws` export for AutoCAD's own CHECKSTANDARDS, and the backend `layer_table()` read (needs a plugin RPC method; Phase B's docstring names the deferral).
- Cross-drawing / cross-machine accumulation, vector search, a global user-level library (`~/.acadmcp`). The project file is the unit of sharing — it diffs in git, which is the point.
- Sheet sets (.dst), and capturing a *sheet* as a template (templates are authored in `templates.json`, not mined).
- Any automatic promotion: nothing enters the library without an explicit `element_capture` + `element_publish`. The research was unambiguous — every working example gates on a human.
