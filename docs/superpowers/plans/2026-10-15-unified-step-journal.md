# Unified Step Journal Implementation Plan

> **For agentic workers:** Implement this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every backend (dxf / com / plugin) records each drawing action as a step in a journal, so a wrong coordinate costs `drawing_undo()` instead of a full redraw, and a headless DXF session can be replayed into AutoCAD.

**Architecture:** A CAD-free `journal.py` (pure bookkeeping) plus a `backends/recording.py` batching layer extracted from `backends/plugin.py`. Backends implement two primitives: `_apply(primitives) -> list[str]` (draw, return handles) and `_erase(handles) -> (erased, missing)`. DXF persists its journal into an XRecord inside the .dxf; the plugin backend keeps its DWG journal authoritative. `server.py` wraps every drawing tool in an implicit step.

**Tech Stack:** Python 3.14 venv, ezdxf, FastMCP/MCPServer (mcp 2.x shim), pytest, ruff.

## Global Constraints

- Millimetres everywhere; degrees CCW from +X.
- Tests never need AutoCAD: `.venv/Scripts/python.exe -m pytest -q` (NOT `uv run pytest` — uv sync fails while the MCP client holds the venv exe).
- Lint: `.venv/Scripts/python.exe -m ruff check . --output-format concise`; line-length 100; ignore E501.
- `geom.py` imports nothing CAD-related; `journal.py` likewise (pure Python, no ezdxf import).
- Primitive wire format is the plugin's existing one: `{"kind": str, "points": [x,y,x,y,...], ...}` — do not invent a second format.
- COM threading rule: attribute access on a COM object must happen inside a zero-arg callable passed to `_call`, e.g. `_call(lambda: obj.Delete())`, never `_call(obj.Delete)`.
- Comment style: explain constraints, don't narrate. Match existing density.

---

### Task 1: `journal.py` — pure step journal

**Files:**
- Create: `src/acadmcp/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `StepRecord(index:int, op:str, label:str, params:dict, primitives:list[dict], handles:list[str], state:str)` — dataclass, state is `"applied"` or `"undone"`
  - `Journal` with `.steps -> list[StepRecord]`, `.append(op, label, params, primitives, handles) -> StepRecord`, `.undo(count=1) -> list[StepRecord]` (newest applied first), `.redo(count=1) -> list[StepRecord]` (oldest undone first), `.rollback_to(index:int) -> list[StepRecord]`, `.describe() -> str`, `.to_dict() -> dict`, `Journal.from_dict(data:dict) -> Journal` (raises `ValueError` on garbage)

**Index convention (measured, not chosen):** the plugin journal's steps are **1-based** (`plugin/src/AcadMcp.Core/Journal.cs:16`: "index 0 means before the first step", so `rollback_to(0)` wipes everything). The Python journal matches it, or `drawing_history` and the palette would number the same step differently.

- [ ] **Step 1: Write the failing tests** — `tests/test_journal.py`:

```python
"""The journal is pure bookkeeping; the backends own erasing and redrawing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp.journal import Journal  # noqa: E402


def _filled(n=3):
    j = Journal()
    for i in range(n):
        j.append(f"op{i}", f"label{i}", {"i": i}, [{"kind": "line"}], [f"H{i}"])
    return j


def test_append_numbers_steps_in_order():
    j = _filled()
    assert [s.index for s in j.steps] == [1, 2, 3]
    assert j.steps[1].handles == ["H1"]


def test_undo_marks_newest_applied_first():
    j = _filled()
    doomed = j.undo(2)
    assert [s.op for s in doomed] == ["op2", "op1"]
    assert [s.state for s in j.steps] == ["applied", "undone", "undone"]


def test_redo_reapplies_oldest_undone_first():
    j = _filled()
    j.undo(2)
    back = j.redo(1)
    assert [s.op for s in back] == ["op1"]
    assert j.steps[2].state == "undone"


def test_a_new_step_drops_the_undone_tail():
    j = _filled()
    j.undo(2)
    j.append("opX", "", {}, [], ["HX"])
    assert [s.op for s in j.steps] == ["op0", "opX"]
    assert [s.index for s in j.steps] == [1, 2]


def test_rollback_to_undoes_everything_after_index():
    j = _filled(4)
    doomed = j.rollback_to(2)
    assert [s.op for s in doomed] == ["op3", "op2"]
    assert [s.state for s in j.steps] == ["applied", "applied", "undone", "undone"]


def test_rollback_to_zero_wipes_everything():
    j = _filled()
    doomed = j.rollback_to(0)
    assert len(doomed) == 3
    assert all(s.state == "undone" for s in j.steps)


def test_round_trip_through_dict():
    j = _filled()
    j.undo(1)
    clone = Journal.from_dict(j.to_dict())
    assert [(s.op, s.state, s.handles) for s in clone.steps] == [
        (s.op, s.state, s.handles) for s in j.steps
    ]


def test_from_dict_rejects_garbage():
    with pytest.raises(ValueError):
        Journal.from_dict({"steps": [{"no_op": True}]})


def test_describe_is_one_line_per_step():
    j = _filled(2)
    j.undo(1)
    text = j.describe()
    assert "op0" in text and "undone" in text and len(text.splitlines()) == 2
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_journal.py -q` — expect FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement** `src/acadmcp/journal.py`:

```python
"""The step journal, backend-neutral and CAD-free.

One journal entry per drawing action: what op produced it, with what
parameters, which primitives were drawn, and the handles of the entities that
came out. The journal itself only keeps the books — erasing and redrawing are
the backend's job, because they are the ones holding the entities.

Why this exists: a wrong coordinate used to cost a whole redraw. With the
journal, undo marks the step and the backend erases exactly its entities;
redo re-applies the recorded primitives, which is free because they were
never thrown away.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: A step is either on the drawing ("applied") or undone and re-doable.
APPLIED = "applied"
UNDONE = "undone"


@dataclass
class StepRecord:
    index: int
    op: str
    label: str
    params: dict[str, Any]
    primitives: list[dict[str, Any]]
    handles: list[str] = field(default_factory=list)
    state: str = APPLIED


