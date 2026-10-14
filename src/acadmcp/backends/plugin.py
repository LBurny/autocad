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
from ..geom import Point, xy
from .base import BackendError


def _points(points: Sequence[Point]) -> list[float]:
    """Flatten to the x, y, x, y… wire form the plugin reads."""
    flat: list[float] = []
    for point in points:
        x, y = xy(point)
        flat.extend((x, y))
    return flat


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


class PluginBackend:
    """A :class:`~acadmcp.backends.base.Backend` that records primitives per step."""

    name = "plugin"

    def __init__(
        self,
        client: PluginClient | None = None,
        port: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.client = client if client is not None else PluginClient(port=port, timeout=timeout)
        self._pending: list[dict[str, Any]] = []
        self._op = ""
        self._label: str | None = None
        self._params: dict[str, Any] = {}
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

        Zooming to a window is left to AutoCAD for now; a framing API that has not
        been verified here would be a guess.
        """
        if window:
            raise BackendError(
                "the plugin backend cannot reframe the view yet; zoom in AutoCAD "
                "first, or use the dxf backend, which renders any window"
            )
        from ..capture import capture_window, find_autocad_window

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return capture_window(find_autocad_window(), path)

    def info(self) -> dict[str, Any]:
        drawing = self.client.call("info")
        status = self.client.call("status")
        return {**drawing, "journal": {k: v for k, v in status.items() if k != "unrecoverable"}}

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        return self.client.call("entities", layer=layer, limit=limit)["entities"]

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

    # --- steps -------------------------------------------------------------

    @property
    def pending(self) -> list[dict[str, Any]]:
        """The primitives recorded since the step began."""
        return self._pending

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

    def commit_step(self) -> dict[str, Any]:
        """Send the recorded batch to the plugin as one step."""
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

    def abort_step(self) -> None:
        """Throw away the recorded batch without drawing anything."""
        self._reset()

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

    # --- internals ---------------------------------------------------------

    def _record(self, kind: str, points: Sequence[Point], **rest: Any) -> int:
        if not self._op:
            raise BackendError(
                "this backend only draws inside a step; open one with "
                "backend.step(...) or begin_step(...)"
            )
        # None values are dropped: the plugin's reader supplies its own defaults, and
        # sending an explicit null for, say, linetype would overwrite "CONTINUOUS".
        primitive: dict[str, Any] = {"kind": kind, "points": _points(points)}
        primitive.update({k: v for k, v in rest.items() if v is not None})
        self._pending.append(primitive)
        return len(self._pending) - 1

    def _reset(self) -> None:
        self._op = ""
        self._label = None
        self._params = {}
        self._pending = []
