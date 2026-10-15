# MCP Prompt Templates Implementation Plan

> **For agentic workers:** Implement this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three MCP prompt templates (`floor_plan`, `headless_to_live`, `drawing_review`) that teach a connected client the server's high-level workflow, its conventions, and its verification gates — the lesson taken from U-C4N/Autocad-MCP's `@mcp.prompt` set, built on our high-level ops instead of their primitive-level ones.

**Architecture:** A new pure-text module `src/acadmcp/prompts.py` holds the prompt builders and a `register(mcp)` function; `server.py` gains one import word and one call, staying thin per AGENTS.md. Tests pin registration, rendering, and — the anti-drift gate — that every `snake_case(...)` token named in a prompt is a registered tool (the same bug shape as the summary.py handler keyed on an op name no tool produces).

**Tech Stack:** mcp SDK 2.3.0 (`MCPServer.prompt(name, description)` decorator; enumerated via `mcp._prompt_manager.list_prompts()`, rendered via `await mcp.get_prompt(name, args)`). No `tags` parameter exists in this SDK version.

> **Erratum (added after completion):** this plan originally said "there is no middleware API" in this SDK. Wrong — mcp 2.3.0 `MCPServer.__init__` takes `middleware: Sequence[ServerMiddleware[Any]]` (a Protocol in `mcp.server.context`: `async __call__(ctx, call_next)`, wrapping every inbound request pre-validation) plus an `instructions` field. What it lacks is FastMCP 3.x's `add_middleware()` method and class-based `on_call_tool` hooks. Audit/timing middleware is therefore achievable without migrating.

## Global Constraints

- Run tests with `.venv/Scripts/python.exe -m pytest` — never `uv run pytest` (a running client server holds the editable install lock).
- `prompts.py` imports nothing CAD-related and nothing from the package — pure text, the `terms.py` rule.
- All prompt text is English, matching every docstring in the repo; room-name examples may be Chinese where they demonstrate the CJK capability.
- Every `word(` token in a prompt body with `word` matching `[a-z][a-z0-9_]{2,}` MUST be a registered tool name — the test suite enforces it. Do not name helper functions (`fit_text_height`, `inner_boundary`) with parentheses in prompt prose.
- `AGENTS.md` and `src/acadmcp/server.py` already carry uncommitted changes; edits must be additive, never a revert.
- No new quotable numbers in docs (prompt counts stay out of prose), so `scripts/check_doc_numbers.py` needs no new derivations. The one number that moves is the collected test count in AGENTS.md — fixed in the integration task, not by the parallel agents.

---

### Task 1: The prompts module, its tests, and the server wiring

**Files:**
- Create: `src/acadmcp/prompts.py`
- Create: `tests/test_prompts.py`
- Modify: `src/acadmcp/server.py` (two additive lines: one import word, one `register` call)

**Interfaces:**
- Produces: `acadmcp.prompts.register(mcp) -> None`; prompt builders `floor_plan(building_name: str = "Building A", scale: str = "1:100") -> str`, `headless_to_live() -> str`, `drawing_review() -> str`. Registered prompt names: `floor_plan`, `headless_to_live`, `drawing_review`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing test** — create `tests/test_prompts.py` exactly:

```python
"""The prompt templates: registered, renderable, and naming only real tools.

A prompt that names a tool the server does not have is the same bug shape as a
handler keyed on an op name no tool produces — so the tool names are read out of
the registry, never trusted.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402

PROMPTS = {p.name: p for p in server.mcp._prompt_manager.list_prompts()}
TOOLS = {t.name for t in server.mcp._tool_manager.list_tools()}

EXPECTED = {"floor_plan", "headless_to_live", "drawing_review"}


def _render(name: str, args: dict | None = None) -> str:
    result = asyncio.run(server.mcp.get_prompt(name, args or {}))
    return result.messages[0].content.text


def test_the_three_prompts_are_registered():
    assert set(PROMPTS) == EXPECTED


def test_every_prompt_carries_a_description():
    missing = [name for name, p in PROMPTS.items() if not (p.description or "").strip()]
    assert not missing


def test_every_prompt_renders_to_substantial_text():
    for name in PROMPTS:
        assert len(_render(name)) > 400, name


def test_prompts_name_only_registered_tools():
    snake_call = re.compile(r"\b([a-z][a-z0-9_]{2,})\(")
    for name in PROMPTS:
        for word in snake_call.findall(_render(name)):
            assert word in TOOLS, f"{name} names {word}(...), which is not a registered tool"


def test_floor_plan_names_the_core_ops():
    text = _render("floor_plan")
    for tool in [
        "draw_wall_with_openings",
        "draw_room",
        "place_element",
        "drawing_sheet",
        "drawing_verify",
        "drawing_summary",
    ]:
        assert tool in text


def test_floor_plan_records_the_conventions():
    text = _render("floor_plan")
    assert "closed=True" in text  # the envelope is one closed run, never four runs
    assert "centreline" in text.lower()  # room boundaries are wall centrelines


def test_floor_plan_arguments_have_defaults_and_render():
    assert "Tower B" in _render("floor_plan", {"building_name": "Tower B"})


def test_headless_to_live_names_replay_and_the_skip_rule():
    text = _render("headless_to_live")
    assert "drawing_replay" in text
    assert "skipped" in text  # unplaceable steps are reported, never silently dropped


def test_drawing_review_forbids_an_unchecked_all_clear():
    text = _render("drawing_review")
    assert "drawing_verify" in text
    assert "never" in text.lower()  # never claim "no issues" without running all three checks
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -q`
Expected: FAIL — collection error or assertion on `set(PROMPTS)` (no prompts registered yet).

