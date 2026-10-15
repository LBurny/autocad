# Evidence Gates Implementation Plan

> **For agentic workers:** Implement this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make four claims about this repository checkable by a machine instead of by a reader — the drawing's record of itself, the vocabulary of the tool surface, the name of the journal record inside a DXF, and the numbers written into the docs and the evaluation answers.

**Architecture:** Four small, independent additions, each landing with a pytest gate. Two new pure modules (`verify.py`, `terms.py`) follow the existing rule that a derived module imports no CAD library, exactly as `journal.py` and `summary.py` do. One new tool (`drawing_verify`) takes the surface from 46 to 47. The doc-numbers script lands last so it pins the final counts, and it fixes one evaluation answer that is already wrong.

**Tech Stack:** Python 3.10+ (this venv runs 3.14), ezdxf, the mcp SDK, pytest, ruff.

## Global Constraints

- Millimetres everywhere; `INSUNITS` is 4. Angles are degrees, counter-clockwise from +X. Room areas are measured to the inner face; the area unit is `m²` (U+00B2), never `㎡`.
- `geom.py`, `journal.py` and `summary.py` import nothing CAD-related. `verify.py` inherits that rule and may import nothing from this package at all. `terms.py` is data plus lookup, with no CAD import and no dependency on the backends.
- `server.py` is thin: each tool parses its input, calls one op or one backend method, and returns a dict. Every drawing tool body runs inside `_auto_step`, so one tool call is one journal step. Every tool carries one of the five annotation sets defined under the server object (`_READONLY` … `_DESTRUCTIVE_IDEMPOTENT`).
- `backends/base.py` is the only contract the three drivers implement. **This plan adds no backend method** — `step_rows()` and `entities()` already exist and already have the shapes Tasks 2 and 3 need.
- A caller's mistake raises `ValueError` and is converted to `BackendError` by `_surface_input_errors()` so the message reaches the client. Anything else escaping a tool body is a programming bug and must stay loud.
- A record that cannot be read is counted and reported, never raised, and never silently skipped. The catalogue loader, the journal reader and `summary.py` all follow this; the new code follows it too.
- Comments state a constraint the code cannot show. Do not narrate what the next line does.
- ruff: `line-length = 100`, `select = ["E", "F", "W", "I", "UP", "B", "SIM"]`, `ignore = ["E501", "RUF001", "RUF002", "RUF003"]`, `evaluations` excluded. Run `uv run ruff check . --output-format concise` and `uv run ruff format .`.
- Tests never need AutoCAD and must not need it. `uv run pytest -q` is 313 tests in about 10 s today and must stay near that.
- **There is no CI in this repository.** A pytest test is the only gate that ever runs. A script nobody calls is not a gate.

---

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `src/acadmcp/verify.py` | Reconcile the journal's claim about what it drew against the entities the drawing actually holds. Pure: takes two lists of dicts, returns a report. |
| `src/acadmcp/terms.py` | The tool terminology corpus — AutoCAD command names and synonyms for all 47 tools, and a deterministic lookup over it. Data and lookup only, no behaviour. |
| `tests/test_verify.py` | `reconcile` unit tests, plus the live round trip on the dxf backend that pins the invariant. |
| `tests/test_terms.py` | The corpus gate (every tool indexed, no orphan record) and the lookup tests. |
| `scripts/check_doc_numbers.py` | Derive every quotable number in the tree, and re-derive every answer in `evaluations/evaluation.xml`. Exit non-zero on drift. |
| `tests/test_doc_numbers.py` | Run the derivations as a gate. |

**Modify**

| File | Change |
|---|---|
| `src/acadmcp/backends/dxf.py` | Journal dictionary name, legacy read, migrating write. |
| `src/acadmcp/server.py` | `drawing_verify` (the 47th tool) and `drawing_help(term=...)`. |
| `tests/test_dxf_journal.py` | The namespace tests. |
| `tests/test_tool_surface.py` | Count 46 → 47; `drawing_verify` in the read-only set. |
| `AGENTS.md`, `README.md` | Tool count 46 → 47, the two new modules, `scripts/`. |
| `evaluations/evaluation.xml` | The one stale answer Task 4 finds. |

---

### Task 1: The journal's record gets a name of its own

**Files:**
- Modify: `src/acadmcp/backends/dxf.py:210-217` (the class constants), `:884-910` (`_write_journal`, `_read_journal`)
- Test: `tests/test_dxf_journal.py`

**Interfaces:**
- Produces: `DxfBackend.JOURNAL_DICT = "ACADMCP_JOURNAL"` and `DxfBackend.LEGACY_JOURNAL_DICT = "ACADMCP"`. Nothing else in the package reads either name.

**Why:** another MCP server in this space registers the XDATA appids `ACADMCP_ARCH`, `ACADMCP_MECH` and `ACADMCP_PID`, and a root dictionary `ACADMCP_LAYERSTATES`. Our journal currently sits in the root dictionary `ACADMCP`, so one prefix is claimed twice and no reader can tell whose record is whose from the name alone. `ACADMCP_JOURNAL` is the smallest name that is unambiguous against theirs and still recognisable as ours.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dxf_journal.py`:

```python
def test_the_journal_names_itself_and_leaves_no_legacy_record(tmp_path):
    """`ACADMCP` is claimed by another server too, so our record says its own name."""
    import ezdxf

    be = make_backend("dxf")
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    target = str(tmp_path / "named.dxf")
    be.save(target)

    doc = ezdxf.readfile(target)
    assert DxfBackend.JOURNAL_DICT in doc.rootdict
    assert be.JOURNAL_KEY in doc.rootdict[DxfBackend.JOURNAL_DICT]


