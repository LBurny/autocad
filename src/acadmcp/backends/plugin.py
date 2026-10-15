"""Plugin backend: draws through the in-AutoCAD step plugin.

Where the COM backend issues one ActiveX call per entity, this one does not draw
as it goes. Each primitive call appends to a pending batch, and the whole batch is
sent as **one step**: the plugin creates it in a single transaction, records the
resulting handles, and writes the step into the drawing's own journal, so it can
later be replayed or rolled back.

Why batching matters: a step is the unit the user reviews, reverts and re-runs. If
every line were its own step, "take back the room I just drew" would mean finding
four of them and hoping nothing in between was wanted.

The plugin also owns the journal, which is what makes the panel usable with the MCP
server stopped: the steps live in the DWG, not in this process.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from ..acad_plugin_client import PluginClient
from ..geom import Point
from ..journal import StepRecord
from .base import BackendError
from .recording import RecordingBackend


class StepResult:
    """What the plugin reported about one committed step.

    Filled in by :meth:`PluginBackend.step`, which is the only thing that sets
    :attr:`outcome`.
    """

    def __init__(self, op: str) -> None:
        self.op = op
        self.outcome: dict[str, Any] = {}

    @property
    def ok(self) -> bool:
        return bool(self.outcome.get("result", {}).get("ok"))

    @property
    def message(self) -> str:
        return str(self.outcome.get("result", {}).get("message", ""))

    @property
    def step(self) -> dict[str, Any]:
        return self.outcome.get("result", {}).get("step", {})

    def __repr__(self) -> str:
        return f"StepResult(op={self.op!r}, ok={self.ok}, message={self.message!r})"


class PluginBackend(RecordingBackend):
    """A :class:`~acadmcp.backends.base.Backend` that records primitives per step."""

    name = "plugin"

    #: The one backend that draws library blocks: the plugin has AutoCAD's own block table
    #: to copy a definition into, so a catalogue entry with a `block` reaches the paper here
    #: as the standard's own block rather than as its parametric twin.
    supports_insert = True

    def __init__(
        self,
        client: PluginClient | None = None,
        port: int | None = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__()
        self.client = client if client is not None else PluginClient(port=port, timeout=timeout)
        self._layers_seen: set[str] = set()
        self.path: str | None = None
        self.units_set = False

    # --- connection --------------------------------------------------------

    def connect(self) -> PluginBackend:
        """Open the connection, or raise PluginUnavailable."""
        self.client.connect()
        return self

    def ping(self) -> dict[str, Any]:
        """Ask the plugin to identify itself. Works while AutoCAD is busy."""
        return self.client.ping()

    def close(self) -> None:
        self.client.close()

    # --- document ----------------------------------------------------------

    def ensure_layer(
        self, name: str, color: int | None = None, linetype: str | None = None
    ) -> dict[str, Any]:
        # Cached on this side as well: ops call this once per layer per op, and the
        # answer cannot change within one drawing session. Call forget_layers()
        # when the active drawing changes, or the cache describes the wrong file.
        if name in self._layers_seen:
            return {"layer": name, "created": False}
        result = self.client.call("ensure_layer", layer=name, color=color, linetype=linetype)
        self._layers_seen.add(name)
        return result

    def forget_layers(self) -> None:
        """Drop the layer cache. Needed after the active drawing changes."""
        self._layers_seen.clear()

    def set_units_mm(self) -> None:
        if not self.units_set:
            self.client.call("set_units_mm")
            self.units_set = True

    def save(self, path: str | None = None) -> str:
        raise BackendError(
            "the plugin backend draws into the drawing that is open in AutoCAD, and "
            "saving it is the user's call — use AutoCAD's own save, or the dxf backend"
        )

    def preview(self, path: str, theme: str = "dark") -> str | None:
        raise BackendError(
            "the plugin backend renders nothing off-screen; use screenshot for the "
            "live view, or the dxf backend for a PNG of the plan"
        )

    def screenshot(
        self,
        path: str,
        *,
        window: Sequence[float] | None = None,
        width: int = 1600,
        height: int = 1200,
        theme: str = "dark",
    ) -> str:
        """Capture the live AutoCAD window.

        `width`, `height` and `theme` do not apply: a window capture is the shape
        the window is. `window` is refused rather than ignored — silently handing
        back a differently framed image than the one asked for is worse than
        saying it cannot be done.

        A minimised window is reported, not restored: bringing AutoCAD to the
        front would cover whatever the user is working in. Unlike the COM backend
        this one talks over the plugin's socket and has no way to ask AutoCAD for
        a rendered drawing yet, so the window has to be up.
        """
        if window:
            raise BackendError(
                "the plugin backend cannot reframe the view yet; zoom in AutoCAD "
                "first, or use the dxf backend, which renders any window"
            )
        from ..capture import WindowUnavailable, capture_window, find_autocad_window

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            return capture_window(find_autocad_window(), path)
        except WindowUnavailable as exc:
            raise BackendError(
                f"{exc}. The plugin backend cannot export the drawing itself; "
                "restore the AutoCAD window, or use acad_connect(mode='com') or the "
                "dxf backend, which both take a picture without it"
            ) from exc

    def info(self) -> dict[str, Any]:
        drawing = self.client.call("info")
        status = self.client.call("status")
        return {**drawing, "journal": {k: v for k, v in status.items() if k != "unrecoverable"}}

    def zoom_extents(self) -> dict[str, Any]:
        """Frame the whole drawing. Synchronous, so a screenshot taken next shows it."""
        return self.client.call("zoom_extents")

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        reply = self.client.call("entities", layer=layer, limit=limit)
        if "entities" not in reply:
            # A KeyError here would surface to the caller as a reasonless
            # "Error executing tool listing_entities"; say what the plugin
            # actually answered instead.
            raise BackendError(f"plugin answered 'entities' without a list: {reply!r}")
        return reply["entities"]

    # --- primitives: recorded, not drawn ------------------------------------

    def line(self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None) -> int:
        return self._record("line", [p1, p2], layer=layer, linetype=linetype)

    def polyline(
        self,
        points: Sequence[Point],
        *,
        closed: bool = False,
        layer: str = "0",
        linetype: str | None = None,
    ) -> int:
        return self._record("polyline", points, layer=layer, closed=closed, linetype=linetype)

    def circle(self, center: Point, radius: float, *, layer: str = "0") -> int:
        return self._record("circle", [center], layer=layer, radius=radius)

    def arc(
        self,
        center: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        *,
        layer: str = "0",
    ) -> int:
        return self._record(
            "arc",
            [center],
            layer=layer,
            radius=radius,
            start_angle=start_deg,
            end_angle=end_deg,
        )

    def text(
        self,
        pos: Point,
        content: str,
        height: float,
        *,
        layer: str = "0",
        rotation: float = 0.0,
        align: str = "left",
    ) -> int:
        return self._record(
            "text", [pos], layer=layer, text=content, height=height, rotation=rotation, align=align
        )

    def mtext(
        self,
        pos: Point,
        content: str,
        height: float,
        width: float = 0.0,
        *,
        layer: str = "0",
        align: str = "center",
    ) -> int:
        return self._record(
            "mtext", [pos], layer=layer, text=content, height=height, width=width, align=align
        )

    def hatch(
        self,
        points: Sequence[Point],
        pattern: str = "ANSI31",
        scale: float = 1.0,
        *,
        layer: str = "0",
        angle: float = 0.0,
        solid: bool = False,
    ) -> int:
        return self._record(
            "hatch",
            points,
            layer=layer,
            pattern=pattern,
            scale=scale,
            angle=angle,
            solid=solid,
        )

    def linear_dimension(
        self,
        p1: Point,
        p2: Point,
        offset: float,
        *,
        layer: str = "0",
        text_height: float | None = None,
    ) -> int:
        return self._record(
            "dimension",
            [p1, p2],
            layer=layer,
            offset=offset,
            text_height=text_height or 0.0,
        )

    def insert(
        self,
        block: str,
        at: Point,
        *,
        source: str | None = None,
        rotation: float = 0.0,
        scale: float = 1.0,
        scale_x: float | None = None,
        scale_y: float | None = None,
        properties: dict[str, Any] | None = None,
        layer: str = "0",
    ) -> int:
        """Place a block reference, importing the definition if the drawing has not got it.

        `properties` are the dynamic block's parameters — the size a beam or a door is
        placed at. The plugin refuses a property the block has not got rather than ignoring
        it, because a wrong size that looks right is worse than an error: see
        `PrimitiveBuilder.FinishInsert`.
        """
        return self._record(
            "insert",
            [at],
            layer=layer,
            block=block,
            source=source,
            rotation=rotation,
            # One scale for both axes unless they are given separately, so the common case
            # stays a single number. Both always travel: the plugin would otherwise have to
            # guess whether a missing one means 1.0.
            scale_x=scale if scale_x is None else scale_x,
            scale_y=scale if scale_y is None else scale_y,
            properties=properties,
        )

    # --- steps -------------------------------------------------------------
    #
    # begin_step/abort_step/_record/_pending/open_step come from
    # RecordingBackend — the same code, byte for byte, before it was extracted.
    #
    # commit_step and plan_step below deliberately do *not* call the base's
    # commit_step: for the plugin the DWG journal is the source of truth, so a
    # commit reports what the plugin recorded rather than appending to a local
    # Journal. That is why undo/redo/history below delegate instead of inheriting.

    def commit_step(self) -> dict[str, Any]:
        """Send the recorded batch to the plugin as one step.

        Returns the plugin's own outcome dict, not a
        :class:`~acadmcp.journal.StepRecord`: the step now lives in the drawing.
        """
        if not self._op:
            raise BackendError("no step is open; call begin_step first")
        if not self._pending:
            raise BackendError(
                f"step {self._op!r} recorded no primitives; the plugin would record an "
                "empty step, which can never be rolled back meaningfully"
            )

        payload = {
            "op": self._op,
            "label": self._label or "",
            "params": self._params,
            "primitives": self._pending,
        }
        self._reset()
        return self.client.call("apply", step=payload)

    def plan_step(self) -> dict[str, Any]:
        """Send the recorded batch as a step that is planned, not drawn.

        This is what lets a whole run of steps be laid out first and drawn one at a
        time afterwards: the geometry is resolved here, and the plugin holds it
        until the user or the AI asks for it.
        """
        if not self._op:
            raise BackendError("no step is open; call begin_step first")
        if not self._pending:
            raise BackendError(
                f"step {self._op!r} recorded no primitives; there would be nothing to plan"
            )

        record = {
            "op": self._op,
            "label": self._label or "",
            "params": self._params,
            "primitives": self._pending,
        }
        self._reset()
        return self.client.call("plan", steps=[record])

    @contextlib.contextmanager
    def step(
        self, op: str, label: str | None = None, params: dict[str, Any] | None = None
    ) -> Iterator[StepResult]:
        """Record the primitives drawn inside the block, then commit them.

        The plugin is not touched until the block finishes, so an op that raises
        halfway leaves no half-drawn step behind::

            with backend.step("draw_room", params=arguments) as step:
                info = ops.draw_room(backend, boundary, name=..., thickness=...)
            # step.outcome holds the plugin's report
        """
        self.begin_step(op, label=label, params=params)
        result = StepResult(op)
        try:
            yield result
        except BaseException:
            self.abort_step()
            raise
        result.outcome = self.commit_step()

    # --- journal actions: delegated to the drawing's own journal -----------

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
        # The plugin's steps are 1-based and index 0 means "before the first
        # step", so rolling back to one less than the oldest doomed step leaves
        # everything before it standing, and undoing step 1 targets 0 — the
        # legal wipe rather than a negative index.
        response = self.client.call("rollback_to", index=doomed[0]["index"] - 1)
        return {
            "undone": [
                {"index": s["index"], "op": s["op"], "label": s.get("label", "")}
                for s in reversed(doomed)
            ],
            "response": response.get("result", {}),
        }

    def redo(self, count: int = 1) -> dict[str, Any]:
        """Run the planned tail of the journal forward, oldest first."""
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
        """The drawing's steps, one line each, read back from the plugin."""
        from ..steps import describe_steps

        return describe_steps(self.step_rows())

    def step_rows(self) -> list[dict[str, Any]]:
        """The DWG's steps as rows, in the shape the local journal reports too.

        The plugin owns this journal, so the rows come over RPC rather than out of the
        in-memory `Journal` this backend also carries (which the plugin path never uses).
        """
        return self.client.call("get_steps", offset=0, limit=10000)["steps"]

    def rollback_to(self, index: int) -> dict[str, Any]:
        """Roll the DWG journal back to `index`, delegating to the plugin.

        The plugin owns the journal, so this is its rollback rather than a local erase —
        the palette and this call must never disagree about what is drawn. An *accepted*
        step standing in the way is refused by the plugin; the response is returned as it
        came so the refusal is reported rather than swallowed (`step_control(force=True)`
        overrides it).
        """
        from ..steps import StepSession

        steps = self.step_rows()
        doomed = [
            s
            for s in steps
            if str(s.get("state", "")) == "applied" and int(s.get("index", 0)) > int(index)
        ]
        response = StepSession(self.client).rollback_to(int(index))
        result = response.get("result", {}) if isinstance(response, dict) else {}
        return {
            "undone": [
                {"index": s.get("index"), "op": s.get("op"), "label": s.get("label", "")}
                for s in reversed(doomed)
            ],
            "response": result,
        }

    def replay_step(self, record: StepRecord) -> dict[str, Any]:
        """Draw another journal's step here, as a real plugin step.

        Returns the plugin's outcome dict, like :meth:`commit_step` — this backend
        journals in the DWG, not in memory, so there is no StepRecord to hand back.
        """
        self.begin_step(record.op, label=record.label, params=record.params)
        self._pending = [dict(p) for p in record.primitives]
        return self.commit_step()