- [ ] **Step 3: Write the implementation** — create `src/acadmcp/prompts.py` exactly:

```python
"""The MCP prompt templates: workflow, conventions and verification gates as text.

Pure text — this module imports nothing CAD-related and nothing from the package,
the same rule `terms.py` follows. A prompt that names a tool is checked against
the live tool registry in `tests/test_prompts.py`, so a renamed tool breaks the
suite rather than a client's session.
"""

from __future__ import annotations

from typing import Any


def floor_plan(building_name: str = "Building A", scale: str = "1:100") -> str:
    """Workflow for an architectural floor plan, from envelope to verified sheet."""
    return f"""You are drawing an architectural floor plan for '{building_name}' at {scale}
with this server's high-level ops. Millimetres everywhere; degrees counter-clockwise
from +X. One tool call is one journal step, so a mistake costs one drawing_undo —
never redraw what an undo can take back.

WORKFLOW:
1. drawing_new(backend="dxf") — draft headless even when AutoCAD is available;
   drawing_replay(backend="plugin") carries the finished plan into AutoCAD later.
2. Building envelope: ONE call of draw_wall_with_openings(points=<ring>,
   closed=True, thickness=200, openings=[...]). Never four separate wall runs —
   each run has square-cut ends and leaves a half-wall notch at every corner.
   An opening is {{"start": mm along the ring, "width": mm,
   "kind": "door"|"window"|"opening", "swing": "left"|"right", "tag": "M0921"}}.
3. Interior partitions: draw_wall(thickness=120), or draw_wall_with_openings
   again when the partition carries doors.
4. Rooms: draw_room(boundary=<wall CENTRELINE ring>, name="...") per room. The
   boundary is the centreline, not the inner face; the reported area is measured
   to the inner face, so a 4000x3000 ring with 200 mm walls reports 3.8x2.8 m.
   When the walls exist already, pass walls=False so only the label is drawn.
   Room names may be Chinese (生产车间, 办公室) — the HZ text style carries CJK
   and the label shrinks to fit the room.
5. Columns and fixtures: place_element(type="column", at=[x, y]); element_types
   lists what the catalogue offers, and the catalogue places on every backend.
6. Cores: draw_stair, draw_lift, draw_equipment(kind="shaft").
7. draw_axis_grid(...) for the structural grid, then add_dimension(points=[...])
   for the dimension chains.
8. drawing_sheet(paper="A3", scale="{scale}") — read plan_mm and fits in the
   reply; when fits is false, the note names the largest scale that holds the
   plan whole. A sheet narrower than the building is a design choice, not an
   error — but say so in your report.

VERIFY — all three, every time:
- drawing_screenshot() and LOOK at it; zoom into regions with
  drawing_screenshot(window=[min_x, min_y, max_x, max_y]) until the room labels
  are legible in the shot.
- drawing_verify() — the verdict must be "matches"; "unaccounted" means
  something drew outside a step, "missing" means entities were lost.
- drawing_summary() — rooms with areas, opening and element counts; read it
  back against the brief you were given.

RECOVERY: drawing_history() lists the steps; drawing_undo() takes back the last
call; drawing_rollback_to(index) erases everything after a named step.
"""


def headless_to_live() -> str:
    """Draft headless in DXF, review the PNGs, then replay into live AutoCAD."""
    return """This server draws the same plan into a headless DXF file or into a live
AutoCAD session, and a plan drafted headless is replayed into AutoCAD without
being redrawn. Prefer that route whenever the drawing is more than a few calls.

WORKFLOW:
1. drawing_new(backend="dxf", path="...") and draw the whole plan headless —
   every drawing tool works without AutoCAD.
2. drawing_screenshot() after each stage and look at it; the preview renders
   CJK text natively, so a Chinese title reads the same as it will in AutoCAD.
3. drawing_save() — the step journal travels inside the .dxf, so a later
   drawing_open(path) keeps every step undoable across sessions.
4. acad_connect(mode="plugin") to attach to the running AutoCAD. The plugin is
   installed once per machine with plugin_install; it puts each step and its
   journal write in one transaction, so one Ctrl+Z in AutoCAD undoes one step.
5. drawing_replay(backend="plugin") re-draws the applied journal step by step.
   Steps the target cannot apply — a library insert onto the dxf backend, going
   the other way — are skipped and REPORTED in the reply's skipped list, never
   silently dropped. Read that list.
6. drawing_verify() on the live side must say "matches", and
   drawing_screenshot() now shows the AutoCAD window itself.

RULES:
- Never redraw by hand what a replay can carry; redrawing doubles the journal.
- The headless draft is disposable; the replayed drawing is the deliverable, so
  the verification gate runs on the live side, not only on the draft.
"""


def drawing_review() -> str:
    """Review the current drawing: summary, journal reconciliation, screenshots."""
    return """Review the current drawing and report what is wrong with it. Never claim
"no issues" without running all three checks — a review that ran one says
nothing, and "I could not check" is reported as such, never treated as fine.

CHECKS:
1. drawing_summary() — rooms with areas (recomputed from the recorded boundary,
   the same geometry the room tool used), openings, elements, and annotation
   counts. Rows it cannot read are counted in unreadable, never skipped
   silently; a non-zero unreadable is itself a finding.
2. drawing_verify() — the journal reconciled against the drawing. "matches"
   means every applied step's entities are present; "unaccounted" means the
   drawing holds more than any step recorded (something drew outside a step);
   "missing" means fewer (a save dropped them, or an erase went too far).
3. drawing_screenshot() — then zoom into each region with
   drawing_screenshot(window=[min_x, min_y, max_x, max_y]) until the room labels
   are legible in the shot; a label too small to read at review scale is a
   finding. When a sheet was laid out, drawing_status reports the paper and
   scale, and the sheet call's own reply said whether the plan fits the
   viewport — a plan cut off by its viewport is a finding.

REPORT: one line per finding with its layer and location, then the counts from
drawing_summary(). Separate "the drawing is wrong" from "I could not check
this"; both belong in the report, and the second is never silently dropped.
"""


def register(mcp: Any) -> None:
    """Attach every prompt in this module to the server object."""
    mcp.prompt(
        name="floor_plan",
        description="Workflow for an architectural floor plan: envelope, rooms, "
        "elements, sheet, and the verification gates.",
    )(floor_plan)
    mcp.prompt(
        name="headless_to_live",
        description="Draft headless in DXF, review the PNGs, then replay into "
        "live AutoCAD without redrawing.",
    )(headless_to_live)
    mcp.prompt(
        name="drawing_review",
        description="Review the current drawing: summary, journal reconciliation, "
        "screenshots — and an honest report.",
    )(drawing_review)
```