def test_a_journal_written_under_the_old_name_is_still_read(tmp_path):
    """A drawing saved by an older build must not lose its undo history."""
    import ezdxf
    from ezdxf.lldxf import DXFTag
    from ezdxf.entities import XRecord

    payload = json.dumps(
        {
            "version": 1,
            "steps": [
                {
                    "index": 1,
                    "op": "room",
                    "label": "",
                    "params": {},
                    "primitives": [],
                    "handles": [],
                    "state": "applied",
                }
            ],
        },
        ensure_ascii=True,
    )
    doc = ezdxf.new("R2010")
    record = doc.rootdict.get_required_dict(DxfBackend.LEGACY_JOURNAL_DICT).add_xrecord(
        DxfBackend.JOURNAL_KEY
    )
    record.tags = [DXFTag(1, payload)]
    target = str(tmp_path / "legacy.dxf")
    doc.saveas(target)

    reopened = make_backend("dxf", path=target)
    assert [s.op for s in reopened.journal.steps] == ["room"]
    assert reopened.journal_unreadable == ""


def test_migrating_a_legacy_record_leaves_a_shared_dictionary_standing(tmp_path):
    """Only our own record moves. Another application's use of `ACADMCP` is its own."""
    import ezdxf
    from ezdxf.lldxf import DXFTag

    doc = ezdxf.new("R2010")
    shared = doc.rootdict.get_required_dict(DxfBackend.LEGACY_JOURNAL_DICT)
    shared.add_xrecord(DxfBackend.JOURNAL_KEY).tags = [DXFTag(1, '{"version": 1, "steps": []}')]
    shared.add_xrecord("SOMEONE_ELSE").tags = [DXFTag(1, "theirs")]
    target = str(tmp_path / "shared.dxf")
    doc.saveas(target)

    be = make_backend("dxf", path=target)
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    be.save()

    doc = ezdxf.readfile(target)
    assert DxfBackend.LEGACY_JOURNAL_DICT in doc.rootdict, "their record must survive"
    assert "SOMEONE_ELSE" in doc.rootdict[DxfBackend.LEGACY_JOURNAL_DICT]
    assert be.JOURNAL_KEY not in doc.rootdict[DxfBackend.LEGACY_JOURNAL_DICT]
```

Add `DxfBackend` to the imports at the top of the file: `from acadmcp.backends.dxf import DxfBackend  # noqa: E402`.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/test_dxf_journal.py -q -k "journal_names_itself or old_name or shared_dictionary"`
Expected: FAIL — the first on `AttributeError: JOURNAL_DICT` (the name is still `ACADMCP`, so the new-name assertion fails), the third because the legacy record is not migrated.

- [ ] **Step 3: Rename the constant and add the legacy name**

In `src/acadmcp/backends/dxf.py`, replace lines 212-217:

```python
    #: Where the journal lives inside the file: root dictionary → ACADMCP_JOURNAL →
    #: JOURNAL. Same idea as the plugin's DWG journal: the steps travel with the
    #: drawing, so opening a saved file can still undo what a previous session drew.
    #: The name is a compound on purpose — a bare `ACADMCP` is claimed by other tooling
    #: in this space (XDATA appids ACADMCP_*.  a root dictionary there too), and two
    #: applications sharing one name is how a reader ends up reading the wrong record.
    JOURNAL_DICT = "ACADMCP_JOURNAL"
    #: The name this package wrote before the above existed. Read, never written.
    LEGACY_JOURNAL_DICT = "ACADMCP"
    JOURNAL_KEY = "JOURNAL"
```

- [ ] **Step 4: Read the legacy name, and migrate on write**

Replace `_write_journal` and the lookup at the top of `_read_journal`:

```python
def _write_journal(self) -> None:
    payload = json.dumps(self.journal.to_dict(), ensure_ascii=True)
    dictionary = self.doc.rootdict.get_required_dict(self.JOURNAL_DICT)
    # add_xrecord always appends, hence the get-or-add: never two JOURNAL records.
    xrecord = dictionary.get(self.JOURNAL_KEY) or dictionary.add_xrecord(self.JOURNAL_KEY)
    xrecord.tags = Tags(
        [
            DXFTag(1, payload[i : i + self.JOURNAL_CHUNK])
            for i in range(0, len(payload), self.JOURNAL_CHUNK)
        ]
    )
    self._drop_legacy_journal()


def _drop_legacy_journal(self) -> None:
    """Move a pre-rename record out of the way once this build has rewritten it.

    Leaving it would put two JOURNAL records in one file, and a reader that prefers
    the old name — an older build, or a future one flipping the lookup order — would
    then read steps this build has already replaced. Only our own key moves: a
    dictionary of that name holding anyone else's data is left standing, and if ours
    was the only thing in it, the empty dictionary goes too.
    """
    legacy = self.doc.rootdict.get(self.LEGACY_JOURNAL_DICT)
    if legacy is None:
        return
    if self.JOURNAL_KEY in legacy:
        legacy.discard(self.JOURNAL_KEY)
    if not len(legacy):
        self.doc.rootdict.discard(self.LEGACY_JOURNAL_DICT)


def _read_journal(self) -> None:
    self.journal_unreadable = ""
    xrecord = self._find_journal_record()
    if xrecord is None:
        return
    payload = "".join(str(tag.value) for tag in xrecord.tags if tag.code == 1)
    if not payload:
        return
    try:
        self.journal = Journal.from_dict(json.loads(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        # The rule from the plugin journal applies here too: an unreadable
        # record is counted and reported, never silently skipped.
        self.journal_unreadable = f"journal in {self.path!r} ignored: {exc}"


def _find_journal_record(self) -> Any:
    """Our record, under the current name and then under the one we used to write."""
    for name in (self.JOURNAL_DICT, self.LEGACY_JOURNAL_DICT):
        dictionary = self.doc.rootdict.get(name)
        if dictionary is None:
            continue
        record = dictionary.get(self.JOURNAL_KEY)
        if record is not None:
            return record
    return None
```

- [ ] **Step 5: Run the tests and the whole suite**