class Journal:
    """Ordered steps with undo/redo bookkeeping. Holds no CAD objects."""

    def __init__(self) -> None:
        self._steps: list[StepRecord] = []

    @property
    def steps(self) -> list[StepRecord]:
        return list(self._steps)

    def append(
        self,
        op: str,
        label: str,
        params: dict[str, Any],
        primitives: list[dict[str, Any]],
        handles: list[str],
    ) -> StepRecord:
        # Drawing a new step drops the undone tail: undo erased those entities,
        # so there is nothing left to redo — keeping the records would promise
        # a redo that can only draw ghosts.
        self._steps = [s for s in self._steps if s.state == APPLIED]
        # 1-based, matching the plugin journal ("index 0 means before the first
        # step"), so a step has the same number here and in the DWG palette.
        record = StepRecord(
            len(self._steps) + 1, op, label, dict(params), list(primitives), list(handles)
        )
        self._steps.append(record)
        return record

    def undo(self, count: int = 1) -> list[StepRecord]:
        """Mark the newest applied steps undone, newest first."""
        doomed = [s for s in reversed(self._steps) if s.state == APPLIED][: max(0, count)]
        for step in doomed:
            step.state = UNDONE
        return doomed

    def redo(self, count: int = 1) -> list[StepRecord]:
        """Mark the oldest undone steps applied again, oldest first.

        Undone steps always form a contiguous tail (undo and rollback both take
        from the end), so the oldest-first order replays history the way it was
        drawn.
        """
        chosen = [s for s in self._steps if s.state == UNDONE][: max(0, count)]
        for step in chosen:
            step.state = APPLIED
        return chosen

    def rollback_to(self, index: int) -> list[StepRecord]:
        """Undo every applied step after `index`, newest first."""
        doomed = [s for s in self._steps if s.state == APPLIED and s.index > index]
        for step in doomed:
            step.state = UNDONE
        return list(reversed(doomed))

    def describe(self) -> str:
        """One line per step, for a tool result a person has to read."""
        if not self._steps:
            return "no steps"
        return "\n".join(
            f"{s.index:>3}  {s.state:<8} {s.label or s.op} ({len(s.handles)} entities)"
            for s in self._steps
        )

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "steps": [asdict(s) for s in self._steps]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Journal:
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            raise ValueError("journal payload has no 'steps' list")
        journal = cls()
        for position, row in enumerate(steps):
            try:
                record = StepRecord(
                    index=int(row["index"]),
                    op=str(row["op"]),
                    label=str(row.get("label", "")),
                    params=dict(row.get("params", {})),
                    primitives=list(row.get("primitives", [])),
                    handles=[str(h) for h in row.get("handles", [])],
                    state=str(row.get("state", APPLIED)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"journal step {position} is unreadable: {exc}") from exc
            if record.state not in (APPLIED, UNDONE):
                raise ValueError(f"journal step {position} has unknown state {record.state!r}")
            journal._steps.append(record)
        return journal
```

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_journal.py -q` — expect 8 PASS

---

### Task 2: `backends/recording.py` — the shared batching layer

**Files:**
- Create: `src/acadmcp/backends/recording.py`
- Test: `tests/test_recording.py`

**Interfaces:**
- Consumes: `acadmcp.journal.Journal`, `acadmcp.geom.Point/xy`
- Produces:
  - `flatten_points(points) -> list[float]`, `unflatten_points(flat) -> list[tuple[float,float]]`
  - `RecordingBackend` with: `__init__()`, `begin_step(op, label=None, params=None)`, `abort_step()`, `commit_step() -> StepRecord`, `step(op, label=None, params=None)` (contextmanager), `replay_step(record: StepRecord) -> StepRecord`, `undo(count=1) -> dict`, `redo(count=1) -> dict`, `history() -> str`, properties `open_step -> str`, `pending -> list[dict]`; protected `_record(kind, points, **kw) -> int`, `_reset()`
  - Abstract: `_apply(primitives: list[dict]) -> list[str]`, `_erase(handles: list[str]) -> tuple[int,int]` (erased, missing)

- [ ] **Step 1: Write the failing tests** — `tests/test_recording.py`, using a list-backed fake:

```python
"""RecordingBackend, exercised through an in-memory fake — no CAD anywhere."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp.backends.base import BackendError  # noqa: E402
from acadmcp.backends.recording import RecordingBackend, unflatten_points  # noqa: E402


class ListBackend(RecordingBackend):
    """Applies primitives by appending them to a list; handles are positions."""

    def __init__(self):
        super().__init__()
        self.drawn: list[dict] = []

    def _apply(self, primitives):
        for prim in primitives:
            self.drawn.append(prim)
        return [f"H{len(self.drawn) - len(primitives) + i}" for i in range(len(primitives))]

    def _erase(self, handles):
        erased = missing = 0
        for h in handles:
            index = int(h[1:])
            if 0 <= index < len(self.drawn) and self.drawn[index] is not None:
                self.drawn[index] = None
                erased += 1
            else:
                missing += 1
        return erased, missing


def test_recording_outside_a_step_raises():
    be = ListBackend()
    with pytest.raises(BackendError):
        be._record("line", [(0, 0), (1, 1)])


def test_commit_draws_and_journals_the_handles():
    be = ListBackend()
    with be.step("draw_line", params={"a": 1}):
        be._record("line", [(0, 0), (1, 1)], layer="A-WALL")
    assert be.drawn == [{"kind": "line", "points": [0.0, 0.0, 1.0, 1.0], "layer": "A-WALL"}]
    step = be.journal.steps[0]
    assert step.op == "draw_line" and step.handles == ["H0"] and step.params == {"a": 1}


def test_none_values_are_not_recorded():
    be = ListBackend()
    with be.step("x"):
        be._record("line", [(0, 0), (1, 1)], linetype=None)
    assert "linetype" not in be.drawn[0]


def test_an_exception_inside_step_leaves_nothing_behind():
    be = ListBackend()
    with pytest.raises(RuntimeError), be.step("boom"):
        be._record("line", [(0, 0), (1, 1)])
        raise RuntimeError("halfway")
    assert be.drawn == [] and be.journal.steps == [] and be.open_step == ""


def test_undo_erases_and_redo_redraws():
    be = ListBackend()
    with be.step("a"):
        be._record("line", [(0, 0), (1, 0)])
    with be.step("b"):
        be._record("circle", [(5, 5)], radius=10.0)
    report = be.undo(1)
    assert report["erased"] == 1 and be.drawn[1] is None
    report = be.redo(1)
    assert report["redrawn"] == 1 and be.drawn[1] == {
        "kind": "circle",
        "points": [5.0, 5.0],
        "radius": 10.0,
    }


def test_steps_do_not_nest():
    be = ListBackend()
    be.begin_step("outer")
    with pytest.raises(BackendError):
        be.begin_step("inner")


def test_replay_step_appends_a_foreign_record():
    source = ListBackend()
    with source.step("draw_room", label="room", params={"w": 4}):
        source._record("line", [(0, 0), (1, 0)])
    target = ListBackend()
    record = target.replay_step(source.journal.steps[0])
    assert record.op == "draw_room" and target.drawn and record.index == 0


def test_unflatten_round_trips():
    assert unflatten_points([1.0, 2.0, 3.0, 4.0]) == [(1.0, 2.0), (3.0, 4.0)]
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_recording.py -q` — expect FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement** `src/acadmcp/backends/recording.py`:

```python
"""The batching layer every journal-capable backend shares.

Extracted from the plugin backend, where the pattern was proven: drawing calls
do not draw — they append a primitive to the open step, and the whole step is
applied as one unit. A step is the unit a user undoes; if every line were its
own step, "take back the room I just drew" would mean finding forty of them.

Subclasses supply two primitives and get undo/redo/history for free:

* ``_apply(primitives) -> [handles]`` — draw the batch, report what came out
* ``_erase(handles) -> (erased, missing)`` — delete by handle, tolerating
  entities that something else already deleted (a hand edit in AutoCAD is not
  an error here, it is counted)
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from typing import Any

from ..geom import Point, xy
from ..journal import Journal, StepRecord
from .base import BackendError


def flatten_points(points: Sequence[Point]) -> list[float]:
    """Flatten to the x, y, x, y… wire form the journal stores."""
    flat: list[float] = []
    for point in points:
        x, y = xy(point)
        flat.extend((x, y))
    return flat


def unflatten_points(flat: Sequence[float]) -> list[tuple[float, float]]:
    values = [float(v) for v in flat]
    return [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]


class RecordingBackend:
    """Base for backends that draw step-by-step and keep a :class:`Journal`."""

    name = "recording"

    def __init__(self) -> None:
        self.journal = Journal()
        self._op = ""
        self._label: str | None = None
        self._params: dict[str, Any] = {}
        self._pending: list[dict[str, Any]] = []

    # --- steps -------------------------------------------------------------

    @property
    def pending(self) -> list[dict[str, Any]]:
        return self._pending

    @property
    def open_step(self) -> str:
        """The op name being recorded, or "" when no step is open."""
        return self._op

    def begin_step(
        self, op: str, label: str | None = None, params: dict[str, Any] | None = None
    ) -> None:
        if self._op:
            raise BackendError(
                f"a step ({self._op!r}) is already open; steps do not nest — "
                "commit or abort it first"
            )
        self._op = op
        self._label = label
        self._params = dict(params or {})
        self._pending = []

    def commit_step(self) -> StepRecord:
        """Apply the recorded batch and journal the resulting handles."""
        if not self._op:
            raise BackendError("no step is open; call begin_step first")
        if not self._pending:
            raise BackendError(
                f"step {self._op!r} recorded no primitives; an empty step can "
                "never be rolled back meaningfully"
            )
        handles = self._apply(list(self._pending))
        record = self.journal.append(
            self._op, self._label or "", self._params, self._pending, handles
        )
        self._reset()
        return record

    def abort_step(self) -> None:
        """Throw away the recorded batch without drawing anything."""
        self._reset()

    @contextlib.contextmanager
    def step(
        self, op: str, label: str | None = None, params: dict[str, Any] | None = None
    ) -> Iterator[None]:
        """Record what the block draws, then commit it as one step.

        Nothing is applied until the block finishes, so an op that raises
        halfway leaves no half-drawn step behind.
        """
        self.begin_step(op, label=label, params=params)
        try:
            yield
        except BaseException:
            self.abort_step()
            raise
        self.commit_step()

    def replay_step(self, record: StepRecord) -> StepRecord:
        """Append another journal's step to this backend, drawing it now.

        This is cross-backend replay: the primitives are backend-neutral, so a
        step recorded on the dxf backend draws the same geometry here.
        """
        self.begin_step(record.op, label=record.label, params=record.params)
        self._pending = [dict(p) for p in record.primitives]
        return self.commit_step()

    # --- journal actions ---------------------------------------------------

    def undo(self, count: int = 1) -> dict[str, Any]:
        doomed = self.journal.undo(count)
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

    def redo(self, count: int = 1) -> dict[str, Any]:
        chosen = self.journal.redo(count)
        redrawn = 0
        for step in chosen:
            # Re-applying creates new entities, so the step's handles change;
            # without the update a second undo would aim at the dead ones.
            step.handles = self._apply(step.primitives)
            redrawn += len(step.handles)
        return {
            "redone": [{"index": s.index, "op": s.op, "label": s.label} for s in chosen],
            "redrawn": redrawn,
        }

    def history(self) -> str:
        return self.journal.describe()

    # --- subclass contract -------------------------------------------------

    def _apply(self, primitives: list[dict[str, Any]]) -> list[str]:
        raise NotImplementedError

    def _erase(self, handles: list[str]) -> tuple[int, int]:
        raise NotImplementedError

    # --- internals ---------------------------------------------------------

    def _record(self, kind: str, points: Sequence[Point], **rest: Any) -> int:
        if not self._op:
            raise BackendError(
                "this backend only draws inside a step; open one with "
                "backend.step(...) or begin_step(...)"
            )
        # None values are dropped: appliers supply their own defaults, and an
        # explicit null for, say, linetype would overwrite "CONTINUOUS".
        primitive: dict[str, Any] = {"kind": kind, "points": flatten_points(points)}
        primitive.update({k: v for k, v in rest.items() if v is not None})
        self._pending.append(primitive)
        return len(self._pending) - 1

    def _reset(self) -> None:
        self._op = ""
        self._label = None
        self._params = {}
        self._pending = []
```

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_recording.py tests/test_journal.py -q` — expect all PASS

- [ ] **Step 5: Lint** `.venv/Scripts/python.exe -m ruff check src/acadmcp/journal.py src/acadmcp/backends/recording.py tests/test_journal.py tests/test_recording.py --output-format concise` — expect clean

---

### Task 3: DxfBackend becomes a RecordingBackend (hybrid)

**Files:**
- Modify: `src/acadmcp/backends/dxf.py`
- Test: `tests/test_dxf_journal.py`

**Interfaces:**
- Consumes: `RecordingBackend` (Task 2), `Journal` (Task 1)
- Produces for Tasks 4-7: `DxfBackend.undo/redo/history/journal` (inherited), `_apply/_erase`, `_write_journal()`, `_read_journal()`, class attr `JOURNAL_DICT="ACADMCP"`, `JOURNAL_KEY="JOURNAL"`, `JOURNAL_CHUNK=1900`

**Pattern — hybrid record-or-draw.** Each public primitive method keeps its signature and gains one branch:

```python
def line(self, p1, p2, *, layer="0", linetype=None):
    if self.open_step:
        return self._record("line", [p1, p2], layer=layer, linetype=linetype)
    return self._draw_line(p1, p2, layer=layer, linetype=linetype)
```

Outside a step the backend draws eagerly exactly as today (existing tests, examples and scripts are untouched); inside a step it records and the commit draws. Rename today's method bodies to `_draw_line/_draw_polyline/_draw_circle/_draw_arc/_draw_text/_draw_mtext/_draw_hatch/_draw_linear_dimension` (keep `self._ops += 1` in them — it counts renders, and recorded steps render once at commit).

**Dispatch** (module level, keys must match plugin.py's `_record` kwargs exactly):

```python
def _apply_primitive(self, prim: dict[str, Any]) -> Any:
    pts = unflatten_points(prim.get("points", []))
    kind = prim["kind"]
    if kind == "line":
        return self._draw_line(
            pts[0], pts[1], layer=prim.get("layer", "0"), linetype=prim.get("linetype")
        )
    if kind == "polyline":
        return self._draw_polyline(
            pts,
            closed=bool(prim.get("closed", False)),
            layer=prim.get("layer", "0"),
            linetype=prim.get("linetype"),
        )
    if kind == "circle":
        return self._draw_circle(pts[0], float(prim["radius"]), layer=prim.get("layer", "0"))
    if kind == "arc":
        return self._draw_arc(
            pts[0],
            float(prim["radius"]),
            float(prim["start_angle"]),
            float(prim["end_angle"]),
            layer=prim.get("layer", "0"),
        )
    if kind == "text":
        return self._draw_text(
            pts[0],
            str(prim["text"]),
            float(prim["height"]),
            layer=prim.get("layer", "0"),
            rotation=float(prim.get("rotation", 0.0)),
            align=str(prim.get("align", "left")),
        )
    if kind == "mtext":
        return self._draw_mtext(
            pts[0],
            str(prim["text"]),
            float(prim["height"]),
            width=float(prim.get("width", 0.0)),
            layer=prim.get("layer", "0"),
            align=str(prim.get("align", "center")),
        )
    if kind == "hatch":
        return self._draw_hatch(
            pts,
            str(prim.get("pattern", "ANSI31")),
            float(prim.get("scale", 1.0)),
            layer=prim.get("layer", "0"),
            angle=float(prim.get("angle", 0.0)),
            solid=bool(prim.get("solid", False)),
        )
    if kind == "dimension":
        # text_height travels as 0.0 when unset; the drawer treats None as "default".
        height = float(prim.get("text_height") or 0.0) or None
        return self._draw_linear_dimension(
            pts[0],
            pts[1],
            float(prim.get("offset", 0.0)),
            layer=prim.get("layer", "0"),
            text_height=height,
        )
    raise BackendError(
        f"the 'dxf' backend cannot apply primitive kind {kind!r}: it cannot read a "
        "library .dwg to copy a block definition from. Use backend='plugin'."
    )
```

**Core:**

```python
def _apply(self, primitives):
    return [self._apply_primitive(p).dxf.handle for p in primitives]


def _erase(self, handles):
    erased = missing = 0
    for handle in handles:
        entity = self.doc.entitydb.get(handle)
        if entity is None or not entity.is_alive:
            missing += 1  # a hand edit already removed it; count, don't fail
            continue
        self.msp.delete_entity(entity)
        erased += 1
    return erased, missing
```

`__init__` gains `super().__init__()` (before opening the file) and, when `path` is given, `self._read_journal()` after `self.msp` is set (Task 4 adds the method; for Task 3 guard with `getattr` is NOT needed — implement Task 4's methods in the same edit; the tasks share this file and land together).

`info()` adds `"journal": {"steps": len(self.journal.steps), "applied": sum(s.state == "applied" for s in self.journal.steps)}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_dxf_journal.py`:

```python
"""The dxf backend journals stepped drawing; eager drawing is unchanged."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


@pytest.fixture()
def be():
    return make_backend("dxf")


def test_eager_drawing_outside_a_step_is_unchanged(be):
    be.line((0, 0), (1000, 0))
    assert be.info()["entities"] == 1
    assert be.journal.steps == []


def test_a_step_draws_at_commit_and_journals_handles(be):
    with be.step("draw_line", params={"p1": [0, 0]}):
        be.line((0, 0), (1000, 0), layer="A-WALL")
        assert be.info()["entities"] == 0  # nothing drawn until commit
    assert be.info()["entities"] == 1
    step = be.journal.steps[0]
    assert step.op == "draw_line" and len(step.handles) == 1


def test_undo_removes_exactly_the_step's_entities(be):
    be.line((0, 0), (1, 0))  # eager, not journaled
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    total = be.info()["entities"]
    report = be.undo(1)
    assert report["erased"] == 4  # two faces + two caps
    assert be.info()["entities"] == total - 4 == 1


def test_redo_redraws_and_a_second_undo_still_works(be):
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    be.undo(1)
    assert be.info()["entities"] == 0
    be.redo(1)
    assert be.info()["entities"] == 4
    report = be.undo(1)  # handles were refreshed by redo
    assert report["erased"] == 4 and report["missing"] == 0


def test_erase_tolerates_entities_deleted_elsewhere(be):
    with be.step("two lines"):
        be.line((0, 0), (1, 0))
        be.line((0, 1), (1, 1))
    victim = next(e for e in be.msp if e.dxf.handle == be.journal.steps[0].handles[0])
    be.msp.delete_entity(victim)
    report = be.undo(1)
    assert report["erased"] == 1 and report["missing"] == 1


def test_insert_inside_a_step_is_refused(be):
    from acadmcp.backends.base import BackendError

    with pytest.raises(BackendError), be.step("place"):
        be.insert("SOME_BLOCK", (0, 0))
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_dxf_journal.py -q` — expect FAIL (`open_step` unknown)

- [ ] **Step 3: Implement** the refactor as specced above (public methods gain the hybrid branch; bodies move to `_draw_*`; add `_apply_primitive/_apply/_erase`; `DxfBackend(RecordingBackend)`).

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_dxf_journal.py tests/test_backends.py tests/test_geom.py tests/test_journal.py tests/test_recording.py -q` — all PASS, including the pre-existing DXF tests unchanged

- [ ] **Step 5: Run the demo** `.venv/Scripts/python.exe examples/floor_plan_demo.py` — still produces DXF + PNG (eager path regression check)

---

### Task 4: DXF journal persistence in an XRecord

**Files:**
- Modify: `src/acadmcp/backends/dxf.py`
- Test: `tests/test_dxf_journal.py` (append)

**Interfaces:**
- Produces: journal survives `save()`/reopen; `DxfBackend.journal_unreadable: str` ("" when fine), surfaced in `info()`

- [ ] **Step 1: Failing tests** — append to `tests/test_dxf_journal.py`:

```python
def test_journal_survives_save_and_reopen(tmp_path):
    be = make_backend("dxf")
    with be.step("room"):
        ops.draw_wall_run(be, [(0, 0), (5000, 0)], 200)
    target = str(tmp_path / "plan.dxf")
    be.save(target)

    reopened = make_backend("dxf", path=target)
    assert [s.op for s in reopened.journal.steps] == ["room"]
    report = reopened.undo(1)  # handles still resolve after the round trip
    assert report["erased"] == 4 and reopened.info()["entities"] == 0


def test_a_journal_without_steps_writes_nothing(tmp_path):
    be = make_backend("dxf")
    be.line((0, 0), (1, 0))
    target = str(tmp_path / "plain.dxf")
    be.save(target)
    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps == [] and reopened.journal_unreadable == ""


def test_chinese_labels_round_trip(tmp_path):
    be = make_backend("dxf")
    with be.step("draw_room", label="生产车间", params={"名称": "车间"}):
        be.line((0, 0), (1, 0))
    target = str(tmp_path / "zh.dxf")
    be.save(target)
    reopened = make_backend("dxf", path=target)
    assert reopened.journal.steps[0].label == "生产车间"
    assert reopened.journal.steps[0].params == {"名称": "车间"}
```

- [ ] **Step 2: Run** — expect FAIL (`journal_unreadable` missing)

- [ ] **Step 3: Implement** in `dxf.py`:

```python
import json

from ezdxf.lldxf.tags import Tags
from ezdxf.lldxf.types import DXFTag

#: Where the journal lives inside the file: root dictionary → ACADMCP → JOURNAL.
#: Same idea as the plugin's DWG journal: the steps travel with the drawing, so
#: drawing_open of a saved file can still undo what a previous session drew.
JOURNAL_DICT = "ACADMCP"
JOURNAL_KEY = "JOURNAL"
#: A group-code-1 string tops out at 2049 bytes; JSON is escaped to ASCII first,
#: so chunk by characters with margin.
JOURNAL_CHUNK = 1900


def _write_journal(self) -> None:
    payload = json.dumps(self.journal.to_dict(), ensure_ascii=True)
    dictionary = self.doc.rootdict.get_required_dict(self.JOURNAL_DICT)
    xrecord = dictionary.get(self.JOURNAL_KEY) or dictionary.add_xrecord(self.JOURNAL_KEY)
    xrecord.tags = Tags(
        [
            DXFTag(1, payload[i : i + self.JOURNAL_CHUNK])
            for i in range(0, len(payload), self.JOURNAL_CHUNK)
        ]
    )


def _read_journal(self) -> None:
    self.journal_unreadable = ""
    dictionary = self.doc.rootdict.get(self.JOURNAL_DICT)
    xrecord = dictionary.get(self.JOURNAL_KEY) if dictionary else None
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
```

Wire-up: class constants on `DxfBackend`; `__init__` sets `self.journal_unreadable = ""` unconditionally and calls `self._read_journal()` when `path` was given; `save()` calls `self._write_journal()` before `self.doc.saveas(target)`; `info()` includes `"journal_unreadable": self.journal_unreadable` and the counts from Task 3. Imports: `from ..journal import Journal`, `from .recording import RecordingBackend, unflatten_points`.

Note: `dictionary.get(key)` on an ezdxf `Dictionary` returns the DXF entity or `None`; `add_xrecord` always appends, hence the get-or-add (never two JOURNAL records).

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_dxf_journal.py -q` — all PASS (9 tests)

- [ ] **Step 5: Round-trip sanity in AutoCAD terms** — run `.venv/Scripts/python.exe -c` snippet saving a journaled DXF, reopening with plain `ezdxf.readfile`, asserting the ACADMCP dictionary and chunk reassembly match `journal.to_dict()`.

---

### Task 5: PluginBackend on the shared layer + undo/redo delegation

**Files:**
- Modify: `src/acadmcp/backends/plugin.py`
- Test: `tests/test_plugin_backend.py` (new, fake client — pattern from `tests/test_plugin_client.py`)

**Interfaces:**
- Produces: `PluginBackend.undo(count=1) -> dict`, `.redo(count=1) -> dict`, `.history() -> str`, `.replay_step(record) -> dict`; keeps `begin_step/commit_step/plan_step/abort_step/step` semantics and the `StepResult` yield exactly as today

- [ ] **Step 1: Failing tests** with a fake client (note: the plugin's step indices are **1-based** — `Journal.cs` numbers them `count+1` and `rollback_to(0)` is the legal "wipe everything", so the undo formula `rollback_to(doomed[0].index - 1)` never goes negative):

```python
class FakeClient:
    def __init__(self):
        self.calls = []
        self.steps = []

    def connect(self):
        return self

    def ping(self):
        return {"pong": True, "queue": "attached"}

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "apply":
            step = dict(payload["step"], index=len(self.steps) + 1, state="applied")
            self.steps.append(step)
            return {"result": {"ok": True, "message": "", "step": step}}
        if method == "get_steps":
            return {"steps": list(self.steps)}
        if method == "rollback_to":
            for s in self.steps:
                if s["index"] > payload["index"] and s["state"] == "applied":
                    s["state"] = "planned"
            return {"result": {"ok": True, "message": "", "touched": []}}
        if method in (
            "run_next",
            "run_to",
            "ensure_layer",
            "set_units_mm",
            "status",
            "info",
            "entities",
        ):
            return {"result": {"ok": True}, "entities": [], "ok": True}
        raise AssertionError(f"unexpected call {method}")
```

Tests:
- `test_undo_rolls_back_to_before_the_last_applied_step`: two stepped draws → `be.undo(1)` → client saw `rollback_to` with `index == 1`, and the fake's step 2 is `planned`.
- `test_undo_of_the_first_step_targets_zero`: one stepped draw → `be.undo(1)` → `rollback_to` with `index == 0` (the wipe position — legal in the plugin).
- `test_redo_runs_the_plan_forward`: after that undo, `be.redo(1)` → client saw `run_to` with the planned step's index.
- `test_undo_with_nothing_applied_is_a_no_op`: fresh backend → `be.undo(1)` → no `rollback_to` call, result has empty `"undone"`.
- `test_history_lists_the_plugin's_steps`: after two draws, `"draw_room"` appears in `be.history()`.

- [ ] **Step 2: Run** — expect FAIL (`undo` missing)

- [ ] **Step 3: Implement** — `plugin.py` changes, nothing else moves:

1. `PluginBackend` subclasses `RecordingBackend`; `__init__` calls `super().__init__()`; delete its local `_points` helper and `_record`/`_reset`/`begin_step`/`abort_step` bodies in favour of the base's (identical code — verify by diff before deleting).
2. Keep `commit_step`/`plan_step`/`step` as they are (they return the plugin's outcome dict, not a StepRecord — the base's are not inherited blindly; add a comment saying why: the DWG journal is the source of truth, so a commit reports what the plugin recorded).
3. Add the delegation:

```python
def undo(self, count: int = 1) -> dict[str, Any]:
    """Roll the DWG journal back past the last `count` applied steps.

    The plugin owns this journal, so undo is its rollback, not a local erase:
    the palette and the MCP tools must never disagree about what is drawn.
    """
    steps = self.client.call("get_steps", offset=0, limit=10000)["steps"]
    applied = [s for s in steps if s["state"] == "applied"]
    if not applied:
        return {"undone": [], "erased": 0, "missing": 0}
    doomed = applied[-max(1, int(count)) :]
    response = self.client.call("rollback_to", index=doomed[0]["index"] - 1)
    return {
        "undone": [
            {"index": s["index"], "op": s["op"], "label": s.get("label", "")}
            for s in reversed(doomed)
        ],
        "response": response.get("result", {}),
    }


def redo(self, count: int = 1) -> dict[str, Any]:
    steps = self.client.call("get_steps", offset=0, limit=10000)["steps"]
    planned = [s for s in steps if s["state"] == "planned"]
    if not planned:
        return {"redone": []}
    chosen = planned[: max(1, int(count))]
    response = self.client.call("run_to", index=chosen[-1]["index"])
    return {
        "redone": [
            {"index": s["index"], "op": s["op"], "label": s.get("label", "")} for s in chosen
        ],
        "response": response.get("result", {}),
    }


def history(self) -> str:
    from ..steps import describe_steps

    return describe_steps(self.client.call("get_steps", offset=0, limit=10000)["steps"])


def replay_step(self, record: StepRecord) -> dict[str, Any]:
    """Draw another journal's step here, as a real plugin step."""
    self.begin_step(record.op, label=record.label, params=record.params)
    self._pending = [dict(p) for p in record.primitives]
    return self.commit_step()
```

Note the inherited `_record` raises outside a step — that is today's plugin behaviour, preserved on purpose.

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_plugin_backend.py tests/test_plugin_client.py tests/test_element_tools.py -q` — all PASS (the existing suites pin the wire behaviour)

- [ ] **Step 5: Full suite** `.venv/Scripts/python.exe -m pytest -q` — all PASS

---

### Task 6: server.py — auto-step every drawing tool + undo/redo/history tools

**Files:**
- Modify: `src/acadmcp/server.py`
- Test: `tests/test_server_journal.py` (new)

**Interfaces:**
- Consumes: `RecordingBackend.step/open_step/undo/redo/history` (Task 2), `PluginBackend.undo/redo/history` (Task 5)
- Produces: `_auto_step(op, params)` contextmanager; tools `drawing_undo(count=1)`, `drawing_redo(count=1)`, `drawing_history()`

- [ ] **Step 1: Failing tests** — `tests/test_server_journal.py`:

```python
"""Auto-stepping and the journal tools, against the headless dxf backend."""

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


def test_every_drawing_tool_call_is_one_step():
    server.draw_line([0, 0], [1000, 0], layer="A-WALL")
    server.draw_circle([500, 500], 100)
    steps = server._state.backend.journal.steps
    assert [s.op for s in steps] == ["draw_line", "draw_circle"]
    assert steps[0].params["layer"] == "A-WALL"


def test_drawing_undo_takes_back_only_the_last_call():
    server.draw_line([0, 0], [1000, 0])
    server.draw_circle([500, 500], 100)
    report = server.drawing_undo()
    assert report["ok"] and report["erased"] == 1
    assert server._state.backend.info()["entities"] == 1
    server.drawing_redo()
    assert server._state.backend.info()["entities"] == 2


def test_drawing_history_is_compact_text():
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="测试室")
    out = server.drawing_history()
    assert out["ok"] and "draw_room" in out["rendered"] and "测试室" in out["rendered"]


def test_a_wrong_coordinate_costs_two_calls_not_a_redraw():
    server.draw_wall([[0, 0], [5000, 0]], thickness=200)
    server.draw_wall([[0, 0], [0, 3000]], thickness=200)  # oops, wrong
    server.drawing_undo()
    server.draw_wall([[5000, 0], [5000, 3000]], thickness=200)
    assert [s.op for s in server._state.backend.journal.steps] == [
        "draw_wall",
        "draw_wall",
    ]
    assert server._state.backend.info()["entities"] == 8
```

Note: tests call the tool functions directly — `@mcp.tool()` leaves the function callable (verify; if the decorator returns a wrapper object instead, call `server.draw_line.fn(...)` or use whatever attribute the installed mcp version exposes — check with `python -c "from mcp.server.mcpserver import MCPServer"` first and adjust all four tests consistently).

- [ ] **Step 2: Run** — expect FAIL (`drawing_undo` missing)

- [ ] **Step 3: Implement** in `server.py`:

```python
import contextlib
from collections.abc import Iterator


@contextlib.contextmanager
def _auto_step(op: str, params: dict[str, Any]) -> Iterator[Backend]:
    """Wrap one tool call in a step, when the backend journals.

    An explicit step (step_begin) takes precedence: while one is open the
    drawing records into it, and no implicit commit happens. Backends without
    a journal are passed through untouched.
    """
    be = _state.require()
    stepper = getattr(be, "step", None)
    if stepper is None or getattr(be, "open_step", ""):
        yield be
        return
    with stepper(op, params=params):
        yield be
```

Wrap these 23 tools (mechanical: move the body into `with _auto_step("<tool_name>", {<normalized args>}) as be:`; drop the leading `be = _state.require()`):

`draw_line, draw_polyline, draw_rect, draw_circle, draw_arc, draw_wall, draw_wall_with_openings, draw_room, add_room_label, draw_partition, draw_door, draw_window, draw_column, draw_stair, draw_lift, draw_shaft, draw_equipment, draw_axis_grid, add_text, add_leader_note, add_dimension, add_dimension_chain, add_hatch`

Exemplar:

```python
@mcp.tool()
def draw_wall(points, thickness=layers.WALL_THICKNESS, closed=False, hatch=False, layer="A-WALL"):
    """<unchanged docstring>"""
    pts = _pts(points)
    params = {
        "points": pts,
        "thickness": float(thickness),
        "closed": closed,
        "hatch": hatch,
        "layer": layer,
    }
    with _auto_step("draw_wall", params) as be:
        ops.draw_wall_run(be, pts, float(thickness), closed=closed, hatch=hatch, layer=layer)
    return {
        "ok": True,
        "segments": len(pts) - 1,
        "thickness": float(thickness),
        "length": round(geom.polyline_length(pts), 3),
    }
```

Do NOT wrap: `layer_create*`, `set_text_style`, `drawing_*` management, `listing_entities`, `plugin_*`, `step_*`, `place_*` (they self-step via `_placed`), `library_list`, `element_types`.

New tools after `drawing_preview`:

```python
@mcp.tool()
def drawing_undo(count: int = 1) -> dict[str, Any]:
    """Take back the last `count` drawing steps, on any backend.

    A wrong coordinate costs this one call plus the redraw of the single step —
    not a redraw of the whole plan. On the plugin backend this is the drawing's
    own journal rolling back, so the palette and this call never disagree.
    """
    be = _state.require()
    undo = getattr(be, "undo", None)
    if undo is None:
        raise BackendError(f"backend {be.name!r} keeps no journal; nothing can be undone")
    return {"ok": True, **undo(int(count))}


@mcp.tool()
def drawing_redo(count: int = 1) -> dict[str, Any]:
    """Re-draw steps that drawing_undo took back (newest work first)."""
    be = _state.require()
    redo = getattr(be, "redo", None)
    if redo is None:
        raise BackendError(f"backend {be.name!r} keeps no journal; nothing can be redone")
    return {"ok": True, **redo(int(count))}


@mcp.tool()
def drawing_history() -> dict[str, Any]:
    """What has been drawn, one line per step — much cheaper than a screenshot.

    Read this to know the drawing's state before deciding what to undo or add;
    reach for drawing_screenshot only when the geometry itself needs eyeballing.
    """
    be = _state.require()
    history = getattr(be, "history", None)
    if history is None:
        raise BackendError(f"backend {be.name!r} keeps no journal")
    return {"ok": True, "backend": be.name, "rendered": history()}
```

Also update `drawing_help()`'s `workflow` list: replace the `plugin_connect() + step_begin/step_commit ...` line with `"just draw — every tool call is one undoable step; drawing_undo/drawing_redo/drawing_history manage the journal"`, and add `"drawing_replay(backend='plugin') to re-draw a headless session into AutoCAD"` before the preview line (Task 7 implements it; add the line in Task 7, not here).

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_server_journal.py -q` — PASS

- [ ] **Step 5: Full suite + lint** `.venv/Scripts/python.exe -m pytest -q` and `.venv/Scripts/python.exe -m ruff check . --output-format concise` — all clean

---

### Task 7: `drawing_replay` — the journal is a portable script

**Files:**
- Modify: `src/acadmcp/server.py`
- Test: `tests/test_server_journal.py` (append)

- [ ] **Step 1: Failing tests**:

```python
def test_replay_redraws_the_journal_on_a_fresh_dxf_backend(tmp_path):
    server.drawing_new(backend="dxf")
    server.draw_line([0, 0], [1000, 0])
    server.draw_room([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="测试室")
    target = str(tmp_path / "replayed.dxf")
    report = server.drawing_replay(backend="dxf", path=target)
    assert report["ok"] and report["replayed"] == 2
    assert server._state.backend.info()["entities"] > 0
    assert [s.op for s in server._state.backend.journal.steps] == ["draw_line", "draw_room"]


def test_replay_reports_steps_a_backend_cannot_apply(tmp_path):
    server.drawing_new(backend="dxf")
    be = server._state.backend
    with be.step("place", params={}):
        be._pending.append({"kind": "insert", "points": [0.0, 0.0], "block": "X"})
        # commit would refuse inserts on dxf, so journal the record directly:
        be.journal.append("place", "", {}, list(be._pending), [])
        be._reset()
    report = server.drawing_replay(backend="dxf", path=str(tmp_path / "r.dxf"))
    assert report["ok"] and len(report["failed"]) == 1
    assert report["failed"][0]["op"] == "place" and "insert" in report["failed"][0]["reason"]
```

- [ ] **Step 2: Run** — expect FAIL

- [ ] **Step 3: Implement** in `server.py`:

```python
@mcp.tool()
def drawing_replay(backend: str = "plugin", path: str | None = None) -> dict[str, Any]:
    """Re-draw the current journal on another backend, step by step.

    The journal is backend-neutral, so a plan drawn headless (fast, previewable)
    replays into live AutoCAD in one call — switching backends no longer means
    redrawing. Each replayed step lands as a real step on the target, so it
    stays undoable there. Steps the target cannot apply (a library insert onto
    the dxf backend) are skipped and reported, never silently dropped.
    """
    source = _state.require()
    journal = getattr(source, "journal", None)
    if journal is None:
        raise BackendError(
            f"backend {source.name!r} keeps no local journal to replay; "
            "the plugin backend's steps live in the DWG — drive them with step_control"
        )
    applicable = [s for s in journal.steps if s.state == "applied"]

    kind = backend.strip().lower()
    if kind == "plugin":
        target = _plugin_backend_connect(new_document=True)
    elif kind == "dxf":
        if not path:
            raise BackendError("drawing_replay(backend='dxf') needs a path to draw into")
        target = make_backend("dxf")
        target.path = path
    else:
        raise BackendError(f"cannot replay onto backend {backend!r}; expected 'plugin' or 'dxf'")

    ops.setup_layers(target)
    failed: list[dict[str, Any]] = []
    for record in applicable:
        try:
            target.replay_step(record)
        except BackendError as exc:
            failed.append({"index": record.index, "op": record.op, "reason": str(exc)})
    if kind == "dxf":
        target.save(path)
    _state.reset(target, kind)
    return {
        "ok": True,
        "backend": kind,
        "replayed": len(applicable) - len(failed),
        "failed": failed,
        **({"path": path} if kind == "dxf" else {}),
    }
```

Update `drawing_help()` workflow: add the replay line (see Task 6 note).

- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_server_journal.py -q` — PASS (6 tests)

- [ ] **Step 5: Full suite + lint** — all clean

---

### Task 8: ComBackend joins the journal (in-memory)

**Files:**
- Modify: `src/acadmcp/backends/com.py`
- Test: no new tests (rule: tests must not need AutoCAD); the refactor is the same mechanical hybrid as Task 3, verified by the unchanged suite still passing and by `tests/test_com_backend.py`'s live check when AutoCAD is available (never in CI).

- [ ] **Step 1: Refactor** — same hybrid pattern as `dxf.py`:
  - `ComBackend(RecordingBackend)`, `super().__init__()` in `__init__`.
  - Public primitives (`line/polyline/circle/arc/text/mtext/hatch/linear_dimension`) gain the `if self.open_step: return self._record(...)` branch with plugin-format kwargs; bodies move to `_draw_*`.
  - `_apply_primitive` dispatch mirroring Task 3's table, returning `str(entity.Handle)` per entity (COM `.Handle` must be read through `_call`: `handle = _call(lambda: entity.Handle)` — attribute rule).
  - `_apply` collects handles; `_erase`:

```python
def _erase(self, handles):
    erased = missing = 0
    for handle in handles:
        try:
            obj = _call(lambda h=handle: self.doc.HandleToObject(h))
            _call(lambda o=obj: o.Delete())
            erased += 1
        except Exception:  # noqa: BLE001 - a hand-deleted entity is counted, not fatal
            missing += 1
    return erased, missing
```

  - `insert` on com stays refused as today (its `_apply_primitive` raises the same BackendError the method raises now).
  - `info()` adds the same two-key `"journal"` summary as Task 3.

- [ ] **Step 2: Full suite + lint** — all clean; `git diff` shows no behaviour change outside the hybrid branch

---

### Task 9: Docs + final verification

**Files:**
- Modify: `AGENTS.md` (Architecture rules + one Gotcha), `README.md` (backend comparison + tools list)

- [ ] **Step 1: AGENTS.md** — update the layout block (`journal.py`, `backends/recording.py`), the Architecture rule "`server.py` is thin" (add: every drawing tool is auto-stepped; the journal is how a mistake costs one undo instead of a redraw), and add a Gotcha:

```markdown
**Every drawing tool call is one journal step.** `_auto_step` in `server.py` wraps
each `draw_*` tool; an explicit `step_begin` overrides it. Undo/redo are
backend-neutral (`drawing_undo`/`drawing_redo`); on the plugin backend they
delegate to the DWG journal so the palette never disagrees. The dxf journal
persists inside the file (`ACADMCP` dictionary, XRecord, JSON chunked at 1900
chars, ASCII-escaped) and survives `drawing_open`. Handles change on redo —
`RecordingBackend.redo` stores the fresh ones, so never cache a handle across a
redo. `drawing_replay` re-draws the applied journal onto another backend.
```

- [ ] **Step 2: README.md** — backend table gains the journal column; tools list gains the four new tools.

- [ ] **Step 3: Full verification** — `.venv/Scripts/python.exe -m pytest -q` (expect 130 + new ≈ 160), `.venv/Scripts/python.exe -m ruff check . --output-format concise`, `.venv/Scripts/python.exe examples/floor_plan_demo.py` (DXF + PNG still produced).

## Execution Waves (subagents)

1. **Wave 1** (one agent): Tasks 1–2 — the foundation everything pins against.
2. **Wave 2** (three parallel agents): Task 3+4 (dxf.py) ∥ Task 5 (plugin.py) ∥ Task 6+7 (server.py). No shared files between agents. Task 6's tests use the real dxf backend — if Task 3 hasn't landed when an agent runs, that agent must still write against the Task 2 interfaces; final integration happens in Wave 3.
3. **Wave 3** (one agent): Task 8 + Task 9 + full-suite green run.