- [ ] **Step 4: Wire it into the server** — in `src/acadmcp/server.py`:
  - Change the line `from . import catalog, geom, layers, ops, settings, sheet, summary, terms, verify` to include `prompts` (keep alphabetical order: `catalog, geom, layers, ops, prompts, settings, sheet, summary, terms, verify`).
  - Immediately after `mcp = _Server("autocad_mcp")` add one line: `prompts.register(mcp)`
  - No other change to that file.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -q`
Expected: 9 passed

- [ ] **Step 6: Lint the new files**

Run: `.venv/Scripts/python.exe -m ruff check src/acadmcp/prompts.py tests/test_prompts.py && .venv/Scripts/python.exe -m ruff format --check src/acadmcp/prompts.py tests/test_prompts.py` (if ruff is not importable from the venv, use `uv run --no-sync ruff ...`)
Expected: clean; if formatting differs, run `ruff format` on the two new files and re-run Step 5.

### Task 2 (parallel): Documentation

**Files:**
- Modify: `AGENTS.md` (layout section + commands section + tests list — additive only)
- Modify: `README.md` (one sentence)

**Interfaces:**
- Consumes: the prompt names fixed in Task 1 (`floor_plan`, `headless_to_live`, `drawing_review`) — no code dependency, can run in parallel.
- Produces: nothing later tasks rely on.

- [ ] **Step 1: AGENTS.md layout section** — add one line to the `src/acadmcp/` listing, after the `terms.py` entry:
  ```
    prompts.py            the MCP prompt templates (floor_plan, headless_to_live,
                          drawing_review). Pure text; no CAD imports.
  ```
  And in the same layout block's `tests/` line list, insert `test_prompts.py (the prompt registry against the tool registry),` after `test_terms.py (every tool has a vocabulary entry),`.

- [ ] **Step 2: AGENTS.md commands section** — add after the `test_terms.py` line:
  ```
  uv run pytest tests/test_prompts.py -q  # a prompt naming a tool that does not exist fails here
  ```
  Do NOT touch the `# 415 tests` figure — the integration task updates it to the new collected count.