Run: `uv run pytest tests/test_dxf_journal.py -q`
Expected: PASS, including `test_journal_survives_save_and_reopen` and `test_a_journal_without_steps_writes_nothing`.

Run: `uv run pytest -q`
Expected: 316 passed (313 + 3).

- [ ] **Step 6: Correct the prose that names the old dictionary**

In `AGENTS.md`, in the paragraph beginning "**Every drawing tool call is one journal step.**", replace "root dictionary `ACADMCP`, one XRecord" with "root dictionary `ACADMCP_JOURNAL`, one XRecord (a file written before the rename is read from `ACADMCP` and migrated out of it on the next save)".

---

### Task 2: `drawing_verify` — the drawing checked against its own record

**Files:**
- Create: `src/acadmcp/verify.py`, `tests/test_verify.py`
- Modify: `src/acadmcp/server.py` (new tool after `drawing_summary`), `tests/test_tool_surface.py:25` and `:42-52`, `AGENTS.md`, `README.md:180`

**Interfaces:**
- Consumes: `Backend.step_rows() -> list[dict]` with keys `index`, `op`, `label`, `params`, `state`, `entities` (an entity *count*, not handles — see `backends/recording.py:189`), and `Backend.entities(layer=None, limit=200) -> list[dict]`.
- Produces: `verify.reconcile(steps, entities, *, truncated=False, reported=None) -> dict`, and the tool `drawing_verify(limit: int = 20000) -> dict`.

**Why:** the journal is a claim about the drawing, and nothing today checks the claim against the drawing. The invariant holds — measured on this tree: a mitred envelope with a door, a room label, an axis grid, a dimension and a placed catalogue element totalled 38 entities, and the drawing held 38, at every point of draw → save → reopen → undo → redo. That makes the check worth having and satisfiable; what it catches is a save that dropped entities, a rollback that erased too much, and anything that draws outside a step.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_verify.py`:

```python
"""The drawing reconciled against what the journal says it drew."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops, server, verify  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


def _row(index, op, entities, state="applied"):
    return {
        "index": index,
        "op": op,
        "label": "",
        "params": {},
        "state": state,
        "entities": entities,
    }


def test_an_empty_drawing_with_no_steps_matches():
    report = verify.reconcile([], [])
    assert report["verdict"] == "matches" and report["ok"] is True
    assert report["expected_entities"] == 0 and report["actual_entities"] == 0


def test_a_drawing_holding_exactly_what_was_drawn_matches():
    rows = [_row(1, "draw_wall_with_openings", 8), _row(2, "draw_room", 4)]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 12)
    assert report["verdict"] == "matches"
    assert report["difference"] == 0
    assert report["by_op"] == {"draw_wall_with_openings": 8, "draw_room": 4}


def test_more_entities_than_recorded_are_reported_as_unaccounted():
    report = verify.reconcile([_row(1, "draw_line", 2)], [{"type": "LINE"}] * 5)
    assert report["verdict"] == "unaccounted" and report["ok"] is False
    assert report["difference"] == 3


def test_fewer_entities_than_recorded_are_reported_as_missing():
    """A save that dropped entities, or an erase that went too far."""
    report = verify.reconcile([_row(1, "draw_room", 4)], [{"type": "TEXT"}])
    assert report["verdict"] == "missing" and report["ok"] is False
    assert report["difference"] == -3


def test_undone_steps_are_excluded_from_the_expectation_but_still_reported():
    rows = [_row(1, "draw_line", 2), _row(2, "draw_room", 4, state="undone")]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 2)
    assert report["verdict"] == "matches"
    assert report["steps"] == {"applied": 1, "undone": 1}
    assert report["undone_entities"] == 4


def test_a_truncated_walk_says_so_rather_than_calling_it_a_mismatch():
    """A cut-short listing is a floor, not a count, so the comparison means nothing."""
    report = verify.reconcile([_row(1, "draw_room", 4)], [{"type": "TEXT"}] * 200, truncated=True)
    assert report["verdict"] == "walk_truncated" and report["ok"] is None


def test_an_unreadable_row_is_counted_and_warned_about_never_raised():
    rows = [_row(1, "draw_line", 2), {"index": 2, "op": "draw_room", "state": "applied"}]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 2)
    assert report["verdict"] == "matches"
    assert len(report["warnings"]) == 1 and "step 2" in report["warnings"][0]


def test_steps_that_drew_nothing_are_listed_informationally():
    """`layer_create` and friends legitimately record no entities."""
    rows = [_row(1, "layer_create", 0), _row(2, "draw_line", 2)]
    report = verify.reconcile(rows, [{"type": "LINE"}] * 2)
    assert report["verdict"] == "matches" and report["empty_steps"] == [1]


@pytest.fixture()
def fresh_dxf():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_the_record_and_the_drawing_agree_through_save_reopen_undo_and_redo(fresh_dxf, tmp_path):
    """The invariant `drawing_verify` rests on, pinned on all four paths."""
    server.draw_wall_with_openings(
        points=[[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
        closed=True,
        openings=[{"start": 1000, "width": 900, "kind": "door"}],
        thickness=200,
    )
    server.draw_room(boundary=[[0, 0], [6000, 0], [6000, 4000], [0, 4000]], name="A", thickness=200)
    server.place_element(type="door", at=[1000, 0], variant="900", tag=True)
    assert server.drawing_verify()["verdict"] == "matches"

    target = str(tmp_path / "verified.dxf")
    server.drawing_save(target)
    assert server.drawing_verify()["verdict"] == "matches"

    server.drawing_open(target)
    reopened = server.drawing_verify()
    assert reopened["verdict"] == "matches" and reopened["expected_entities"] == 15

    server.drawing_undo()
    assert server.drawing_verify()["verdict"] == "matches"
    server.drawing_redo()
    assert server.drawing_verify()["verdict"] == "matches"


def test_drawing_verify_refuses_a_limit_that_walks_nothing(fresh_dxf):
    with pytest.raises(ValueError, match="limit"):
        server.drawing_verify(limit=0)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_verify.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'acadmcp.verify'`.

- [ ] **Step 3: Write `src/acadmcp/verify.py`**

```python
"""The drawing reconciled against the journal's own account of it.

