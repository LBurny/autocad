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
from typing import Any, NoReturn

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

    #: No backend here draws library blocks unless it says so. `dxf` has no library to copy
    #: a definition from and `com` refuses, so a catalogue entry with no parametric twin
    #: cannot reach either of them — which the backend has to be able to say before anything
    #: is drawn, because inside a step the refusal is deferred to the commit.
    supports_insert = False

    #: No backend here writes paperspace unless it says so. A sheet is a page setup on a layout
    #: entity plus a frame drawn at 1:1 in paper millimetres, and only the dxf driver writes
    #: that; the live drivers refuse by name rather than draw half a sheet. Declared for the
    #: same reason `supports_insert` is: the refusal inside a step is deferred to the commit.
    supports_sheet = False

    def __init__(self) -> None:
        self.journal = Journal()
        self._op = ""
        self._label: str | None = None
        self._params: dict[str, Any] = {}
        self._pending: list[dict[str, Any]] = []

    # --- sheets ------------------------------------------------------------
    def _no_sheet(self, what: str) -> NoReturn:
        """Refuse a paperspace request by name, so the caller knows what to use instead."""
        raise BackendError(
            f"backend {self.name!r} cannot {what}: a sheet needs a paperspace layout, and only "
            "the dxf driver writes one. Use drawing_new(backend='dxf') for a sheet, or set the "
            "page setup in AutoCAD itself"
        )

    def set_page_setup(
        self, paper: Any, scale: tuple[int, int], *, layout: str = "", style_sheet: str = ""
    ) -> dict[str, Any]:
        self._no_sheet("set a page setup")

    def paper_primitives(self, primitives: list[dict[str, Any]]) -> int:
        self._no_sheet("draw into paperspace")

    def plan_extent(self) -> tuple[float, float, float, float] | None:
        """(min_x, min_y, max_x, max_y) of what the model holds, or None.

        A read-back a sheet needs to answer "does the plan fit the page at this scale", which is
        the one thing a caller cannot work out from the sheet's own reply. `None` is the honest
        answer for a driver that cannot measure its model without a round trip it has not got.
        """
        return None

    def plot_pdf(
        self, path: str, *, paper: Any, scale: tuple[int, int], theme: str = "light"
    ) -> str:
        self._no_sheet("plot a sheet")

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
        # A failed _apply must not wedge the backend: without the reset the
        # half-committed step stays "open" and every later begin_step raises.
        # Entities already drawn by the partial apply stay unjournaled — the
        # same state eager drawing would have left.
        try:
            handles = self._apply(list(self._pending))
        except BaseException:
            self._reset()
            raise
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

    def rollback_to(self, index: int) -> dict[str, Any]:
        """Erase everything drawn after step `index`, newest first.

        `drawing_undo(count)` makes a caller count steps; this lets it name the step it
        wants standing instead. Index 0 is before the first step, which is what empties a
        drawing. The handles are the step's own, so this erases exactly what each step
        drew — no more.
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

    def history(self) -> str:
        return self.journal.describe()

    def step_rows(self) -> list[dict[str, Any]]:
        """The journal as rows, the shape the plugin reports its steps in.

        A row carries what a read-back needs and nothing it cannot know: the step's own
        number, the op and arguments that produced it, whether it is applied, and how many
        entities it drew. `summary.py` reads exactly this shape from any backend.
        """
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

    # --- the catalogue that drew it ----------------------------------------

    def note_catalog(self, loaded: Any) -> None:
        """Record which catalogue this drawing is being drawn with.

        Stamped when the drawing starts and again on save, so the drawing says what drew it
        even if the catalogue file is edited afterwards. The identity carries the *entries*,
        not just a path: a drawing reopened on another machine has to be able to resolve an
        element the same way, which a file path cannot promise. The library root rides along
        as a plain path in the payload — not as a `{library}` token — because the copy in
        the drawing has to be usable as it stands where it is opened.
        """
        # Imported here rather than at module scope: `catalog` and `settings` reach back into
        # the package, and a module-level import would make importing a backend pull the whole
        # catalogue chain in.
        from .. import catalog as _catalog
        from .. import settings as _settings

        loaded = loaded if loaded is not None else _catalog.current()
        self.journal.note_meta(catalog=loaded.identity(_settings.current().library))

    def drawing_catalog(self) -> dict[str, Any]:
        """What the drawing recorded, and whether the catalogue loaded now agrees.

        A disagreement is *reported*, never acted on: the local standard is the authority,
        and silently redrawing somebody else's doors with this office's blocks is the failure
        this whole mechanism exists to prevent. The `warning` key is what turns a silent
        wrong answer into a question.
        """
        from .. import catalog as _catalog

        recorded = self.journal.meta.get("catalog")
        loaded = _catalog.current()
        out: dict[str, Any] = {
            "catalog": recorded,
            "loaded": {"count": len(loaded.elements), "digest": loaded.digest()},
        }
        if isinstance(recorded, dict) and recorded.get("digest") != out["loaded"]["digest"]:
            out["warning"] = (
                "this drawing was drawn with a different catalogue ("
                f"{recorded.get('count')} elements, {recorded.get('library') or 'no library'}); "
                "the one loaded now wins, but the elements may not be the ones it was drawn with"
            )
        return out

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