- [ ] **Step 3: README.md** — add ONE sentence where the feature list lives (keep it lean, per repo convention): the server ships three MCP prompt templates — `floor_plan`, `headless_to_live`, `drawing_review` — that walk a client through the workflow and its verification gates. No other README change.

### Task 3 (integration, after Tasks 1+2 land): the gates

Run by the orchestrator, not a subagent.

- [ ] **Step 1: Full suite** — `.venv/Scripts/python.exe -m pytest -q`; expected: all pass, collected count = 415 + 9 = 424 (verify actual).
- [ ] **Step 2: Fix the derived doc number** — update AGENTS.md's `# 415 tests` to the collected count; run `.venv/Scripts/python.exe scripts/check_doc_numbers.py`; expected `ok:` line.
- [ ] **Step 3: Ruff the tree** — `uv run --no-sync ruff check . --output-format concise`; expected clean.
- [ ] **Step 4: Wire-level check in a fresh sandbox** — write `tests/sandbox_prompts_check.py`:

```python
"""Wire check: the prompts reach a real MCP client over stdio."""

async def draw(tools):
    prompts = await tools.session.list_prompts()
    names = sorted(p.name for p in prompts.prompts)
    print("sandbox prompts:", names)
    assert names == ["drawing_review", "floor_plan", "headless_to_live"], names
    got = await tools.session.get_prompt("floor_plan", {"building_name": "Sandbox Tower"})
    text = got.messages[0].content.text
    assert "Sandbox Tower" in text and "draw_wall_with_openings" in text
    print("sandbox: floor_plan renders over the wire,", len(text), "chars")
```

Run: `.venv/Scripts/python.exe tests/sandbox_driver.py tests/sandbox_prompts_check.py`
Expected: the HEAD/dirty-count banner, `sandbox prompts: [...]`, and `draw() finished clean`. (Sandbox scripts live in tests/ but are not collected by pytest — `sandbox_driver.py` and `live_rpc_check.py` set the precedent.)

- [ ] **Step 5: Report** — state what shipped, the test count before/after, and that the client needs a server restart to see the prompts (same rule as new tools).

---

## Self-review notes

- Spec coverage: prompts (Task 1) ✓, docs (Task 2) ✓, gates (Task 3) ✓. The rival's audit-middleware lesson is explicitly rejected: mcp 2.3.0 `MCPServer` has no `add_middleware` (verified by introspection).
- The `snake_call` regex was checked against every prompt body above: each `word(` token is a registered tool (`drawing_new`, `draw_wall_with_openings`, `draw_wall`, `draw_room`, `place_element`, `element_types`, `draw_stair`, `draw_lift`, `draw_equipment`, `draw_axis_grid`, `add_dimension`, `drawing_sheet`, `drawing_screenshot`, `drawing_verify`, `drawing_summary`, `drawing_history`, `drawing_undo`, `drawing_rollback_to`, `drawing_save`, `drawing_open`, `acad_connect`, `plugin_install`, `drawing_replay`, `drawing_status` — all in the 49-tool registry). Helper functions are deliberately named without parentheses.
- `kind="shaft"` and `backend="dxf"` contain `=` not `(`, so they cannot trip the regex.
- Type consistency: prompt names and signatures identical across Tasks 1–3 and the sandbox check.
- Length: within budget.