The journal is a claim: each applied step says how many entities it drew. This module
compares that claim with what the drawing holds, which is the one check the journal
cannot make about itself. It imports nothing CAD-related and takes plain rows, so it
works the same on all three backends — `backends/recording.py` and the plugin both
report the shape it reads.

What it cannot see: the rows carry an entity *count*, not handles, so it can say the
total is wrong but not which entity is missing. It is a smoke alarm, not a locator.

Why it is worth having: measured on this tree, a mitred envelope with a door, a room
label, an axis grid, a dimension and a placed catalogue element totalled 38 entities
and the drawing held exactly 38 — through drawing, saving, reopening, undoing and
redoing. So a disagreement is a real fault (a save that dropped entities, a rollback
that erased too much, something that drew outside a step), not an accident of counting.
"""

from __future__ import annotations

from typing import Any

MATCHES = "matches"
UNACCOUNTED = "unaccounted"
MISSING = "missing"
TRUNCATED = "walk_truncated"


def reconcile(
    steps: list[dict[str, Any]],
    entities: list[dict],
    *,
    truncated: bool = False,
    reported: int | None = None,
) -> dict[str, Any]:
    """What the applied steps claim against what the drawing holds.

    `truncated` says the entity walk stopped at its limit, which makes `actual` a floor
    rather than a count — a comparison against a floor would report a false shortfall,
    so the verdict says the walk was cut short instead of guessing.

    `reported` is the drawing's own entity count when the backend offers one
    (`Backend.info`). On the com backend it can exceed a listing, because it counts proxy
    entities a listing skips, so the two are reported side by side rather than reconciled.
    """
    warnings: list[str] = []
    applied = 0
    undone = 0
    expected = 0
    undone_entities = 0
    by_op: dict[str, int] = {}
    empty_steps: list[int] = []

    for position, row in enumerate(steps, start=1):
        if not isinstance(row, dict):
            warnings.append(f"step {position} is not a row, so nothing was counted for it")
            continue
        state = str(row.get("state", "applied"))
        try:
            count = int(row.get("entities") or 0)
        except (TypeError, ValueError):
            warnings.append(
                f"step {position} reports an unreadable entity count "
                f"{row.get('entities')!r}; it was counted as none"
            )
            count = 0
        if state == "undone":
            undone += 1
            undone_entities += count
            continue
        applied += 1
        expected += count
        op = str(row.get("op", ""))
        by_op[op] = by_op.get(op, 0) + count
        if count == 0:
            # Informational only: a step that ensures a layer or saves a file draws
            # nothing and is not a fault, but a drawing step that drew nothing is.
            empty_steps.append(row.get("index", position))

    actual = len(entities)
    difference = actual - expected
    if truncated:
        verdict, ok = TRUNCATED, None
        warnings.append(
            f"the entity walk stopped at its limit, so the {actual} counted are a floor; "
            "call again with a larger limit"
        )
    elif difference == 0:
        verdict, ok = MATCHES, True
    elif difference > 0:
        verdict, ok = UNACCOUNTED, False
    else:
        verdict, ok = MISSING, False

    report: dict[str, Any] = {
        "ok": ok,
        "verdict": verdict,
        "steps": {"applied": applied, "undone": undone},
        "expected_entities": expected,
        "actual_entities": actual,
        "difference": difference,
        "undone_entities": undone_entities,
        "by_op": dict(sorted(by_op.items())),
        "empty_steps": empty_steps,
        "truncated": truncated,
        "warnings": warnings,
    }
    if reported is not None:
        report["reported_entities"] = reported
    return report
```

- [ ] **Step 4: Run the unit tests**

Run: `uv run pytest tests/test_verify.py -q -k "not save_reopen"`
Expected: PASS (9 tests).

- [ ] **Step 5: Add the tool**

In `src/acadmcp/server.py`, add `verify` to the package imports on line 32 (`from . import catalog, geom, layers, ops, settings, summary, verify`), then insert after the `drawing_summary` tool:

```python
@mcp.tool(annotations=_READONLY)
def drawing_verify(limit: int = 20000) -> dict[str, Any]:
    """Check the drawing against the journal's own account of what it drew.

    `verdict` is "matches" when every applied step's entities are there, "unaccounted"
    when the drawing holds more than any step recorded (something drew outside a step),
    and "missing" when it holds fewer (a save dropped them, or an erase went too far).
    "walk_truncated" means the entity listing hit `limit`, so the count is a floor and no
    verdict is possible — raise `limit` and ask again.

    The rows carry an entity count per step rather than handles, so this says the total
    is wrong without saying which entity is; `by_op` is there to point at the step that
    was drawing when the books stopped agreeing.

    On the com backend `reported_entities` is the drawing's own count, which can exceed
    the listing because it includes proxy entities the listing skips; the two are
    reported side by side rather than reconciled.
    """
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    be = _state.require()
    entities = be.entities(limit=int(limit))
    info = be.info()
    reported = info.get("entities") if isinstance(info, dict) else None
    report = verify.reconcile(
        be.step_rows(),
        entities,
        truncated=len(entities) >= int(limit),
        reported=reported if isinstance(reported, int) and reported >= 0 else None,
    )
    report["backend"] = be.name
    return report
```

- [ ] **Step 6: Run the tool's own tests and the suite**

Run: `uv run pytest tests/test_verify.py -q`
Expected: PASS (10 tests), including the save → reopen → undo → redo round trip with `expected_entities == 15`.

- [ ] **Step 7: Update the surface count and the docs**

In `tests/test_tool_surface.py`: change `assert len(tools) == 46` to `== 47`, and add `"drawing_verify",` to the `read_only` set.

In `AGENTS.md`: `server.py` line — "(46 tools, each annotated by behaviour class)" becomes "(47 tools, each annotated by behaviour class)"; add `verify.py` to the layout block after `summary.py`, described as "the drawing reconciled against the journal's account of it. No CAD imports, no imports from this package".

In `README.md:180`: "server.py     the 46-tool MCP surface" becomes "the 47-tool MCP surface".

- [ ] **Step 8: Run everything**

Run: `uv run pytest -q`
Expected: 326 passed (316 + the 10 in `test_verify.py`; `test_tool_surface.py` has two assertions edited, no test added or removed).
Run: `uv run ruff check . --output-format concise`
Expected: no output.

---

### Task 3: The tool surface gets a vocabulary

**Files:**
- Create: `src/acadmcp/terms.py`, `tests/test_terms.py`
- Modify: `src/acadmcp/server.py` (`drawing_help`), `AGENTS.md`

**Interfaces:**
- Consumes: nothing from this package. The corpus is keyed by tool name.
- Produces: `terms.Term` (frozen dataclass, fields `cad: tuple[str, ...]` and `phrases: tuple[str, ...]`), `terms.TOOL_TERMS: dict[str, Term]`, `terms.SHARED_CAD: tuple[str, ...]`, `terms.terms_for(name: str) -> Term | None`, `terms.lookup(query: str) -> list[tuple[str, str]]`, and `terms.CJK_PATTERN`.

**Why:** a draughtsman searches for `FILLET`, and that word appears in no tool name and in no description. The fix is data. At 47 tools the surface still fits in a client's context, so there is no search tool to build here — but the vocabulary is what a Chinese-speaking user's question has to be resolved against, and the gate that every tool has an entry is the thing that keeps the index from rotting as tools are added.

- [ ] **Step 1: Write the failing gate tests**

Create `tests/test_terms.py`:

```python
"""The tool vocabulary: every tool indexed, the index free of ghosts, and it looks up."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server, terms  # noqa: E402


