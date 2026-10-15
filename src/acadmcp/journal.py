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
        # The op is always shown — it is what was drawn; a label is optional
        # human decoration on top of it, so the pair reads "draw_room 车间".
        return "\n".join(
            f"{s.index:>3}  {s.state:<8} {f'{s.op} {s.label}'.strip()} "
            f"({len(s.handles)} entities)"
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
