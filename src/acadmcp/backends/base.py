"""The interface every drawing backend implements.

The contract is deliberately small: a backend renders primitives, it never
computes architecture. All of that lives in :mod:`acadmcp.geom` and
:mod:`acadmcp.ops`, so both drivers produce byte-for-byte equivalent geometry.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from ..geom import Point

try:  # mcp 2.x renamed the package path; the class is the same.
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError as _ToolError


class BackendError(_ToolError, RuntimeError):
    """Raised when a backend cannot carry out a drawing request.

    Subclassing the SDK's ToolError keeps the message visible to the client:
    the SDK masks any other exception behind a bare "Error executing tool
    <name>", and for an AI caller the message is the recovery signal.
    """


@runtime_checkable
class Backend(Protocol):
    name: str

    #: Whether this backend can place a library block reference at all. Declared rather than
    #: probed: inside a step the `dxf` and `com` refusals are deferred to the commit, so a
    #: caller cannot find out by trying, and which half of a catalogue entry to draw has to
    #: be decided before anything is drawn.
    supports_insert: bool

    #: Whether this backend can put a sheet on the drawing: a named paper, a plot scale, a frame
    #: and a title block on a paperspace layout. Declared for the same reason `supports_insert`
    #: is — the refusal inside a step is deferred to the commit. The dxf driver writes the page
    #: setup onto the file's own LAYOUT entity, which is where AutoCAD reads it, so a DXF saved
    #: here opens with the sheet already configured. The live drivers have no paperspace path
    #: and say so.
    supports_sheet: bool

    #: Whether this backend can create a block definition from primitives. Declared rather than
    #: probed for the same reason the other two are: which half of a capture can happen has to be
    #: known before anything is drawn. Only the dxf driver implements it this wave; the live
    #: drivers refuse by name.
    supports_block_create: bool

    # --- layers & document -------------------------------------------------
    def ensure_layer(
        self,
        name: str,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
    ) -> Any:
        """Create the named layer, or leave the existing one alone — safe to repeat.

        `lineweight` is a width in **millimetres** — the GB/T line-width groups a layer
        standard is half made of — and it is optional, because a layer with no width set draws
        ByLayer, which is what every layer this package drew before the standard became data.
        It goes on the wire and into the file in AutoCAD's own 1/100 mm unit, through
        :func:`acadmcp.standards.lineweight_centimm`, so the three drivers cannot disagree
        about what 0.7 mm is.
        """
        ...

    def set_units_mm(self) -> None: ...

    def set_page_setup(
        self, paper: Any, scale: tuple[int, int], *, layout: str = "", style_sheet: str = ""
    ) -> dict[str, Any]:
        """Name a layout for this sheet and give it a paper, a plot scale and a style sheet.

        `paper` is a :class:`acadmcp.sheet.Paper` and `scale` a ratio as
        :func:`acadmcp.sheet.resolve_scale` returns it. Returns the sheet as a reportable dict:
        `paper`, `orientation`, `width`, `height`, `scale`, `layout`, `style_sheet`.
        """
        ...

    def paper_primitives(self, primitives: list[dict[str, Any]]) -> int:
        """Record primitives as paperspace content of the open step, and return how many.

        The same vocabulary :meth:`_apply` takes, so `ops` builds the frame and the title block
        the way it builds everything else and only the destination differs — a primitive marked
        `paper` is drawn on the sheet's layout at 1:1 in paper millimetres instead of into the
        model. Counts rather than handles because a step defers its drawing to the commit, so
        the handles do not exist yet; the commit's own `_apply` returns them like any other
        step's, which is what makes a sheet one undoable step.
        """
        ...

    def plan_extent(self) -> tuple[float, float, float, float] | None:
        """``(min_x, min_y, max_x, max_y)`` of what the model holds, or None when it cannot say.

        A read-back a sheet needs, to answer the one question a caller cannot work out from the
        sheet's own reply: does the whole plan fit the page at this scale. A driver that cannot
        measure its model without a round trip it has not got answers None rather than guessing.
        """
        ...

    def plot_pdf(
        self, path: str, *, paper: Any, scale: tuple[int, int], theme: str = "light"
    ) -> str:
        """Render the sheet — frame, title block, and the plan at the plot scale — to a PDF.

        A PDF rather than a PNG because a PDF carries its own paper size, so what was plotted
        can be measured against what was asked for instead of trusted.
        """
        ...

    def save(self, path: str | None = None) -> str: ...

    def preview(self, path: str, theme: str = "dark") -> str | None: ...

    def screenshot(
        self,
        path: str,
        *,
        window: Sequence[float] | None = None,
        width: int = 1600,
        height: int = 1200,
        theme: str = "dark",
    ) -> str: ...

    def info(self) -> dict[str, Any]: ...

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]: ...

    def step_rows(self) -> list[dict[str, Any]]:
        """The journal as rows: ``index``, ``op``, ``label``, ``params``, ``state`` and an
        entity count.

        The shape the plugin already reports its steps in, so a read-back written once
        reads every backend. `RecordingBackend` maps its own :class:`StepRecord`\\ s;
        `PluginBackend` asks the DWG over RPC, because there the DWG is the journal.
        """
        ...

    def rollback_to(self, index: int) -> dict[str, Any]:
        """Erase everything drawn after step ``index``, newest first.

        Index 0 is before the first step, which is what empties a drawing. Backend-neutral
        even though the two journals are not: `RecordingBackend` marks its own steps undone
        and erases their handles, while `PluginBackend` delegates to the DWG's own rollback,
        so the palette and this call cannot disagree about what is drawn.
        """
        ...

    # --- primitives --------------------------------------------------------
    def line(
        self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None
    ) -> Any: ...

    def polyline(
        self,
        points: Sequence[Point],
        *,
        closed: bool = False,
        layer: str = "0",
        linetype: str | None = None,
    ) -> Any: ...

    def circle(self, center: Point, radius: float, *, layer: str = "0") -> Any: ...

    def arc(
        self,
        center: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        *,
        layer: str = "0",
    ) -> Any: ...

    def text(
        self,
        pos: Point,
        content: str,
        height: float,
        *,
        layer: str = "0",
        rotation: float = 0.0,
        align: str = "left",
    ) -> Any: ...

    def mtext(
        self,
        pos: Point,
        content: str,
        height: float,
        width: float = 0.0,
        *,
        layer: str = "0",
        align: str = "center",
    ) -> Any: ...

    def hatch(
        self,
        points: Sequence[Point],
        pattern: str = "ANSI31",
        scale: float = 1.0,
        *,
        layer: str = "0",
        angle: float = 0.0,
        solid: bool = False,
    ) -> Any: ...

    def linear_dimension(
        self,
        p1: Point,
        p2: Point,
        offset: float,
        *,
        layer: str = "0",
        text_height: float | None = None,
    ) -> Any: ...

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
    ) -> Any:
        """Place a block reference, importing the definition if it is not there yet.

        Only the plugin backend has this so far; the others raise
        :class:`BackendError`. It is in the contract rather than on one backend because
        placing a library element is an ordinary drawing operation, and an op that quietly
        worked on one backend and not another would be worse than one that says so.
        """
        ...

    def create_block(self, name: str, primitives: list[dict[str, Any]], base_point: Point) -> Any:
        """Create a block definition from recorded primitives, `base_point` its origin.

        A block definition is **document state**: it is not journalled, and a rollback never
        erases it. `create_block` is therefore called directly rather than through a recorded
        draw method — the same rule the page setup and the plugin's `EnsureBlock` follow —
        because an element captured into a definition and then placed is two steps, and
        rolling the first back must leave the definition standing or the second's reference
        would dangle. Only the dxf backend implements this so far; the others raise
        :class:`BackendError` naming the capability, the honest refusal `insert` makes where a
        library block cannot go.
        """
        ...