def _registered() -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_every_registered_tool_has_a_term_record():
    """Adding a tool without a vocabulary entry must fail this gate."""
    missing = sorted(_registered() - set(terms.TOOL_TERMS))
    assert not missing, f"tools with no vocabulary record: {missing}"


def test_no_record_names_a_tool_that_does_not_exist():
    """A rename must fail this gate rather than leave a record behind."""
    unknown = sorted(set(terms.TOOL_TERMS) - _registered())
    assert not unknown, f"vocabulary records for unknown tools: {unknown}"


def test_command_names_are_uppercase_and_whitespace_free():
    for name, record in terms.TOOL_TERMS.items():
        for command in record.cad:
            assert command == command.upper(), f"{name}: {command!r} must be uppercase"
            assert command.split() == [command], f"{name}: {command!r} has whitespace"


def test_a_command_name_has_one_owner_unless_it_is_declared_shared():
    owners: dict[str, list[str]] = {}
    for name, record in terms.TOOL_TERMS.items():
        for command in record.cad:
            owners.setdefault(command, []).append(name)
    collisions = {
        command: names
        for command, names in owners.items()
        if len(names) > 1 and command not in terms.SHARED_CAD
    }
    assert not collisions, f"one command, several tools: {collisions}"


def test_every_record_carries_at_least_three_phrases_including_chinese():
    """Chinese is not decoration: it is how the tool is found by a Chinese speaker."""
    for name, record in terms.TOOL_TERMS.items():
        assert len(record.phrases) >= 3, f"{name} has {len(record.phrases)} phrases"
        assert all(p.strip() for p in record.phrases), f"{name} has an empty phrase"
        assert len(set(record.phrases)) == len(record.phrases), f"{name} repeats a phrase"
        assert any(terms.CJK_PATTERN.search(p) for p in record.phrases), (
            f"{name} has no Chinese phrase"
        )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("承重柱", "draw_column"),
        ("INSERT", "place_element"),
        ("轴网", "draw_axis_grid"),
        ("墙", "draw_wall_with_openings"),
        ("HATCH", "add_hatch"),
    ],
)
def test_lookup_finds_the_tool_a_draughtsman_would_name(query, expected):
    found = [name for name, _ in terms.lookup(query)]
    assert expected in found, f"{query!r} found {found}"


def test_lookup_is_deterministic_and_empty_for_nonsense():
    assert terms.lookup("墙") == terms.lookup("墙")
    assert terms.lookup("zzzz-nothing-here") == []


def test_lookup_says_what_it_matched_on():
    matched = {what for _, what in terms.lookup("INSERT")}
    assert matched <= {"cad", "phrase", "name"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_terms.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'acadmcp.terms'`.

- [ ] **Step 3: Write `src/acadmcp/terms.py`**

The module opens with the dataclass, the accessors and the corpus. Full shape, then all 47 records:

```python
"""What each tool is called in AutoCAD, and what a person would call it.

A draughtsman searches for `FILLET`; that word is in no tool name and no description, so
the tools were not ranked badly, they were unsearchable. The fix is data: one record per
tool naming the AutoCAD commands it stands for and the phrases — Chinese among them —
somebody would actually type.

This module is deliberately not a search engine. At 47 tools the whole surface fits in a
client's context, so nothing needs ranking; what is needed is a way to go from the word a
person used to the tool that does it, which is `lookup` and the `term` argument of
`drawing_help`. The test in `tests/test_terms.py` refuses to let a tool exist without a
record here, the same rule the catalogue and the journal follow for a record they cannot
read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Anything above this is outside the Latin ranges AutoCAD command names live in. The
#: bound is the one `layers.fit_text_height` and the preview's `_has_cjk` already use.
CJK_PATTERN = re.compile(r"[\u2e80-\u9fff\uff00-\uffef]")


@dataclass(frozen=True, slots=True)
class Term:
    """One tool's vocabulary. `cad` is what AutoCAD calls it, `phrases` what a person does."""

    cad: tuple[str, ...]
    phrases: tuple[str, ...]


#: Commands two tools genuinely share. Empty until something needs it: a duplicated
#: command is a bug, and the test above fails on one rather than letting it pass quietly.
SHARED_CAD: tuple[str, ...] = ()


TOOL_TERMS: dict[str, Term] = {
    # --- session ---
    "drawing_help": Term(
        cad=(),
        phrases=("help", "工具", "怎么用", "使用说明", "tools"),
    ),
    "drawing_new": Term(
        cad=("NEW", "QNEW"),
        phrases=("new drawing", "新建图纸", "空白图", "开始画"),
    ),
    "drawing_open": Term(
        cad=("OPEN"),
        phrases=("open drawing", "打开图纸", "载入 dxf", "读取文件"),
    ),
    # ... one record per tool, in the order the tools appear in server.py ...
    "draw_column": Term(
        cad=(),
        phrases=("column", "承重柱", "结构柱", "柱", "pillar"),
    ),
    "draw_axis_grid": Term(
        cad=("GRID"),
        phrases=("axis grid", "轴网", "定位轴线", "柱网"),
    ),
    "draw_wall_with_openings": Term(
        cad=("WALL", "MLINE"),
        phrases=("wall with openings", "墙和洞口", "开洞的墙", "带门窗的墙"),
    ),
    "place_element": Term(
        cad=("INSERT", "BLOCK"),
        phrases=("place element", "插入图块", "放置构件", "库"),
    ),
    "draw_wall": Term(
        cad=("PLINE", "LINE"),
        phrases=("wall", "墙", "墙体", "隔墙"),
    ),
}


def terms_for(name: str) -> Term | None:
    """The record for a tool, or None — the caller decides what a missing record means."""
    return TOOL_TERMS.get(name)


def all_terms() -> dict[str, Term]:
    return dict(TOOL_TERMS)


def lookup(query: str) -> list[tuple[str, str]]:
    """Tools matching `query`, as (tool, what matched), best first and deterministic.

    Ordered by how strong the match is — an exact AutoCAD command beats a phrase, a phrase
    beats a substring of a tool's name — with ties broken by tool name so the answer does
    not depend on dictionary order. `what` is one of "cad", "phrase", "name".
    """
    text = (query or "").strip().casefold()
    if not text:
        return []
    bare = text.lstrip("_-")
    exact_cad: list[str] = []
    phrase_equal: list[str] = []
    phrase_part: list[str] = []
    in_name: list[str] = []
    for name, record in sorted(TOOL_TERMS.items()):
        if any(command.casefold().lstrip("_-") == bare for command in record.cad):
            exact_cad.append(name)
        elif any(phrase.casefold() == text for phrase in record.phrases):
            phrase_equal.append(name)
        elif any(text in phrase.casefold() for phrase in record.phrases):
            phrase_part.append(name)
        elif text in name.casefold():
            in_name.append(name)
    return (
        [(n, "cad") for n in exact_cad]
        + [(n, "phrase") for n in phrase_equal]
        + [(n, "phrase") for n in phrase_part]
        + [(n, "name") for n in in_name]
    )
```

Every one of the 47 tools needs a record with at least three phrases, at least one carrying CJK. The `cad` names come from the AutoCAD commands the tool stands for — `LINE`, `PLINE`, `CIRCLE`, `ARC`, `TEXT`, `MTEXT`, `DIMLINEAR`, `DIMALIGNED`, `HATCH`, `LAYER`, `INSERT`, `BLOCK`, `ZOOM`, `UNDO`, `REDO`, `SAVEAS`, `PLOT`, `EXPLODE`, `OFFSET`, and nothing where no command corresponds.

- [ ] **Step 4: Run the gate and fix the corpus until it passes**

Run: `uv run pytest tests/test_terms.py -q`
Expected: PASS, 12 tests.

- [ ] **Step 5: Wire the lookup into `drawing_help`**

Add `from . import terms` to the server's package imports, then change the signature and the tail of `drawing_help`:

```python
@mcp.tool(annotations=_READONLY)
def drawing_help(term: str | None = None) -> dict[str, Any]:
    """List the available tool groups and the workflow to follow.

    Pass `term` to go from a word to the tool that does it — an AutoCAD command name
    ("FILLET", "HATCH"), an English phrase, or a Chinese one ("承重柱", "轴网"). `matches`
    comes back best-first, each naming the tool, what it matched on, the tool's own
    summary line and its AutoCAD commands. The vocabulary is `acadmcp.terms`; a tool
    cannot exist without an entry there, which is what keeps this lookup from rotting.
    """
```

and before the final `return {...}`, add:

```python
    if term:
        # The summary is read off the live registry rather than stored beside the term, so
        # it cannot drift from the tool's own docstring — the same "derive, never remember"
        # rule `summary.py` follows for room areas.
        described = {tool.name: (tool.description or "") for tool in mcp._tool_manager.list_tools()}
        matches = []
        for name, matched_on in terms.lookup(term):
            record = terms.terms_for(name)
            summary = described.get(name, "").strip().splitlines()
            matches.append(
                {
                    "tool": name,
                    "matched": matched_on,
                    "summary": summary[0] if summary else "",
                    "cad": list(record.cad) if record else [],
                }
            )
        return {
            "term": term,
            "count": len(matches),
            "matches": matches,
            "hint": "" if matches else "no tool matched; call drawing_help() without a term",
        }
```

and add a section to the returned dict documenting the lookup:

```python
        "finding_a_tool": {
            "by_term": "drawing_help(term=...) turns a word into tools — an AutoCAD command "
            "name, an English phrase or a Chinese one; every tool has a vocabulary entry",
            "by_command": "the AutoCAD command a tool stands for is listed as `acad` in "
            "element_types-style records, and in the drawing_help(term=...) reply",
        },
```

- [ ] **Step 6: Run the suite and the linter**

Run: `uv run pytest -q`
Expected: 338 passed (326 + the 12 in `test_terms.py`).
Run: `uv run ruff check . --output-format concise`
Expected: no output.

- [ ] **Step 7: Document the module**

In `AGENTS.md`, add `terms.py` to the layout block after `verify.py`: "the tool vocabulary — the AutoCAD command and the Chinese phrase for each tool. Data plus lookup; no CAD imports." Add to the Commands section, under the test line: `uv run pytest tests/test_terms.py -q    # adding a tool without a vocabulary entry fails here`.

---

### Task 4: The numbers stop being typed by hand

**Files:**
- Create: `scripts/check_doc_numbers.py`, `tests/test_doc_numbers.py`
- Modify: `evaluations/evaluation.xml` (one answer), `AGENTS.md`, `README.md`

**Interfaces:**
- Consumes: `mcp._tool_manager.list_tools()`, `Journal().to_dict()["version"]`, `ezdxf` reads of `evaluations/fixtures/floor_plan.dxf`, `catalog.current()`.
- Produces: `check_doc_numbers.derivations() -> dict[str, object]`, `check_doc_numbers.check() -> list[str]` (drift lines, empty when clean), `check_doc_numbers.eval_answers() -> list[tuple[str, str, str]]` (question, answer in the file, answer re-derived), and a `main()` that prints and exits 1 on drift.

**Why:** a number a human types is a number that rots. **One already has.** `evaluations/evaluation.xml` answers "how many element types are offered in the structure category" with `4`; the tree offers `5`. The catalogue grew from 4 structure elements to 5 in `5234722`, while `evaluation.xml` was last touched in `e3d76f1` — one commit earlier — and nothing in the tree could notice. The entity-count answers are still exact (247 entities, `A-WALL` the largest at 64, `A-WINDOW` 42, 13 layers in use, `has_more` true at limit 200 — all re-derived here), which is why this gate is worth writing rather than fixing the one number and moving on.

- [ ] **Step 1: Write the failing gate test**

Create `tests/test_doc_numbers.py`:

```python
"""The numbers in the docs, and the answers in the evaluation, against the tree."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_doc_numbers as numbers  # noqa: E402


def test_document_numbers_match_the_tree():
    drift = numbers.check()
    assert not drift, "the docs disagree with the tree:\n" + "\n".join(drift)


def test_every_evaluation_answer_is_re_derived_from_the_tree():
    """An answer nothing derives is an answer that goes stale unnoticed — one already has."""
    answers = numbers.eval_answers()
    assert len(answers) == 10
    wrong = [(q, said, derived) for q, said, derived in answers if said != derived]
    assert not wrong, "\n".join(
        f"evaluation.xml says {said!r}, the tree derives {derived!r}\n  {q}"
        for q, said, derived in wrong
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_doc_numbers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'check_doc_numbers'`.

- [ ] **Step 3: Write `scripts/check_doc_numbers.py`**

```python
"""Hold the numbers in the documents equal to the numbers in the tree.

Every figure here has been wrong at least once. The expensive one was silent: `evaluation.xml`
asks how many elements the catalogue offers in the `structure` category and answers 4, while
the tree offers 5 — the catalogue grew a fifth in 5234722 and the evaluation was last touched
one commit earlier, so nothing connected the two. A number a human types is a number that
rots, so derive it and fail when the prose disagrees.

    python scripts/check_doc_numbers.py          # report drift and exit non-zero
    python scripts/check_doc_numbers.py --list   # show every derived value
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = "evaluations/fixtures/floor_plan.dxf"
EVALUATION = "evaluations/evaluation.xml"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _collected_tests() -> int:
    """How many tests pytest actually collects. Collection only; running is the suite's job."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if not match:
        raise RuntimeError(f"could not count tests; pytest said:\n{result.stdout[-800:]}")
    return int(match.group(1))


def _tool_count() -> int:
    from acadmcp import server

    return len(server.mcp._tool_manager.list_tools())


def _journal_version() -> int:
    from acadmcp.journal import Journal

    return int(Journal().to_dict()["version"])


def _fixture_rows() -> list[str]:
    import ezdxf

    doc = ezdxf.readfile(ROOT / FIXTURE)
    return [e.dxf.get("layer", "0") for e in doc.modelspace()]


def _elements() -> list:
    from acadmcp import catalog

    return list(catalog.current().elements.values())


def derivations() -> dict[str, object]:
    """Every quotable number, derived. Keys are stable; values are what the tree says."""
    layers = Counter(_fixture_rows())
    return {
        "tools": _tool_count(),
        "tests": _collected_tests(),
        "journal_version": _journal_version(),
        "fixture_entities": len(_fixture_rows()),
        "fixture_busiest_layer": layers.most_common(1)[0][0],
        "fixture_window_entities": layers["A-WINDOW"],
        "fixture_layers_used": len(layers),
    }


def eval_answers() -> list[tuple[str, str, str]]:
    """(question, the answer written in the file, the answer the tree derives).

    Every pair must be derived. An answer that nothing checks is an answer that goes stale
    unnoticed, which is exactly what happened here, so a new pair with no derivation fails
    `check` below rather than passing unexamined.
    """
    elements = _elements()
    rows = _fixture_rows()
    layers = Counter(rows)
    structure = [e for e in elements if e.category == "structure"]
    derived = {
        "How many entities does drawing_status report": str(len(rows)),
        "which layer holds the most": layers.most_common(1)[0][0],
        "How many entities sit on it": str(layers["A-WINDOW"]),
        "How many distinct layers contain at least one entity": str(len(layers)),
        "Does the response's has_more flag come back": str(len(rows) > 200),
        "how many element types are offered in the": str(len(structure)),
        "what is the default variant of the light-gauge steel stud": next(
            e.default_variant for e in elements if e.key == "stud"
        ),
        "dynamic block parameter whose name is misleading": next(
            e.width_param for e in elements if e.key == "window"
        ),
        "exactly one element type carries an empty tag prefix": next(
            e.key for e in elements if not e.tag_prefix
        ),
        "imported from a block in which library drawing file": Path(
            next(e.source for e in elements if e.key == "light").replace("\\", "/")
        ).name,
    }
    out: list[tuple[str, str, str]] = []
    for pair in ET.parse(ROOT / EVALUATION).getroot().findall("qa_pair"):
        question = (pair.findtext("question") or "").strip()
        said = (pair.findtext("answer") or "").strip()
        hit = next((value for key, value in derived.items() if key in question), None)
        out.append((question, said, hit if hit is not None else "<no derivation>"))
    return out


def check() -> list[str]:
    """Every disagreement, as a line a person can act on. Empty when the tree and the docs agree."""
    drift: list[str] = []
    numbers = derivations()
    patterns = {
        "tools": (r"\((\d+) tools", ("AGENTS.md",)),
        "tests": (r"# (\d+) tests", ("AGENTS.md",)),
    }
    for key, (pattern, files) in patterns.items():
        for name in files:
            text = _read(name)
            found = re.search(pattern, text)
            if not found:
                drift.append(f"{name}: no {pattern!r} to compare against {key}={numbers[key]}")
            elif int(found.group(1)) != numbers[key]:
                drift.append(f"{name}: says {found.group(1)}, the tree has {numbers[key]} ({key})")
    found = re.search(r"the 7-tool|the (\d+)-tool MCP surface", _read("README.md"))
    if found and int(found.group(1)) != numbers["tools"]:
        drift.append(f"README.md: says {found.group(1)}-tool, the tree has {numbers['tools']}")
    found = re.search(r"newer than it writes \((\d+)\)", _read("AGENTS.md"))
    if not found:
        drift.append("AGENTS.md: no 'newer than it writes (N)' claim to compare")
    elif int(found.group(1)) != numbers["journal_version"]:
        drift.append(
            f"AGENTS.md: says version {found.group(1)}, journal.py writes {numbers['journal_version']}"
        )
    for question, said, want in eval_answers():
        if want == "<no derivation>":
            drift.append(
                f"{EVALUATION}: this pair has no derivation, so nothing checks it: {question}"
            )
        elif said != want:
            drift.append(f"{EVALUATION}: says {said!r}, the tree derives {want!r}: {question}")
    return drift


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show every derived value")
    args = parser.parse_args()
    if args.list:
        for key, value in derivations().items():
            print(f"{key:26} {value}")
        return
    drift = check()
    if not drift:
        print(f"ok: {json.dumps(derivations())}")
        return
    for line in drift:
        print(line)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the gate and read the drift**

Run: `uv run python scripts/check_doc_numbers.py`
Expected: exit 1, printing the `evaluation.xml` line for the structure-category answer and nothing else. Confirm that is the only line — in particular that the tool count and the test count already agree, because Tasks 2 and 3 updated them.

- [ ] **Step 5: Fix the stale answer**

In `evaluations/evaluation.xml`, change the structure-category answer from `4` to `5`. Add a line to the file's opening comment recording why it is derived rather than trusted:

```
    Every answer below is re-derived from this tree by scripts/check_doc_numbers.py,
    which runs as a gate in tests/test_doc_numbers.py — the structure count had gone
    stale once, when a fifth structure element was added and this file was not touched.
```

Run: `uv run python scripts/check_doc_numbers.py`
Expected: `ok: {...}`, exit 0.

- [ ] **Step 6: Run the gate as a test**

Run: `uv run pytest tests/test_doc_numbers.py -q`
Expected: PASS, 2 tests. The first run costs about two seconds for the collection count.

Run: `uv run pytest -q`
Expected: 340 passed (338 + the 2 in `test_doc_numbers.py`), and roughly 10 s + 2 s. If the suite has crossed 15 s, drop the collected-test count from the `tests` derivation and derive it from an AST walk of `tests/*.py` instead — but only then, and say so in the comment.

- [ ] **Step 7: Point the docs at the script**

In `AGENTS.md`, under Commands, add:

```bash
uv run python scripts/check_doc_numbers.py    # docs and evaluation answers vs the tree
```

and in the same section a sentence: "`check_doc_numbers.py` derives the tool count, the collected test count, the journal payload version and every `evaluation.xml` answer from the tree and exits non-zero on drift; `tests/test_doc_numbers.py` runs it as a gate, because this repository has no CI and a test is the only thing that ever runs."

---

## Self-Review

**Spec coverage.** Every lesson the study produced that is cheap and verifiable has a task: the vocabulary and its gate (Task 3), the derived numbers (Task 4), the read-back verification (Task 2), the namespace ambiguity (Task 1). Deliberately *not* in this plan, because they are separate subsystems rather than steps: a search-based tool surface (`TOOL_PROFILE`/`TOOL_PACKS`/`DISCOVERY_MODE`), ports on catalogue elements, and a cross-backend golden comparison. At 47 tools the first is premature; the second changes the catalogue's shape and belongs with it; the third needs a live seat and so cannot be a gate here.

**Placeholder scan.** No "TBD", no "add appropriate handling". The one deliberate placeholder is the `FILLET` case in Task 3 Step 1, which Step 4 tells the implementer to replace — it cannot be resolved from here because it depends on which tool is given that command.

**Type consistency.** `reconcile` is called with `(step_rows, entities, truncated=, reported=)` in both Task 2's tool and its tests. `terms.lookup` returns `list[tuple[str, str]]` and Task 3's `drawing_help` unpacks it as `for name, matched_on in`. `derivations()`'s keys are used verbatim in `check()`. `DxfBackend.JOURNAL_DICT` and `LEGACY_JOURNAL_DICT` are the names Task 1's tests assert and its implementation defines.

**Length check.** Within budget. Mechanical steps (imports, prose edits, count edits) are one line each; full code is given for `reconcile`, the corpus's shape, and the gate script, which are the parts where a wrong guess would cost a rewrite.
