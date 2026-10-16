"""The MCP server: the tool surface an AI client talks to.

Every drawing tool is a thin wrapper over :mod:`acadmcp.ops` and a backend, so the
same tool works whether the target is a live AutoCAD session (``backend="com"``), a
headless DXF file (``backend="dxf"``), or the in-AutoCAD step plugin
(``backend="plugin"``), which keeps what it draws in the drawing's own journal so a
step can be reviewed and rolled back.

The ``step_*`` and ``plugin_*`` tools at the end of this file drive that journal.

Run it with ``acadmcp`` or ``python -m acadmcp.server``.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # mcp 2.x renamed FastMCP to MCPServer; the decorator API is unchanged.
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image

from mcp.types import ToolAnnotations

from . import (
    capture_ops,
    catalog,
    critique,
    geom,
    layers,
    ops,
    prompts,
    settings,
    sheet,
    standards,
    suggest,
    summary,
    templates,
    terms,
    verify,
)
from .acad_plugin_client import PluginClient, PluginError, PluginUnavailable
from .backends import make_backend
from .backends.base import Backend, BackendError
from .backends.plugin import PluginBackend
from .catalog import Element
from .replies import (
    CheckStandardReply,
    CritiqueReply,
    ElementTypesReply,
    EntitiesReply,
    HistoryReply,
    StatusReply,
    SuggestReply,
    SummaryReply,
    TemplateListReply,
    VerifyReply,
)
from .steps import StepSession, describe_steps

log = logging.getLogger("acadmcp")

# The briefing a client receives at initialize. It names tools and prompts, and
# tests/test_audit_middleware.py checks every `word(` in it against the live
# tool registry, so a renamed tool breaks the suite rather than a client's
# first session.
_INSTRUCTIONS = """\
Architectural floor-plan server: walls, rooms, doors, windows, cores, catalogue
elements, sheets. Millimetres everywhere; degrees counter-clockwise from +X.

The fast path:
1. drawing_new(backend="dxf") — headless is the default; AutoCAD is not needed.
2. draw_wall_with_openings(points=<ring>, closed=True, openings=[...]) — the
   building envelope as ONE closed run, never four separate runs.
3. draw_room(boundary=<wall centreline ring>, name=...) once per room.
4. place_element, draw_axis_grid, add_dimension — then drawing_sheet(paper=...,
   scale=...) and read its fits flag.
5. Verify before reporting done: drawing_screenshot() and look at it,
   drawing_verify() must say "matches", drawing_summary() read back against the
   brief, and drawing_critique() checks the components against the journal —
   then drawing_finalize(), the done-gate, which refuses while an error stands.

One tool call is one journal step: drawing_undo() takes the last call back, and
drawing_replay(backend="plugin") carries a headless draft into live AutoCAD.
Three prompt templates — floor_plan, headless_to_live, drawing_review — walk
through each workflow in full.
"""


def _result_is_error(result: Any) -> bool:
    """Read the error flag off whatever shape the result arrived as.

    The middleware tier sits above serialisation, so the result may be the raw
    dict, a BaseModel carrying ``isError``, or an object exposing ``is_error``.
    """
    if isinstance(result, dict):
        return bool(result.get("isError"))
    return bool(getattr(result, "isError", getattr(result, "is_error", False)))


class _AuditMiddleware:
    """Log every tool call with its elapsed time.

    Follows the mcp 2.x ``ServerMiddleware`` protocol — an async
    ``(ctx, call_next) -> result`` callable handed to the constructor's
    ``middleware`` list — rather than FastMCP's ``add_middleware`` class API,
    which this SDK does not have. ``ctx.params`` is raw (pre-validation), so a
    malformed call still gets logged under the name it asked for.
    """

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        tool = (ctx.params or {}).get("name", "?")
        start = time.monotonic()
        try:
            result = await call_next(ctx)
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            log.warning("TOOL ERR %-40s %6.1fms  %s", tool, elapsed, exc)
            raise
        elapsed = (time.monotonic() - start) * 1000
        if _result_is_error(result):
            log.warning("TOOL ERR %-40s %6.1fms  (refused)", tool, elapsed)
        else:
            log.info("TOOL OK  %-40s %6.1fms", tool, elapsed)
        return result


# The mcp 1.x fallback constructor has neither parameter, so both are gated on
# the signature rather than on the import branch that picked _Server.
_ctor_params = inspect.signature(_Server.__init__).parameters
_server_kwargs: dict[str, Any] = {}
if "instructions" in _ctor_params:
    _server_kwargs["instructions"] = _INSTRUCTIONS
if "middleware" in _ctor_params:
    _server_kwargs["middleware"] = [_AuditMiddleware()]

mcp = _Server("autocad_mcp", **_server_kwargs)
prompts.register(mcp)

# Tool annotations, one set per behaviour class — the client reads these to decide
# what it may auto-approve, so the rules are:
#   _READONLY   a pure query: no state changes, repeats freely.
#   _IDEMPOTENT a side effect that repeats cleanly: render a file, move the view,
#               save, ensure a layer or text style exists.
#   _MUTATING   additive drawing and journaling: two calls draw twice, but nothing
#               is destroyed — a wrong step is one drawing_undo away.
#   _DESTRUCTIVE            erases entities or orphans the session's drawing
#                           (drawing_undo, step_control, target switches).
#   _DESTRUCTIVE_IDEMPOTENT a target switch that repeats cleanly (drawing_open and
#                           the connects) or the re-runnable installer.
# openWorldHint is False throughout: everything here is local machine state.
_READONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_MUTATING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


class _Session:
    """Holds the one drawing the tools are currently acting on."""

    def __init__(self) -> None:
        self.backend: Backend | None = None
        self.kind: str = "dxf"

    def require(self) -> Backend:
        if self.backend is None:
            # Start a scratch DXF document so tools work without an explicit new/open.
            self.backend = make_backend("dxf")
            self.kind = "dxf"
            ops.setup_layers(self.backend)
            _stamp_catalog(self.backend)
        return self.backend

    def reset(self, backend: Backend, kind: str) -> None:
        self.backend = backend
        self.kind = kind


_state = _Session()


# ---------------------------------------------------------------------------
# the catalogue a drawing remembers
# ---------------------------------------------------------------------------
#
# The catalogue is ambient — a package file plus whichever project acadmcp.json is found
# by walking up from the working directory — so the same drawing reopened elsewhere would
# resolve its doors against something else. The identity (entries and all) is stamped into
# the journal where a drawing begins, so the drawing says what drew it, and reported back.


def _stamp_catalog(be: Backend, loaded: Any = None) -> None:
    """Record which catalogue this drawing is being drawn with.

    A backend without `note_catalog` is left alone rather than made to grow one, and a
    backend that journals elsewhere (the plugin keeps its journal in the DWG) accepts the
    stamp on its local Journal without harm.
    """
    note = getattr(be, "note_catalog", None)
    if note is None:
        return
    note(loaded if loaded is not None else catalog.current())


def _recorded_catalog(be: Backend) -> dict[str, Any] | None:
    """The catalogue identity a drawing recorded, read straight off its journal, or None.

    Read off the journal rather than through `drawing_catalog()` because this is called on
    every drawing call (to see whether a stamp is still needed) and must not hash the
    loaded catalogue to answer a yes/no question.
    """
    meta = getattr(getattr(be, "journal", None), "meta", None)
    if not isinstance(meta, dict):
        return None
    recorded = meta.get("catalog")
    return recorded if isinstance(recorded, dict) else None


def _ensure_catalog_stamp(be: Backend) -> None:
    """Stamp the catalogue the first time a drawing is touched, and not again per call."""
    if _recorded_catalog(be) is None:
        _stamp_catalog(be)


def _adopt_catalog(be: Backend) -> tuple[Any, bool, dict[str, Any]]:
    """Choose the catalogue a drawing about to be edited resolves its elements against.

    Precedence is the plan's and it is a decision, not an accident: a project's own files
    win, then the copy embedded in the drawing, then the package's starter. The local
    standard is the authority — a drawing from another office must not silently redraw its
    doors with this office's blocks — but any difference is *reported*, never hidden.

    Returns the chosen catalogue, whether it still needs stamping, and the report keys.
    """
    project = list(settings.current().catalogs or [])
    getter = getattr(be, "embedded_catalog", None)
    embedded = getter() if getter is not None else None
    if project:
        chosen, source, stamp = catalog.current(), "project", True
    elif embedded is not None and embedded.elements:
        # No local standard, so the copy the drawing carries beats the package starter —
        # and it is already recorded in the drawing verbatim, so it needs no re-stamp.
        chosen, source, stamp = catalog.use(embedded), "drawing", False
    else:
        chosen, source, stamp = catalog.current(), "package", True

    out: dict[str, Any] = {"catalog_source": source}
    recorded = _recorded_catalog(be)
    if (
        isinstance(recorded, dict)
        and recorded.get("digest")
        and recorded["digest"] != chosen.digest()
    ):
        out["catalog_warning"] = (
            "this drawing was drawn with a different catalogue "
            f"({recorded.get('count')} elements, {recorded.get('library') or 'no library'}); "
            f"the {source} catalogue wins, but the elements may not be the ones it was drawn with"
        )
    return chosen, stamp, out


@contextlib.contextmanager
def _auto_step(op: str, params: dict[str, Any], label: str | None = None) -> Iterator[Backend]:
    """Wrap one tool call in a step, when the backend journals.

    An explicit step (step_begin) takes precedence: while one is open the
    drawing records into it, and no implicit commit happens. Backends without
    a journal are passed through untouched.

    `label` is human decoration on the step, not a parameter — the history
    renders "{op} {label}", so a room's name belongs there rather than buried
    in the params.
    """
    be = _state.require()
    _ensure_catalog_stamp(be)
    stepper = getattr(be, "step", None)
    if stepper is None or getattr(be, "open_step", ""):
        yield be
        return
    with stepper(op, label=label, params=params):
        yield be


def _pt(p: Any) -> tuple[float, float]:
    """Accept [x, y], (x, y) or {"x": .., "y": ..} from the client, refusing the rest by name.

    Every way a point can be malformed raises a ValueError that names what was received and
    what a point is, because the SDK masks anything but a ToolError behind a bare "Error
    executing tool <name>" — a short list, a dict missing a key, None or a member that is not
    a number all used to reach the model as nothing at all, leaving it unable to correct
    itself. ``_surface_input_errors`` turns this ValueError back into a readable message.
    """
    if isinstance(p, dict):
        try:
            x, y = p["x"], p["y"]
        except KeyError as exc:
            raise ValueError(
                "a point is {'x': x, 'y': y} in millimetres, got a mapping without "
                f"{exc.args[0]!r}: {p!r}"
            ) from exc
    else:
        try:
            x, y = p[0], p[1]
        except (TypeError, IndexError, KeyError) as exc:
            raise ValueError(f"a point is [x, y] in millimetres, got {p!r}") from exc
    try:
        return (float(x), float(y))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"a point's coordinates must be numbers in millimetres, got {p!r}"
        ) from exc


def _pts(points: Any) -> list[tuple[float, float]]:
    return [_pt(p) for p in points]


def _window(window: Any) -> tuple[float, float, float, float] | None:
    """Accept [min_x, min_y, max_x, max_y] or [p1, p2] as a capture window."""
    if not window:
        return None
    values = list(window)
    if len(values) == 4 and not isinstance(values[0], (list, tuple, dict)):
        return tuple(float(v) for v in values)  # type: ignore[return-value]
    if len(values) == 2:
        (x1, y1), (x2, y2) = _pt(values[0]), _pt(values[1])
        return (x1, y1, x2, y2)
    raise BackendError("window must be [min_x, min_y, max_x, max_y] or [p1, p2]")


# ---------------------------------------------------------------------------
# session / document
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
def drawing_help(term: str | None = None) -> dict[str, Any]:
    """List the available tool groups and the workflow to follow.

    Pass `term` to go from a word to the tool that does it — an AutoCAD command name
    ("HATCH", "-INSERT"), an English phrase, or a Chinese one ("承重柱", "房间面积"). Each
    match names the tool, what it matched on, the tool's own summary line and the AutoCAD
    commands it stands for. The vocabulary is `acadmcp.terms`, and a tool cannot exist
    without an entry there, which is what keeps this lookup from going stale.
    """
    if term:
        # The summary is read off the live registry rather than stored beside the term, so
        # it cannot drift from the tool's own docstring — the rule `summary.py` follows for
        # room areas, applied to the surface that `drawing_summary` cannot see.
        described = {t.name: (t.description or "") for t in mcp._tool_manager.list_tools()}
        matches = []
        for name, matched_on in terms.lookup(term):
            record = terms.terms_for(name)
            lines = described.get(name, "").strip().splitlines()
            matches.append(
                {
                    "tool": name,
                    "matched": matched_on,
                    "summary": lines[0].strip() if lines else "",
                    "cad": list(record.cad) if record else [],
                }
            )
        return {
            "term": term,
            "count": len(matches),
            "matches": matches,
            "hint": ""
            if matches
            else (
                "no tool matched. This index covers the vocabulary of the 56 tools — an "
                "AutoCAD command name, an English phrase or a Chinese one. It does not cover "
                "the element catalogue: for those, call element_types() (钢立柱, 焊接符号, "
                "钢梁 and the rest live there), and call drawing_help() with no term for the "
                "whole surface."
            ),
        }
    return {
        "units": "millimetres throughout",
        "backends": {
            "dxf": "headless ezdxf document; no AutoCAD needed, previewable to PNG",
            "com": "drives a running AutoCAD over COM; entities appear live in the editor",
            "plugin": "drives the in-AutoCAD plugin in steps you can review and roll back",
        },
        "workflow": [
            "drawing_new(backend='dxf'|'com', path=...) or drawing_open(path)",
            "just draw — every tool call is one undoable step; drawing_undo/"
            "drawing_redo/drawing_history manage the journal",
            "draw_axis_grid(...) to lay out the structural grid",
            "draw_wall / draw_wall_with_openings / draw_room for the envelope and partitions",
            "draw_door, draw_window, draw_column for openings and structure",
            "place_element / place_along / place_grid to repeat a library element — a frame"
            " of columns, a wall of windows — sized through the block's dynamic parameters",
            "draw_stair, draw_lift, draw_equipment(kind='shaft') for the core and risers",
            "draw_room again with walls=False to label a room whose walls exist",
            "add_dimension / add_text / add_hatch for annotation",
            "drawing_sheet(paper='A3', scale='1:100', title=...) to put it on a named sheet — a "
            "page setup, a frame and a title block on the drawing's own layout — then "
            "drawing_plot_pdf(...) for a PDF whose paper size is read back out of the file",
            "drawing_replay(backend='plugin') re-draws a headless session into AutoCAD",
            "drawing_screenshot(...) to eyeball it (inline=False for just a PNG file), "
            "drawing_save(...) to keep it",
        ],
        "element_library": {
            "how": "element_types() lists what the catalogue offers; library_list(source) "
            "lists the blocks in a library .dwg and their dynamic parameters",
            "catalogue": "a JSON file naming the library block, size variants, layer and tag "
            "prefix for each element; a project adds its own via acadmcp.json",
            "tags": "numbered from the tags already in the drawing, never from a stored counter",
        },
        "choosing_a_tool": {
            "catalogue_or_parametric": (
                "A catalogue element is a specific named artefact — a model number, a "
                "schedule row, a standard section — and it is placed with place_element. "
                "Everything whose identity is just numbers you already have (a wall, a "
                "room, a column by size, a lift shaft by its corners) is drawn by its own "
                "tool. place_element also draws parametric twins where the library cannot "
                "be inserted, so one element name works on every backend."
            ),
            "checking_your_work": (
                "drawing_summary first — rooms, areas, openings and counts as data. "
                "drawing_history is what was drawn, one line per step. "
                "drawing_verify checks the drawing against the journal's own account of "
                "it, which is what catches a save that dropped entities. "
                "drawing_screenshot last, and pass `window` to frame a detail."
            ),
            "recovering": (
                "Every tool call is one journal step: drawing_undo(count) or "
                "drawing_rollback_to(index) takes back exactly what was drawn, and "
                "drawing_history gives the indices to aim at."
            ),
            "finding_a_tool": (
                "drawing_help(term=...) turns a word into tools: an AutoCAD command name "
                '("HATCH"), an English phrase, or a Chinese one ("承重柱"). Every tool '
                "has a vocabulary entry in acadmcp.terms, and tests/test_terms.py fails if "
                "one is added without it."
            ),
        },
        "conventions": {
            "layers": sorted(settings.current().layers or layers.LAYERS),
            "room_area": "computed from the wall centreline ring minus half a wall thickness",
            "angles": "degrees, counter-clockwise from +X",
            "door_swing": "'left' or 'right' relative to the wall direction",
            "units": "millimetres everywhere — a metres-valued call is refused, not drawn",
        },
    }


@mcp.tool(annotations=_READONLY)
def drawing_status() -> StatusReply:
    """Report the active backend, document, entity count and layer list.

    `entities` is what is in the drawing now; `ops` is a cumulative count of
    draw calls since connect and does not go down on undo, so ops > entities
    just means steps were taken back.

    `catalog` is the catalogue in force now, `drawing_catalog` is what the drawing itself
    recorded (and whether the two disagree), and on the com backend `sidecar` names the
    file a session's journal was written to beside the drawing.

    `sheet` is the paper this drawing is set up on — empty until `drawing_sheet` is called.

    `standard` is the drafting standard in force: how many are loaded, which one is in
    force, and every file or row the loader had to skip — so a project's own GB/T file is
    visible here rather than only in how the layers came out.
    """
    be = _state.require()
    out = dict(be.info())
    loaded = catalog.current()
    out["catalog"] = {
        "count": len(loaded.elements),
        "digest": loaded.digest(),
        "library": settings.current().library,
        "skipped": list(loaded.skipped),
    }
    out["sheet"] = dict(getattr(be, "sheet", {}) or {})
    out["standard"] = standards.describe()
    recorder = getattr(be, "drawing_catalog", None)
    if recorder is not None:
        out["drawing_catalog"] = recorder()
    return out


@mcp.tool(annotations=_DESTRUCTIVE)
def drawing_new(
    backend: str = "dxf", path: str | None = None, template: str = ""
) -> dict[str, Any]:
    """Start a new drawing on the chosen backend, optionally from a template.

    backend="dxf" builds a headless document (optionally at `path` — the file is
    written on drawing_save, not read now); backend="com" asks the running
    AutoCAD for a new drawing.

    `template` is a key from `template_list` (`gb_a3_arch` is the A3 architectural sheet).
    A non-empty template starts the drawing from a specification: the standard it names
    drives the layer table, the paper, orientation and scale become the page setup, and the
    frame and title block are drawn as one `drawing_sheet` step — so a plan begins on the
    right paper at the right scale instead of gaining a sheet later. An unknown key is
    refused, naming the templates that exist. Empty (the default) is the bare new drawing
    this tool has always made. A template draws paperspace, so it is `dxf`-only: the com
    and plugin backends have no paperspace path and refuse by name.

    Starting a new drawing replaces the session's current one; when that one
    had content, the reply carries a `note` saying so.
    """
    # Resolved before the backend is built: a misspelt key is the caller's mistake and must
    # not drop the drawing they were working on (the refusal travels as a ValueError, which
    # the tool surface re-raises with its message intact).
    spec = templates.resolve_template(template) if template.strip() else None
    dropped = _session_drawing()
    kind = backend.strip().lower()
    if kind == "com":
        be = make_backend("com", new_document=True)
    elif kind == "dxf":
        be = make_backend("dxf", path=path, create=True)
    elif kind == "plugin":
        # The plugin draws into the AutoCAD drawing that is open: it cannot be told
        # to open a file the way the dxf backend can, so say that rather than ignore
        # `path`.
        if path:
            raise BackendError(
                "the plugin backend draws into AutoCAD's active drawing; open the file "
                "in AutoCAD, or use backend='dxf'"
            )
        be = _plugin_backend_connect(new_document=True)
    else:
        raise BackendError(f"unknown backend {backend!r}; expected 'dxf', 'com' or 'plugin'")
    _state.reset(be, kind)
    ops.setup_layers(be)
    _stamp_catalog(be)
    sheet_report = _start_from_template(be, spec) if spec is not None else {}
    return {"ok": True, **be.info(), **sheet_report, **_switch_note(dropped)}


def _start_from_template(be: Backend, spec: templates.TemplateSpec) -> dict[str, Any]:
    """Apply a template's specification to a freshly started drawing, as one sheet step.

    The order is the one ``scripts/build_templates.py`` builds its artefacts in — the
    standard's layers, then the page setup, then the frame and title block — so a drawing
    started from a template and the committed artefact for that template carry the same
    sheet. The 比例 field is written from the spec's own scale, because a sheet whose title
    block states no scale fails the sheet's own point (GB/T 10609.1).
    """
    if not getattr(be, "supports_sheet", False):
        raise BackendError(
            f"backend {be.name!r} cannot start from template {spec.key!r}: a template is a "
            "page setup, a frame and a title block, which are paperspace — only the dxf "
            "driver writes them. Use drawing_new(backend='dxf', template=...)"
        )
    ops.setup_layers(be, standard=spec.standard)
    paper = sheet.resolve_paper(spec.paper, spec.orientation)
    ratio = sheet.resolve_scale(spec.scale)
    be.set_page_setup(paper, ratio)
    fields = {**spec.title_fields, "比例": spec.scale}
    params = {
        "paper": paper.name,
        "scale": f"{ratio[0]}:{ratio[1]}",
        "orientation": spec.orientation,
        "template": spec.key,
    }
    with _auto_step("drawing_sheet", params, label=spec.label) as active:
        report = ops.draw_sheet(active, paper, ratio, fields)
    report["template"] = spec.describe()
    return report


@mcp.tool(annotations=_READONLY)
def template_list() -> TemplateListReply:
    """List the templates a drawing can start from, with the sheet each one builds.

    Each row names the key `drawing_new(template=...)` takes, the standard the template
    draws under, the paper, orientation and plot scale it sets, the title block's own
    fields, and the committed `.dxf` artefact under `templates/` that the specification
    builds. A project's own templates file would merge over the package's; today this is
    what ships.
    """
    return {"ok": True, "templates": templates.template_rows()}


@mcp.tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def drawing_open(path: str) -> dict[str, Any]:
    """Open an existing DXF file so further tools edit it.

    Opening replaces the session's current drawing; when that one had content,
    the reply carries a `note` saying so.

    The catalogue to draw with is chosen in order: the project's own catalogue files, then
    the copy embedded in the drawing, then the package's starter. A difference between the
    two is reported (`catalog_warning`), not hidden — the local standard wins, but the
    elements may not be the ones the drawing was drawn with.
    """
    dropped = _session_drawing()
    be = make_backend("dxf", path=path)
    _state.reset(be, "dxf")
    chosen, stamp, provenance = _adopt_catalog(be)
    if stamp:
        _stamp_catalog(be, chosen)
    return {"ok": True, **be.info(), **provenance, **_switch_note(dropped)}


@mcp.tool(annotations=_MUTATING)
def drawing_sheet(
    paper: str = "A3",
    scale: str = "1:100",
    orientation: str = "landscape",
    title: str = "",
    number: str = "",
    sheet_of: str = "",
    style_sheet: str = "",
) -> dict[str, Any]:
    """Put a sheet on the drawing: a named paper, a plot scale, a frame and a title block.

    The paper is the ISO A series (`A0`…`A4`), which is what GB/T 50001 takes its sheet sizes
    from; `scale` is a ratio like `1:100`. Both are refused by name when this package has no
    row for them, because a plan on the wrong paper or at the wrong scale is discovered at the
    printer rather than here. `title`, `number` and `sheet_of` are written into the title block
    as 图名/图号/张次, with 比例 always added from the scale — a sheet whose title block does not
    state its scale fails the sheet's own point. A field too long for its cell is shrunk to fit.

    The page setup goes onto the file's own layout, so a DXF saved from here opens in AutoCAD
    with the sheet already configured — `style_sheet` names a plot style (a `.ctb`) when the
    office has one. The frame, the title block and the viewport are drawn at 1:1 in paper
    millimetres, so they are paper rather than anything that scales with the plan.

    The page setup is document state and outlives an undo, the way a block definition does: one
    `drawing_undo` takes back the frame and the viewport, not the paper. The com and plugin
    backends have no paperspace path and refuse by name.
    """
    be = _state.require()
    if not getattr(be, "supports_sheet", False):
        raise BackendError(
            f"backend {be.name!r} cannot make a sheet: a page setup, a frame and a viewport are "
            "paperspace, which only the dxf driver writes. Use drawing_new(backend='dxf') for a "
            "sheet, or set the page setup in AutoCAD itself"
        )
    resolved = sheet.resolve_paper(paper, orientation)
    ratio = sheet.resolve_scale(scale)
    # A frame is drawn content, not a setting, so a second sheet would put a second frame and a
    # second viewport on the same layout — measured: two calls left 24 lines on A-SHEET where
    # there should be 12. Refused with the recovery named rather than replaced in place: erasing
    # the old frame is not this step's to do, and an undo that cannot put it back is worse than a
    # refusal. The page setup itself is document state and outlives the undo, so the sequence the
    # message gives ends with the sheet the caller wanted.
    already = be.entities(layer="A-SHEET", limit=1000)
    if already:
        raise BackendError(
            f"this drawing already carries a sheet ({len(already)} lines on A-SHEET): the frame "
            "is drawn, so a second one would leave two frames and two viewports on the same "
            "layout. Call drawing_undo to take the existing sheet back — the page setup itself is "
            "document state and outlives the undo — then call this again with the paper and scale "
            "you want"
        )
    meta = {k: v for k, v in {"图名": title, "图号": number, "张次": sheet_of}.items() if v}
    meta["比例"] = f"{ratio[0]}:{ratio[1]}"
    params = {
        "paper": resolved.name,
        "scale": f"{ratio[0]}:{ratio[1]}",
        "orientation": orientation,
    }
    with _auto_step("drawing_sheet", params, label=title) as be:
        report = be.set_page_setup(resolved, ratio, style_sheet=style_sheet)
        report.update(ops.draw_sheet(be, resolved, ratio, meta))

    # Whether the whole plan is on the page is the one thing a caller cannot work out from the
    # sheet, and the one it would otherwise discover from a plot with the building running off
    # the edge: at 1:100 an A3 shows 19.5 x 26.7 m, so a 24 m wide bay does not fit and the reply
    # has to say so. Reported, never raised — a sheet narrower than the building is a design
    # choice, and a detail sheet is drawn that way on purpose.
    extent = None
    measurer = getattr(be, "plan_extent", None)
    if measurer is not None:
        extent = measurer()
    if extent is not None:
        report["plan_mm"] = [round(v, 3) for v in (extent[2] - extent[0], extent[3] - extent[1])]
        report["fits"] = sheet.fits_on_sheet(resolved, ratio, extent)
        if not report["fits"]:
            report["note"] = (
                f"the plan is {report['plan_mm'][0]:.0f} x {report['plan_mm'][1]:.0f} mm and "
                f"{resolved.name} at {report['scale']} shows "
                f"{' x '.join(f'{v:.0f}' for v in sheet.model_window(resolved, ratio))} mm, so "
                f"the sheet holds a detail rather than all of it — the largest scale that holds "
                f"it whole is {sheet.largest_scale_that_fits(resolved, extent)}"
            )
    return {"ok": True, "title_block": list(meta), **report}


@mcp.tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def acad_connect(
    mode: str = "com", new_document: bool = False, visible: bool = True
) -> dict[str, Any]:
    """Attach to a running AutoCAD (launching and showing it if needed).

    mode="com" draws live over ActiveX; mode="plugin" draws through the step
    plugin, so every step is reviewable and can be rolled back from the palette
    (open one explicitly with step_begin, draw, then step_commit). Call this
    before issuing drawing commands against AutoCAD.
    """
    dropped = _session_drawing()
    kind = mode.strip().lower()
    if kind == "com":
        be = make_backend("com", visible=visible, new_document=new_document)
    elif kind == "plugin":
        be = _plugin_backend_connect(new_document=new_document)
    else:
        raise BackendError(f"unknown mode {mode!r}; expected 'com' or 'plugin'")
    _state.reset(be, kind)
    ops.setup_layers(be)
    return {"ok": True, **be.info(), **_switch_note(dropped)}


def _switch_note(dropped: str) -> dict[str, str]:
    """The `note` a backend switch attaches when it discarded a drawing."""
    if not dropped:
        return {}
    return {
        "note": (
            f"this session was drawing {dropped}, which is now no longer the target; "
            "save it with drawing_save(path=...dxf) first if it is still wanted"
        )
    }


def _session_drawing() -> str:
    """What the session is drawing right now, or "" when there is nothing to lose.

    Switching backend drops the current drawing, and that used to happen silently.
    """
    be = _state.backend
    if be is None:
        return ""
    try:
        info = be.info()
    except Exception:  # noqa: BLE001 - a status probe must never block the switch
        return ""
    count = info.get("entities")
    if not count:
        return ""
    return f"{count} entities in {info.get('path') or info.get('document') or 'an unsaved drawing'}"


def _default_screenshot_path() -> str:
    """Where a screenshot goes when the caller did not name a file.

    Deliberately not the working directory: the server's cwd is somebody's project, and
    a relative default dropped ``screenshot.png`` into a repository root during one run
    of this tool — even for a call that then failed.
    """
    return str(Path(tempfile.gettempdir()) / "acadmcp-screenshot.png")


@mcp.tool(annotations=_IDEMPOTENT)
def acad_zoom(corner1: Any = None, corner2: Any = None) -> dict[str, Any]:
    """Zoom the AutoCAD view: to the whole drawing, or to a rectangle.

    With no corners the view is fitted to all of the drawing. With corners it
    frames that rectangle — home in on a detail before drawing_screenshot, so
    the capture is legible rather than a whole plan at thumbnail scale.
    """
    be = _state.require()
    if corner1 is None or corner2 is None:
        zoom = getattr(be, "zoom_extents", None)
        if zoom is None:
            return {"ok": False, "reason": "only available on the com and plugin backends"}
        zoom()
        return {"ok": True}
    zoom_window = getattr(be, "zoom_window", None)
    if zoom_window is None:
        return {"ok": False, "reason": "only available on the com and plugin backends"}
    zoom_window(_pt(corner1), _pt(corner2))
    return {"ok": True}


@mcp.tool(annotations=_IDEMPOTENT)
def drawing_save(path: str | None = None) -> dict[str, Any]:
    """Save the drawing, to .dwg or .dxf as the path's suffix says.

    On the com backend this saves through AutoCAD, and saving a live drawing to
    .dwg renames that document — pass a .dxf path for a non-destructive copy.

    A save stamps the catalogue that drew the drawing into the journal. On the com backend
    AutoCAD cannot store the journal inside the .dwg, so it is written to a sidecar file
    beside the drawing — the reply names it, because a file appearing next to somebody's
    drawing unannounced is not acceptable.
    """
    be = _state.require()
    _stamp_catalog(be)
    saved = be.save(path)
    out: dict[str, Any] = {"ok": True, "path": saved}
    sidecar = getattr(be, "sidecar_path", None)
    if sidecar:
        out["sidecar"] = str(sidecar)
        out["note"] = (
            "the step journal and the catalogue identity were also written to "
            f"{sidecar} — AutoCAD cannot store them inside the .dwg, so a com session's "
            "undo history lives in that sidecar file beside the drawing"
        )
    return out


@mcp.tool(annotations=_DESTRUCTIVE)
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


@mcp.tool(annotations=_MUTATING)
def drawing_redo(count: int = 1) -> dict[str, Any]:
    """Re-draw steps that drawing_undo took back, oldest first.

    Undone steps always form a contiguous tail, so "oldest first" replays history in
    the order it was drawn — after undo(count=2), one redo restores the earlier of the
    two steps, not the later one.
    """
    be = _state.require()
    redo = getattr(be, "redo", None)
    if redo is None:
        raise BackendError(f"backend {be.name!r} keeps no journal; nothing can be redone")
    return {"ok": True, **redo(int(count))}


@mcp.tool(annotations=_READONLY)
def drawing_history() -> HistoryReply:
    """What has been drawn, one line per step — much cheaper than a screenshot.

    Read this to know the drawing's state before deciding what to undo or add;
    reach for drawing_screenshot only when the geometry itself needs eyeballing.

    `rendered` is the text a person reads; `steps` is the same history as machine-readable
    rows (index, op, label, params, state), so an agent can pick an index to aim
    drawing_rollback_to at without counting.
    """
    be = _state.require()
    history = getattr(be, "history", None)
    if history is None:
        raise BackendError(f"backend {be.name!r} keeps no journal")
    rows = getattr(be, "step_rows", None)
    return {
        "ok": True,
        "backend": be.name,
        "rendered": history(),
        "steps": rows() if rows is not None else [],
    }


@mcp.tool(annotations=_READONLY)
def drawing_summary() -> SummaryReply:
    """What the drawing contains, as data: rooms with areas, openings, elements, layers.

    Derived from the journal, which records each step's op and arguments, so it needs no
    screen and survives a reopen on the headless backend. Read this before a screenshot:
    it answers "how many doors, what sizes, what is the floor area" exactly, and a
    screenshot cannot answer it at all.
    """
    be = _state.require()
    return {"ok": True, **summary.summarise(be.step_rows(), catalog=_recorded_catalog(be))}


@mcp.tool(annotations=_READONLY)
def drawing_verify(limit: int = 20000) -> VerifyReply:
    """Check the drawing against the journal's own account of what it drew.

    `verdict` is "matches" when every applied step's entities are there, "unaccounted" when
    the drawing holds more than any step recorded (something drew outside a step), and
    "missing" when it holds fewer (a save dropped them, or an erase went too far).
    "walk_truncated" means the listing hit `limit`, so the count is a floor and no verdict is
    possible — raise `limit` and ask again. "no_baseline" means the drawing holds entities and
    records no steps at all, so there is nothing to reconcile: a file opened with
    `drawing_open` carries no record of what was already in it, and that is reported rather
    than called a fault.

    A step's row carries an entity count rather than handles, so this says the total is
    wrong without saying which entity is; `by_op` points at what was drawing when the books
    stopped agreeing. `empty_steps` lists applied steps that recorded nothing, which is
    ordinary for one that only ensured a layer and suspicious for one that draws.

    On the com backend `reported_entities` is the drawing's own count, which can exceed the
    listing because it includes proxy entities the listing skips; the two are reported side
    by side rather than reconciled.
    """
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    be = _state.require()
    # One more row than asked for: a walk returning exactly `limit` has either reached the end
    # or been cut short there, and the count alone cannot tell those apart. The extra row costs
    # nothing and makes `truncated` a fact instead of a guess — a caller who reads the entity
    # count out of drawing_status and passes it as `limit` used to get a false warning.
    entities = be.entities(limit=int(limit) + 1)
    info = be.info()
    reported = info.get("entities") if isinstance(info, dict) else None
    report = verify.reconcile(
        be.step_rows(),
        entities,
        truncated=len(entities) > int(limit),
        reported=reported if isinstance(reported, int) and reported >= 0 else None,
    )
    report["backend"] = be.name
    return report


@mcp.tool(annotations=_READONLY)
def drawing_critique(focus: str = "") -> CritiqueReply:
    """Check the drawing's components against the journal's own record of them.

    `drawing_verify` asks "is everything the journal recorded still there"; this asks
    "is what was drawn right": a door whose hinge sits on no recorded wall or on a wall
    no opening was cut into, an opening measured past the end of its run, a tag that
    breaks the M0921/C1521 convention or duplicates another, a text masquerading as a
    dimension, a plan the sheet's viewport cannot hold at its scale. The checks read the
    journal — present on every drawing this server made, on every backend — never an
    optional side record, so an empty `issues` really means nothing was found. A row that
    cannot be read is counted in `unreadable`, never raised.

    `focus` runs one check by its code (an unknown code is refused, naming the valid
    ones); empty runs them all. `counts` breaks the findings down by severity.
    """
    be = _state.require()
    tag_prefixes = {
        e.key: e.tag_prefix for e in catalog.current().elements.values() if e.tag_prefix
    }
    return {"ok": True, **critique.run(be.step_rows(), tag_prefixes, focus=focus)}


def _standard_snapshot(be: Backend) -> dict[str, Any]:
    """The drawing as `standards.check` reads it, gathered off the backend as plain data.

    ``be.entities()`` carries no text height, so the texts come from the journal's applied
    steps' recorded primitives instead — every ``text`` primitive carries its height, layer
    and content, and a sheet's title block (whose text is paper, at 1:1) rides along. The
    plugin keeps its journal in the DWG and reports the primitives on its own step rows, so
    both owners are read here — the same reconciliation `critique.py` already does.
    """
    info = be.info()
    records = {step.index: step for step in getattr(getattr(be, "journal", None), "steps", [])}
    texts: list[dict[str, Any]] = []
    for row in be.step_rows():
        if str(row.get("state", "")).strip().lower() in critique.INACTIVE_STATES:
            continue
        primitives = row.get("primitives")
        if not isinstance(primitives, list):
            record = records.get(row.get("index"))
            primitives = getattr(record, "primitives", None) or []
        for primitive in primitives:
            if not isinstance(primitive, dict) or primitive.get("kind") != "text":
                continue
            texts.append(
                {
                    "height": primitive.get("height"),
                    "layer": primitive.get("layer", ""),
                    "content": primitive.get("text", ""),
                    "paper": bool(primitive.get("paper")),
                }
            )
    return {
        "units": info.get("units", "mm"),
        "layers_in_use": sorted(
            {str(row.get("layer", "")) for row in be.entities(limit=10000) if row.get("layer")}
        ),
        "texts": texts,
        "sheet": info.get("sheet") or {},
    }


@mcp.tool(annotations=_READONLY)
def drawing_check_standard(standard: str = "") -> CheckStandardReply:
    """Check the drawing against the drafting standard in force.

    `standard` names one of the loaded GB/T standards; empty uses the standard in force
    (`drawing_status`'s `standard` block says which that is, and every file the loader had
    to skip). An unknown name is refused by name.

    The checks are the standard's own, and the ones AutoCAD's CHECKSTANDARDS runs: the unit
    is the millimetre this package draws in; every text height is in the standard's nominal
    series, read as a *paper* height (model height times the plot scale, and text on the
    sheet's layers already at 1:1 — a rule the reply's `assumed_scale` names when the
    drawing carries no sheet to take a denominator from); every layer in use is one the
    standard's table names (a warning, because a project's own layers are legitimate); and
    the sheet states its 比例. Like `drawing_critique` this reports and never draws: each
    finding carries `rule`, `severity`, `detail` and the `entities` it is about. It is
    advisory — `drawing_finalize` does not gate on it this wave.

    Deferred and named, never faked: the layer *definitions'* own colour, linetype and
    lineweight need a `layer_table()` read no backend exposes yet, so this wave compares
    the layer *names* in use (see `acadmcp.standards`'s docstring).
    """
    std = standards.in_force(standard)
    if std is None:
        names = ", ".join(standards.current().keys()) or "none"
        raise BackendError(
            f"no standard {standard!r} is loaded, so there is nothing to check against; "
            f"this session has: {names}. Empty `standard` uses the standard in force"
        )
    be = _state.require()
    issues = standards.check(std, _standard_snapshot(be))
    counts = {"error": 0, "warning": 0}
    for issue in issues:
        severity = str(issue.get("severity", ""))
        if severity in counts:
            counts[severity] += 1
    return {
        "ok": True,
        "standard": std.name,
        "label": std.label,
        "digest": std.digest(),
        "issues": issues,
        "counts": counts,
    }


@mcp.tool(annotations=_READONLY)
def drawing_finalize() -> dict[str, Any]:
    """The done-gate: verify and critique the drawing before it may be called finished.

    Refuses while anything is wrong — `drawing_verify`'s verdict must be "matches" and
    `drawing_critique` must hold no error-severity finding — and the refusal names what
    failed, so a floating door or a lost entity is reported, not shipped. A drawing this
    server did not draw gets "no_baseline" and is refused for that reason: the gate only
    vouches for what the journal accounts for. Warnings do not block; they are reported.

    A sheet is not required: a sheetless plan is finalizable, because a sheet is furniture
    around the drawing, not part of what the gate vouches for.
    """
    report = drawing_verify()
    verdict = report.get("verdict")
    problems: list[str] = []
    if verdict != "matches":
        problems.append(f"drawing_verify says {verdict!r}, not 'matches'")
    crit = drawing_critique()
    errors = [i for i in crit["issues"] if i["severity"] == "error"]
    if errors:
        shown = "; ".join(i["message"] for i in errors[:5])
        problems.append(f"{len(errors)} critique error(s): {shown}")
    if problems:
        raise BackendError("the drawing is not finished — " + " | ".join(problems))
    summ = drawing_summary()
    return {
        "ok": True,
        "verdict": verdict,
        "warnings": crit["counts"]["warning"],
        "rooms": len(summ.get("rooms", [])),
        "openings": len(summ.get("openings", [])),
        "elements": len(summ.get("elements", [])),
    }


@mcp.tool(annotations=_DESTRUCTIVE)
def drawing_rollback_to(index: int) -> dict[str, Any]:
    """Erase everything drawn after step `index`, newest first.

    Aim the undo at a step instead of counting: `drawing_history` lists the steps, so you
    can name the one you want standing. Index 0 is before the first step, which empties the
    drawing. On the plugin backend the DWG's own journal performs the rollback, so the
    palette and this call never disagree — but an *accepted* step blocks a rollback, and
    that refusal is reported rather than swallowed; override it explicitly with
    step_control(action='rollback_to', index=..., force=True).
    """
    be = _state.require()
    rollback = getattr(be, "rollback_to", None)
    if rollback is None:
        raise BackendError(f"backend {be.name!r} keeps no journal; nothing can be rolled back")
    return {"ok": True, **rollback(int(index))}


@mcp.tool(annotations=_DESTRUCTIVE)
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
    dropped = _session_drawing()
    _state.reset(target, kind)
    return {
        "ok": True,
        "backend": kind,
        "replayed": len(applicable) - len(failed),
        "failed": failed,
        **({"path": path} if kind == "dxf" else {}),
        **_switch_note(dropped),
    }


@mcp.tool(annotations=_IDEMPOTENT)
def drawing_screenshot(
    path: str | None = None,
    window: Any = None,
    width: int = 1600,
    height: int = 1200,
    theme: str = "dark",
    inline: bool = True,
) -> Any:
    """Look at the drawing. Returns an image you can see directly.

    This is the tool to call after drawing something, to check the result before
    going further or to verify a fix. Pass `window` to frame a region rather than
    the whole drawing — a full floor plan shrunk to 1600 px leaves the room
    labels unreadable, so zoom in when you need to read text.

    `window` is [min_x, min_y, max_x, max_y] in millimetres, or [p1, p2].
    inline=False returns only the PNG's path, for scripts that need a file on
    disk rather than a picture to look at.

    What you get depends on the backend: with backend="dxf" this re-renders the
    drawing cleanly, white-on-dark or black-on-white. With backend="com" it
    photographs the live AutoCAD window, so you see the true current view and
    zoom, but also AutoCAD's ribbon and panels around it.
    """
    be = _state.require()
    target = path or _default_screenshot_path()
    out = be.screenshot(
        target,
        window=_window(window),
        width=int(width),
        height=int(height),
        theme=theme,
    )
    if not inline:
        return {"ok": True, "path": out}
    return Image(path=out)


@mcp.tool(annotations=_IDEMPOTENT)
def drawing_plot_pdf(path: str | None = None, theme: str = "light") -> dict[str, Any]:
    """Plot the sheet to a PDF, then read the paper size back out of the PDF itself.

    `verified` answers one question — is the file the paper the sheet claims — and the size it
    answers with is measured from the PDF's own `/MediaBox`, not from the paper that was asked
    for, because the size a plot produced is what the file says it is. `mismatch_mm` gives how far
    the two disagree, so a near miss is visible rather than rounded into a pass.

    Needs a sheet: call `drawing_sheet` first, or open a drawing saved with one. The plan is drawn
    at the sheet's plot scale inside the viewport, and `plan_span_mm` says how much of the model is
    in shot. The com and plugin backends refuse by name — plotting a live drawing goes through
    AutoCAD's own plotter, which is not wired up here.
    """
    be = _state.require()
    if not getattr(be, "supports_sheet", False):
        raise BackendError(
            f"backend {be.name!r} cannot plot a sheet: only the dxf driver writes paperspace"
        )
    state = dict(getattr(be, "sheet", {}) or {})
    if not state:
        raise BackendError(
            "this drawing has no sheet to plot: call drawing_sheet(paper=..., scale=...) first"
        )
    paper = sheet.resolve_paper(state["paper"], state.get("orientation", "landscape"))
    ratio = sheet.resolve_scale(state["scale"])
    target = Path(path) if path else Path(_default_screenshot_path()).with_suffix(".pdf")
    be.plot_pdf(str(target), paper=paper, scale=ratio, theme=theme)

    measured = sheet.read_pdf_mediabox_mm(target)
    asked = (paper.width, paper.height)
    mismatch = max(abs(measured[0] - asked[0]), abs(measured[1] - asked[1]))
    plot = getattr(be, "last_plot", {}) or {}
    return {
        "ok": True,
        "path": str(target),
        "paper_mm": [round(v, 3) for v in measured],
        "asked_for_mm": [round(v, 3) for v in asked],
        "mismatch_mm": round(mismatch, 3),
        "verified": bool(mismatch <= 0.5),
        "scale": state["scale"],
        "viewport_mm": plot.get("viewport_mm", []),
        "plan_span_mm": plot.get("plan_span_mm", []),
    }


@mcp.tool(annotations=_READONLY)
def listing_entities(layer: str | None = None, limit: int = 100, offset: int = 0) -> EntitiesReply:
    """List entities, optionally filtered to one layer.

    Pages with `limit` and `offset`: `has_more` says the page was cut short and
    `next_offset` is where the next page starts. `total_count` is the full count
    when it is known — once a page reaches the end it is exact, and an
    unfiltered listing reads the drawing's own entity count; a layer-filtered
    page that has not reached the end reports None rather than walk the whole
    drawing to find out.

    With a `layer` filter the answer also carries `layer_known`: False means
    the layer name is not in the drawing at all, which is how a typo is told
    apart from an empty layer.
    """
    be = _state.require()
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    if int(offset) < 0:
        raise ValueError(f"offset must be at least 0, got {offset}")
    # The Backend protocol has no offset, so a page is cut from one over-fetch;
    # the protocol stays untouched and a shallow page costs one walk either way.
    probe = be.entities(layer=layer, limit=int(offset) + int(limit) + 1)
    page = probe[int(offset) : int(offset) + int(limit)]
    has_more = len(probe) > int(offset) + int(limit)
    total: int | None
    if not has_more:
        total = len(probe)  # the walk ended, so this many entities exist
    elif layer is None:
        # The drawing's own count — on com this may include proxy entities a
        # listing skips, so it can exceed the last page a caller ever reaches.
        reported = be.info().get("entities")
        total = reported if isinstance(reported, int) else None
    else:
        total = None
    out: dict[str, Any] = {
        "count": len(page),
        "offset": int(offset),
        "has_more": has_more,
        "next_offset": int(offset) + len(page) if has_more else None,
        "total_count": total,
        "entities": page,
    }
    if layer is not None:
        known = be.info().get("layers")
        if known is not None:
            out["layer_known"] = layer in known
    return out


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_IDEMPOTENT)
def layer_create(
    name: str | None = None, color: int | None = None, linetype: str | None = None
) -> dict[str, Any]:
    """Create a layer (no-op if it already exists) with an ACI colour and linetype.

    With no name, creates the full standard architectural layer set
    (A-WALL, A-DOOR, ...) instead.
    """
    be = _state.require()
    if name is None:
        return {"ok": True, "layers": ops.setup_layers(be)}
    be.ensure_layer(name, color=color, linetype=linetype)
    return {"ok": True, "layer": name}


@mcp.tool(annotations=_IDEMPOTENT)
def set_text_style(
    name: str = "HZ", font: str = "simsun.ttc", big_font: str | None = None
) -> dict[str, Any]:
    """Point drawn text at a text style with a font that has CJK glyphs.

    A stock AutoCAD drawing's Standard style is txt.shx with no big font, and SHX
    fonts carry no Chinese characters, so text drawn without this comes out as
    "?". TrueType (the default here) needs no big font; pass both a font and a
    big font (e.g. gbenor.shx with gbcbig.shx) to match a client's own standard.

    The default is SimSun because it was measured: SimHei, the obvious other choice,
    has no superscript-two glyph, so every `m²` label came out as a box. Repointing a
    style does not repaint text already drawn, which is why the backends issue a regen.
    """
    be = _state.require()
    setter = getattr(be, "set_text_style", None)
    if setter is None:
        raise BackendError(f"backend {be.name!r} has no text styles")
    return {"ok": True, "style": setter(name, font, big_font)}


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_line(p1: Any, p2: Any, layer: str = "0", linetype: str | None = None) -> dict[str, Any]:
    """Draw a straight line between two points."""
    a, b = _pt(p1), _pt(p2)
    params = {"p1": a, "p2": b, "layer": layer, "linetype": linetype}
    with _auto_step("draw_line", params) as be:
        be.line(a, b, layer=layer, linetype=linetype)
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_polyline(points: Any, closed: bool = False, layer: str = "0") -> dict[str, Any]:
    """Draw a polyline through a list of points."""
    pts = _pts(points)
    params = {"points": pts, "closed": closed, "layer": layer}
    with _auto_step("draw_polyline", params) as be:
        ops.draw_polyline(be, pts, closed=closed, layer=layer)
    return {"ok": True, "vertices": len(pts), "closed": closed}


@mcp.tool(annotations=_MUTATING)
def draw_rect(corner1: Any, corner2: Any, layer: str = "0") -> dict[str, Any]:
    """Draw an axis-aligned rectangle from two opposite corners."""
    a, b = _pt(corner1), _pt(corner2)
    params = {"corner1": a, "corner2": b, "layer": layer}
    with _auto_step("draw_rect", params) as be:
        be.polyline(geom.rect_corners(a, b), closed=True, layer=layer)
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_circle(
    center: Any,
    radius: float,
    start_deg: float | None = None,
    end_deg: float | None = None,
    layer: str = "0",
) -> dict[str, Any]:
    """Draw a circle — or an arc, when start_deg and end_deg are given.

    Angles are degrees, counter-clockwise from +X.
    """
    if radius <= 0:
        raise ValueError(f"a circle needs a positive radius, got {radius}")
    c = _pt(center)
    params = {
        "center": c,
        "radius": float(radius),
        "start_deg": start_deg,
        "end_deg": end_deg,
        "layer": layer,
    }
    with _auto_step("draw_circle", params) as be:
        if start_deg is None or end_deg is None:
            be.circle(c, float(radius), layer=layer)
        else:
            be.arc(c, float(radius), float(start_deg), float(end_deg), layer=layer)
    return {"ok": True}


# ---------------------------------------------------------------------------
# walls, rooms, openings
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_wall(
    points: Any,
    thickness: float = layers.WALL_THICKNESS,
    closed: bool = False,
    hatch: bool = False,
    layer: str = "A-WALL",
) -> dict[str, Any]:
    """Draw a wall as two offset faces along a centreline polyline.

    Set closed=True for a room envelope, hatch=True to poché the wall.
    thickness=120 makes a light internal partition.
    """
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
    # A closed ring's closing edge is a segment too; len(pts) - 1 undercounts it,
    # and the ring length is only a perimeter when the repeat leg is included.
    ring = pts[:-1] if closed and geom.is_closed(pts) else pts
    measured = [*ring, ring[0]] if closed else ring
    return {
        "ok": True,
        "segments": len(measured) - 1,
        "thickness": float(thickness),
        "length": round(geom.polyline_length(measured), 3),
    }


@mcp.tool(annotations=_MUTATING)
def draw_wall_with_openings(
    points: Any,
    openings: Any,
    thickness: float = layers.WALL_THICKNESS,
    hatch: bool = False,
    closed: bool = False,
) -> dict[str, Any]:
    """Draw a wall run and cut door/window openings into it in one step.

    `openings` is a list of {"start": mm along the run, "width": mm,
    "kind": "door"|"window"|"opening", "swing": "left"|"right", "tag": "M1522"}.
    This is the tool to reach for when reproducing a real plan: it keeps the
    wall faces and the opening symbols consistent.

    Pass `closed=True` for a building envelope, with `points` as the ring: the corners
    are then mitred into each other. Drawing the envelope as four separate runs instead
    leaves a half-wall-thickness notch at every corner, because a run has two square-cut
    ends and each one lands on a corner.

    The returned `symbols` counts every entity of every symbol, tag texts
    included, so a tagged door counts more than an untagged one.

    The reply's `opening_positions` lists each opening actually cut, with the
    caller's own `start`, the width that was drawn (trimmed if it ran off the
    end), and `at` — the absolute point of the opening's centre — so a
    mis-measured start is caught against intent rather than discovered on paper.
    """
    pts = _pts(points)
    params = {
        "points": pts,
        "openings": openings,
        "thickness": float(thickness),
        "hatch": hatch,
        "closed": closed,
    }
    with _auto_step("draw_wall_with_openings", params) as be:
        result = ops.draw_wall_with_openings(
            be, pts, openings, float(thickness), hatch=hatch, closed=closed
        )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def draw_room(
    boundary: Any,
    name: str | None = None,
    thickness: float = layers.WALL_THICKNESS,
    hatch: bool = False,
    show_area: bool = True,
    bound_layer: str | None = None,
    walls: bool = True,
    label_offset: float = 0.0,
) -> dict[str, Any]:
    """Wall a room in, then label it with its name and computed area in m².

    `boundary` is the wall centreline ring as a list of points. Pass `bound_layer`
    (e.g. "A-ROOM-BOUND") to also draw the face the area is measured to. With
    walls=False only the label (and optional bound face) is drawn — for a room
    whose walls exist already. `thickness` must match the walls actually drawn:
    the default 200 against a 120 mm partition ring silently shrinks the area.

    The label sits on the room's centroid; `label_offset` (mm) raises it along +Y
    when a stair or a piece of equipment occupies the middle of the room.
    """
    pts = _pts(boundary)
    params = {
        "boundary": pts,
        "name": name,
        "thickness": float(thickness),
        "hatch": hatch,
        "show_area": show_area,
        "bound_layer": bound_layer,
        "walls": walls,
        "label_offset": float(label_offset),
    }
    # The name is the step's label as well as a parameter, so the history reads
    # "draw_room 测试室" rather than a wall of coordinates.
    with _auto_step("draw_room", params, label=name) as be:
        if walls:
            result = ops.draw_room(
                be,
                pts,
                name,
                float(thickness),
                hatch=hatch,
                show_area=show_area,
                label_offset=float(label_offset),
                bound_layer=bound_layer,
            )
        else:
            result = ops.add_room_label(
                be,
                pts,
                name,
                float(thickness),
                show_area=show_area,
                label_offset=float(label_offset),
                bound_layer=bound_layer,
            )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def draw_door(
    hinge: Any,
    width: float = layers.DOOR_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    rotation: float = 0.0,
    open_angle: float = 90.0,
    swing: str = "left",
    tag: str | None = None,
) -> dict[str, Any]:
    """Draw a single-leaf door: jambs, leaf and swing arc.

    `hinge` is the hinge-side jamb on the wall centreline; `rotation` is the wall
    direction in degrees; swing is "left" or "right".
    """
    hinge_pt = _pt(hinge)
    params = {
        "hinge": hinge_pt,
        "width": float(width),
        "wall_thickness": float(wall_thickness),
        "rotation": float(rotation),
        "open_angle": float(open_angle),
        "swing": swing,
        "tag": tag,
    }
    with _auto_step("draw_door", params) as be:
        ops.draw_door(
            be,
            hinge_pt,
            float(width),
            float(wall_thickness),
            rotation=float(rotation),
            open_angle=float(open_angle),
            swing=swing,
            tag=tag,
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_window(
    start: Any,
    width: float = layers.WINDOW_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    rotation: float = 0.0,
    tag: str | None = None,
) -> dict[str, Any]:
    """Draw a window symbol spanning an opening, starting at the given jamb."""
    start_pt = _pt(start)
    params = {
        "start": start_pt,
        "width": float(width),
        "wall_thickness": float(wall_thickness),
        "rotation": float(rotation),
        "tag": tag,
    }
    with _auto_step("draw_window", params) as be:
        ops.draw_window(
            be, start_pt, float(width), float(wall_thickness), rotation=float(rotation), tag=tag
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_column(
    center: Any,
    size: float = layers.COLUMN_SIZE,
    depth: float | None = None,
    rotation: float = 0.0,
    hatch: bool = True,
) -> dict[str, Any]:
    """Draw a structural column as a rotated, optionally filled rectangle."""
    c = _pt(center)
    params = {
        "center": c,
        "size": float(size),
        "depth": depth,
        "rotation": float(rotation),
        "hatch": hatch,
    }
    with _auto_step("draw_column", params) as be:
        ops.draw_column(be, c, float(size), rotation=float(rotation), depth=depth, hatch=hatch)
    return {"ok": True}


# ---------------------------------------------------------------------------
# core & equipment
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_stair(
    corner1: Any,
    corner2: Any,
    steps: int = 9,
    axis: str = "x",
    up_label: str | None = "\u4e0a",
    down_label: str | None = "\u4e0b",
) -> dict[str, Any]:
    """Draw a stair flight: outline, treads, direction arrow and 上/下 labels.

    `axis` is the travel direction ("x" or "y").
    """
    a, b = _pt(corner1), _pt(corner2)
    params = {
        "corner1": a,
        "corner2": b,
        "steps": int(steps),
        "axis": axis,
        "up_label": up_label,
        "down_label": down_label,
    }
    with _auto_step("draw_stair", params) as be:
        result = ops.draw_stair(
            be,
            a,
            b,
            steps=int(steps),
            axis=axis,
            up_label=up_label,
            down_label=down_label,
        )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def draw_lift(corner1: Any, corner2: Any, tag: str | None = None) -> dict[str, Any]:
    """Draw a lift/elevator shaft with its car outline and X cross."""
    a, b = _pt(corner1), _pt(corner2)
    params = {"corner1": a, "corner2": b, "tag": tag}
    with _auto_step("draw_lift", params) as be:
        ops.draw_lift(be, a, b, tag=tag)
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def draw_equipment(
    corner1: Any, corner2: Any, name: str | None = None, kind: str = "equipment"
) -> dict[str, Any]:
    """Draw a rectangular piece of fixed equipment with a caption.

    kind="shaft" draws a service riser instead (强电井/弱电井/水井): the same
    captioned rectangle plus a diagonal cross, on A-SHAFT.

    `kind` is a closed set for the same reason `swing` and `axis` are: a misspelt
    "shfts" drew an ordinary equipment box and said ok, so a riser silently became
    a machine.
    """
    if kind not in ("equipment", "shaft"):
        raise ValueError(f"kind must be 'equipment' or 'shaft', got {kind!r}")
    a, b = _pt(corner1), _pt(corner2)
    params = {"corner1": a, "corner2": b, "name": name, "kind": kind}
    with _auto_step("draw_equipment", params) as be:
        if kind == "shaft":
            ops.draw_shaft(be, a, b, name=name)
        else:
            ops.draw_equipment(be, a, b, name=name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_MUTATING)
def draw_axis_grid(
    x_positions: Any,
    y_positions: Any,
    extent: Any,
    x_labels: Any = None,
    y_labels: Any = None,
    bubble_r: float | None = None,
) -> dict[str, Any]:
    """Draw the structural grid: centre lines, bubbles and axis labels.

    `extent` is [min_x, min_y, max_x, max_y]. x_labels default to 1,2,3...,
    y_labels to A,B,C... `bubble_r` defaults to 4 mm of paper at the sheet's
    plot scale (400 mm at 1:100); the axis label inside is the standard's 5 mm
    paper height, whatever the scale.
    """
    xs = [float(v) for v in x_positions]
    ys = [float(v) for v in y_positions]
    ex = [float(v) for v in extent]
    if len(ex) != 4:
        raise ValueError(f"extent needs [min_x, min_y, max_x, max_y], got {len(ex)} numbers")
    params = {
        "x_positions": xs,
        "y_positions": ys,
        "extent": ex,
        "x_labels": x_labels,
        "y_labels": y_labels,
        "bubble_r": None if bubble_r is None else float(bubble_r),
    }
    with _auto_step("draw_axis_grid", params) as be:
        result = ops.draw_axis_grid(
            be,
            xs,
            ys,
            x_labels=x_labels,
            y_labels=y_labels,
            extent=(ex[0], ex[1], ex[2], ex[3]),
            bubble_r=None if bubble_r is None else float(bubble_r),
        )
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def add_text(
    pos: Any,
    text: str,
    height: float | None = None,
    rotation: float = 0.0,
    align: str = "left",
    layer: str = "A-ANNO",
) -> dict[str, Any]:
    """Place single-line text. align is left, center, right or top-center.

    `height` defaults to the standard's 3.5 mm paper height at the sheet's plot
    scale (350 mm at 1:100) — pass a value only to override it deliberately.
    """
    pos_pt = _pt(pos)
    params = {
        "pos": pos_pt,
        "text": text,
        "height": None if height is None else float(height),
        "rotation": float(rotation),
        "align": align,
        "layer": layer,
    }
    with _auto_step("add_text", params) as be:
        ops.annotate(
            be,
            pos_pt,
            text,
            height=None if height is None else float(height),
            rotation=float(rotation),
            layer=layer,
            align=align,
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def add_leader_note(
    anchor: Any,
    text: str,
    direction: float = 45.0,
    length: float = 1500.0,
    height: float | None = None,
) -> dict[str, Any]:
    """Draw a one-segment leader from `anchor` with the note at its far end.

    Anchor on the boundary of what is being noted: an anchor inside a SOLID
    hatch disappears under the fill. `height` defaults to the standard's 3.5 mm
    paper height at the sheet's plot scale (350 mm at 1:100).
    """
    anchor_pt = _pt(anchor)
    params = {
        "anchor": anchor_pt,
        "text": text,
        "direction": float(direction),
        "length": float(length),
        "height": None if height is None else float(height),
    }
    with _auto_step("add_leader_note", params) as be:
        ops.leader_note(
            be,
            anchor_pt,
            text,
            direction=float(direction),
            length=float(length),
            height=None if height is None else float(height),
        )
    return {"ok": True}


@mcp.tool(annotations=_MUTATING)
def add_dimension(points: Any, offset: float = layers.DIM_OFFSET) -> dict[str, Any]:
    """Add an aligned dimension, offset sideways by `offset` mm.

    Two points give a single dimension; three or more give a chain along the
    run, plus the overall dimension. The dimension line lands on the LEFT of
    the travel direction for a positive `offset` — a south edge dimensioned
    east-to-west therefore takes a *positive* offset to fall outside the
    building. Either point order draws that same line; a reversed span is
    normalised before drawing so the value never reads mirrored.
    """
    pts = _pts(points)
    params = {"points": pts, "offset": float(offset)}
    with _auto_step("add_dimension", params) as be:
        if len(pts) == 2:
            ops.dimension_between(be, pts[0], pts[1], float(offset))
            result: dict[str, Any] = {}
        else:
            result = ops.dimension_chain(be, pts, float(offset))
    return {"ok": True, **result}


@mcp.tool(annotations=_MUTATING)
def add_hatch(
    points: Any,
    pattern: str = "ANSI31",
    scale: float = 40.0,
    angle: float = 0.0,
    solid: bool = False,
    layer: str = "A-HATCH",
) -> dict[str, Any]:
    """Hatch a closed boundary given as points (pattern, or solid fill).

    `scale` multiplies the pattern's own base spacing, which differs per
    pattern: ANSI31's base is ≈5 mm, so scale=40 gives the ≈130 mm pitch of a
    wall poché, while AR-HBONE starts far larger and wants a single-digit
    scale. `angle` is added to the pattern's own angle — ANSI31 is already
    45°, so angle=45 makes it vertical. `pattern` must be one of the standard
    acad.pat names (ANSI31…ANSI38, AR-*, NET, …); a misspelt name raises
    rather than drawing a pattern that does not exist.
    """
    pts = _pts(points)
    if not solid and pattern.upper() not in layers.HATCH_PATTERNS:
        raise ValueError(
            f"unknown hatch pattern {pattern!r}; the standard names are "
            "ANSI31…ANSI38, AR-*, NET, SOLID and the rest of acad.pat"
        )
    params = {
        "points": pts,
        "pattern": pattern,
        "scale": float(scale),
        "angle": float(angle),
        "solid": solid,
        "layer": layer,
    }
    with _auto_step("add_hatch", params) as be:
        be.hatch(pts, pattern, float(scale), layer=layer, angle=float(angle), solid=solid)
    return {"ok": True, "pattern": "SOLID" if solid else pattern}


# --------------------------------------------------------------------------- #
# The in-AutoCAD step plugin
# --------------------------------------------------------------------------- #
#
# Drawing through the plugin is not like drawing through the other backends: a step
# has to be opened and closed around the drawing calls, so that everything in
# between becomes one reviewable step rather than a pile of loose entities. The
# usual drawing tools do the work; these open, close and drive the journal.

_plugin_client: PluginClient | None = None


def _client() -> PluginClient:
    """The one connection to the plugin, created on first use."""
    global _plugin_client
    if _plugin_client is None:
        _plugin_client = PluginClient()
    return _plugin_client


def _plugin_backend_connect(new_document: bool = False) -> PluginBackend:
    """Attach to the plugin, optionally opening a fresh drawing in AutoCAD."""
    try:
        client = _client().connect()
        if new_document:
            client.call("new_document")
    except PluginUnavailable as exc:
        raise BackendError(
            f"{exc}. Install the plugin with plugin_install, start AutoCAD, and then "
            "check plugin_status."
        ) from exc

    backend = PluginBackend(client=client)
    backend.forget_layers()
    backend.set_units_mm()
    return backend


def _require_plugin() -> PluginBackend:
    backend = _state.backend
    if not isinstance(backend, PluginBackend):
        raise BackendError(
            "the step tools draw through the plugin; call acad_connect(mode='plugin') first"
        )
    return backend


def _entry(key: str) -> Element:
    """The catalogue entry for this key, or a refusal that lists what there is."""
    element = catalog.current().get(key)
    if element is None:
        raise BackendError(
            f"no element '{key}' in the catalogue; it has: {', '.join(catalog.current().keys())}"
        )
    return element


def _placement_backend() -> Backend:
    """The drawing a placement acts on, refused rather than assumed when there is none.

    The primitive tools start a scratch DXF when nothing is open, because a stray line has
    nowhere else to go. A placement is a different question: which half of the catalogue
    entry reaches the paper — the library block or its parametric twin — and what the
    reply's step and journal look like both depend on the backend, so the backend is the
    caller's to name rather than one to invent on its behalf.
    """
    be = _state.backend
    if be is None:
        raise BackendError(
            "no drawing is open, so there is nowhere to place this element; start one with "
            "drawing_new(backend='dxf') to write a plan headless, or call "
            "acad_connect(mode='plugin') to place it into AutoCAD"
        )
    return be


def _placed(be: Backend, op: str, label: str, params: dict[str, Any], work: Any) -> dict:
    """Run one placement call as a single step, on whichever backend is active.

    The two journal owners answer differently and that is deliberate: the plugin's commit
    replies with its own record of the step (the DWG journal is the source of truth there),
    while a local journal commits to a `StepRecord`. Both are flattened to the same keys, so
    a caller does not have to know which backend it is talking to — which is what lets one
    element name be placed headless and replayed into AutoCAD.

    An explicit step (step_begin) is not this call's to close, so when one is open the work
    is recorded into it and nothing commits; the same rule `_auto_step` follows.
    """
    begin = getattr(be, "begin_step", None)
    if begin is None or getattr(be, "open_step", ""):
        summary = work()
        return {"ok": True, **(summary if isinstance(summary, dict) else {})}
    _ensure_catalog_stamp(be)
    begin(op, label=label, params=params)
    try:
        summary = work()
    except BaseException:
        be.abort_step()
        raise
    committed = be.commit_step()
    if isinstance(committed, dict):
        outcome = _outcome(committed, "apply")
    else:
        outcome = {
            "ok": True,
            "action": "commit",
            "message": "",
            "touched": [],
            "step": {"index": committed.index, "op": committed.op, "label": committed.label},
            "refusal": None,
            "journal": dict(be.info().get("journal", {})),
        }
    return {**outcome, **(summary if isinstance(summary, dict) else {})}


def _placement(be: Backend, op: str, label: str, params: dict[str, Any], work: Any) -> dict:
    """Like `_placed`, but a bad catalogue argument comes back as a refusal to the caller.

    A variant name that does not exist is a caller's mistake, not a drawing failure, and the
    catalogue raises to say so; this is where that becomes a message rather than a traceback.
    """
    try:
        return _placed(be, op, label, params, work)
    except ValueError as exc:
        raise BackendError(str(exc)) from exc


def _outcome(response: dict[str, Any], action: str) -> dict[str, Any]:
    """Flatten the plugin's reply into something a reader can use."""
    result = response.get("result", {})
    return {
        "ok": bool(result.get("ok")),
        "action": result.get("action", action),
        "message": result.get("message", ""),
        "touched": result.get("touched", []),
        "step": result.get("step"),
        "refusal": result.get("refusal"),
        "journal": response.get("status", {}),
    }


@mcp.tool(annotations=_READONLY)
def plugin_status(with_steps: bool = True) -> dict[str, Any]:
    """Report the in-AutoCAD step plugin: whether it is there, and what it holds.

    This answers even while AutoCAD is busy — the plugin's ping does not touch the
    drawing — so it is also how "AutoCAD is stuck" is told apart from "the plugin is
    not loaded".
    """
    try:
        pong = _client().ping()
    except PluginError as exc:
        return {"ok": False, "available": False, "reason": str(exc)}

    session = StepSession(_client())
    try:
        status = session.status()
    except PluginError as exc:
        return {"ok": False, "available": True, "reason": str(exc), **pong}

    out: dict[str, Any] = {"ok": True, "available": True, **pong, **status}
    if with_steps:
        out["steps"] = session.steps()
        out["rendered"] = describe_steps(out["steps"])
    return out


@mcp.tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def plugin_install(force: bool = False) -> dict[str, Any]:
    """Build and install the AutoCAD plugin into the per-user plug-in folder.

    Windows only, and only from a checkout of this repository: it runs
    plugin/scripts/install.sh, which needs the .NET 8 SDK and wants AutoCAD closed
    (pass force=True to let it close a running AutoCAD). It also adds the folder to
    AutoCAD's TRUSTEDPATHS, without which an unsigned plugin raises a security
    prompt on every load.
    """
    script = Path(__file__).resolve().parents[2] / "plugin" / "scripts" / "install.sh"
    if not script.is_file():
        return {
            "ok": False,
            "reason": (
                f"no installer at {script}: plugin_install needs a checkout of this "
                "repository, not an installed copy of the package"
            ),
        }

    command = ["bash", str(script)] + (["--force"] if force else [])
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": f"could not run {script.name}: {exc}"}
    return {
        "ok": done.returncode == 0,
        "returncode": done.returncode,
        "output": (done.stdout + done.stderr).strip(),
    }


@mcp.tool(annotations=_MUTATING)
def step_begin(
    op: str, label: str | None = None, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Open a step: everything drawn until step_commit becomes that one step.

    `op` names the operation the step came from and `params` records the arguments
    that produced it, so the step can be read back and reproduced later. Open a step,
    call draw_room, then step_commit — and the room is one step the user can roll
    back in a single action.
    """
    backend = _require_plugin()
    backend.begin_step(op, label=label, params=params)
    return {
        "ok": True,
        "op": op,
        "label": label or "",
        "hint": "draw with the ordinary tools, then call step_commit",
    }


@mcp.tool(annotations=_MUTATING)
def step_commit(planned: bool = False) -> dict[str, Any]:
    """Close the step opened by step_begin.

    planned=False (the default) draws it now and journals it as applied.
    planned=True only records it, to be drawn later — by the user pressing Run next
    in the palette, or by step_control("run_next") — which is how a whole plan is
    laid out first and drawn one step at a time.
    """
    backend = _require_plugin()
    if not backend.pending:
        return {
            "ok": False,
            "reason": (
                f"step {backend.open_step or '(none)'} recorded no primitives: nothing "
                "was drawn between step_begin and step_commit"
            ),
        }
    response = backend.plan_step() if planned else backend.commit_step()
    return _outcome(response, "plan" if planned else "apply")


@mcp.tool(annotations=_READONLY)
def step_list() -> dict[str, Any]:
    """List the steps in the drawing's journal, oldest first."""
    try:
        rows = StepSession(_client().connect()).steps()
    except PluginError as exc:
        raise BackendError(str(exc)) from exc
    return {"ok": True, "count": len(rows), "steps": rows, "rendered": describe_steps(rows)}


@mcp.tool(annotations=_DESTRUCTIVE)
def step_control(
    action: str,
    index: int | None = None,
    force: bool = False,
    note: str | None = None,
    patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive the step journal.

    Actions, and what each one needs:
      run_next / run_all   — run the next step, or every remaining one
      run_to               — index: run up to and including that step
      rollback_to          — index: erase everything after it, back to planned
      reject               — index: drop it and everything after it
      accept / unaccept    — index: mark reviewed, so a rollback will not pass it
      note                 — index, note: annotate a step
      update               — index, patch: change a step's params, label or note
      snapshot             — record a reviewed baseline at the end
      clear_plan / reset / reload

    rollback_to is refused when an accepted step stands in the way; force overrides
    that refusal, and the result says so.
    """
    session = StepSession(_client().connect())
    try:
        if action == "rollback_to":
            if index is None:
                raise BackendError("rollback_to needs an index")
            response = session.rollback_to(int(index), force=bool(force))
        else:
            arguments: dict[str, Any] = {}
            if index is not None:
                arguments["index"] = int(index)
            if note is not None:
                arguments["note"] = note
            if patch is not None:
                arguments["patch"] = patch
            response = session.control(action, **arguments)
    except (PluginError, ValueError) as exc:
        raise BackendError(str(exc)) from exc
    return _outcome(response, action)


@mcp.tool(annotations=_READONLY)
def library_list(source: str | None = None, limit: int = 500, offset: int = 0) -> dict[str, Any]:
    """List the block definitions in the open drawing, or in a library .dwg.

    This is what the element catalogue is written against, so it is also how a catalogue
    that has drifted from the library is caught. Dynamic blocks come back with their
    parameters, the values each accepts and which are read-only. A library file is opened
    read-only on the side and never becomes a reference to it.

    Pages with `limit` and `offset`: `has_more` says the page was cut short and
    `next_offset` is where the next page starts. `total_count` is the full count
    once a page reaches the end; before that it is None, because the plugin's
    blocks call reports no library-wide total without reading the whole file.
    """
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    if int(offset) < 0:
        raise ValueError(f"offset must be at least 0, got {offset}")
    try:
        result = _client().call("blocks", source=source, limit=int(offset) + int(limit) + 1)
    except (PluginError, PluginUnavailable) as exc:
        raise BackendError(str(exc)) from exc
    blocks = result.get("blocks", [])
    page = blocks[int(offset) : int(offset) + int(limit)]
    has_more = len(blocks) > int(offset) + int(limit)
    result["blocks"] = page
    result["count"] = len(page)
    result["offset"] = int(offset)
    result["has_more"] = has_more
    result["next_offset"] = int(offset) + len(page) if has_more else None
    result["total_count"] = len(blocks) if not has_more else None
    return {"ok": True, **result}


@mcp.tool(annotations=_READONLY)
def element_types(category: str | None = None, check_library: bool = True) -> ElementTypesReply:
    """What the element catalogue offers, and whether the library still matches it.

    The catalogue is a plain JSON file naming, for each element, which library block stands
    for it and which parameters select a size. A project supplies its own file — an
    institute's model numbers and wall thicknesses are its own — and this reports anything
    that could not be read, anything it had to skip, and every entry whose block the
    library has not got.

    An entry placed by `insert_captured` also carries `fragment_ready`: whether its geometry
    fragment resolves to a file on disk. False means the drawing that captured the element
    can still place it, but a fresh drawing cannot until it is published (element_publish).
    """
    loaded = catalog.current()
    out: dict[str, Any] = {
        "ok": True,
        "count": len(loaded.elements),
        "skipped": loaded.skipped,
        "library": {str(path): library for path, library in loaded.libraries.items()},
        "categories": {
            name: [_describe_element(loaded, element) for element in items]
            for name, items in loaded.by_category(category).items()
        },
    }
    if check_library:
        problems = _check_catalog(loaded)
        out["problems"] = problems
        out["matches_library"] = not problems
        if any("not reachable" in problem for problem in problems):
            # "could not check" is not "fine", and `placement` is not what the check was
            # about: it says how each entry can be drawn, which is a fact about the entry,
            # not about the backend this session happens to be on.
            out["placement_note"] = (
                "the library could not be checked, but `placement` is a property of each "
                "catalogue entry, not of the active backend"
            )
    return out


def _describe_element(loaded: catalog.Catalog, element: catalog.Element) -> dict[str, Any]:
    """One catalogue row, with `fragment_ready` added where a fragment exists to be ready."""
    row = element.describe()
    ready = loaded.fragment_ready(element)
    if ready is not None:
        row["fragment_ready"] = ready
    return row


def _check_catalog(loaded: catalog.Catalog) -> list[str]:
    """Compare the catalogue against the libraries it names, reading each one once."""
    try:
        client = _client()
    except (PluginError, PluginUnavailable):
        return ["AutoCAD is not reachable, so the catalogue could not be checked"]

    known: dict[str, list[str]] = {}
    for source in loaded.sources():
        if source in known:
            continue
        try:
            known[source] = [
                block["name"] for block in client.call("blocks", source=source)["blocks"]
            ]
        except PluginError as exc:
            return [f"could not read '{source}': {exc}"]
    return loaded.check_against(known)


@mcp.tool(annotations=_MUTATING)
def place_element(
    type: str,  # noqa: A002 - the tool surface speaks the caller's language
    at: Any,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    tag: bool = True,
    tag_offset: float | None = None,
) -> dict[str, Any]:
    """Place one catalogue element, as a single step, on whatever backend is active.

    Where the backend can insert library blocks — the plugin — the block's definition is
    imported into the drawing if it is not there yet and the reference is placed at `at`.
    Everywhere else the entry's parametric twin is drawn instead, so the same element name
    works headless: a plan can be written in DXF with no AutoCAD, then replayed into it.

    `variant` picks a size from the catalogue ("900" for a 900 mm door) and `params`
    overrides individual properties on top. With tag=True the next free tag number is
    derived from the tags already in the drawing — numbers are never stored, so a tag
    added by hand cannot desynchronise them.

    `at` is the element's *own* reference point, and it differs per kind: a door's is its
    **hinge** (the jamb end of the opening, not its centre), a window's is the opening's
    centre, a column's its centre, a lift's its corner. When placing into openings cut by
    draw_wall_with_openings, prefer that tool's `element` key — it does the hinge maths
    itself — or read the jamb off the reply's `opening_positions`.
    """
    be = _placement_backend()
    element = _entry(type)

    def work() -> dict[str, Any]:
        tags = ops._numbering(be, element, 1) if tag else []
        ops.place_element(
            be,
            element,
            geom.xy(at),
            variant=variant,
            params=params,
            rotation=float(rotation),
            scale=float(scale),
            tag=tags[0] if tags else None,
            tag_offset=tag_offset,
        )
        return {"placed": 1, "tag": tags[0] if tags else None}

    return _placement(
        be,
        "place_element",
        f"{element.label} {variant or element.default_variant}".strip(),
        {
            "type": element.key,
            "at": geom.xy(at),
            "variant": variant,
            "params": params or {},
            "rotation": float(rotation),
            "scale": float(scale),
            "tag": tag,
        },
        work,
    )


@mcp.tool(annotations=_MUTATING)
def place_along(
    type: str,  # noqa: A002
    start: Any,
    end: Any,
    count: int | None = None,
    spacing: float | None = None,
    inset: float = 0.0,
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float | None = None,
    tag: bool = True,
) -> dict[str, Any]:
    """Repeat a catalogue element along a run, as one step.

    Give either `count` (that many, evenly spread, ends included) or `spacing` (every that
    far, starting at `start`). `inset` pulls the ends in, which is what keeps a column off
    a corner. `rotation` defaults to the direction of the run, so a row of doors lies in
    the wall rather than all facing the same way.
    """
    be = _placement_backend()
    element = _entry(type)
    return _placement(
        be,
        "place_along",
        f"{len(_positions(start, end, count, spacing, inset))} x {element.label}",
        {
            "type": element.key,
            "start": geom.xy(start),
            "end": geom.xy(end),
            "count": count,
            "spacing": spacing,
            "rotation": None if rotation is None else float(rotation),
            "tag": tag,
        },
        lambda: ops.place_along(
            be,
            element,
            geom.xy(start),
            geom.xy(end),
            count=count,
            spacing=spacing,
            inset=float(inset),
            rotation=None if rotation is None else float(rotation),
            variant=variant,
            params=params,
            tag=tag,
        ),
    )


@mcp.tool(annotations=_MUTATING)
def place_grid(
    type: str,  # noqa: A002
    xs: list[float],
    ys: list[float],
    variant: str | None = None,
    params: dict[str, Any] | None = None,
    rotation: float = 0.0,
    tag: bool = True,
) -> dict[str, Any]:
    """Place a catalogue element at every intersection of two coordinate lists.

    A frame of columns is dimensioned to its axis positions, so those are what this takes:
    the same `xs` and `ys` the axis grid was drawn to.
    """
    be = _placement_backend()
    element = _entry(type)
    return _placement(
        be,
        "place_grid",
        f"{len(xs) * len(ys)} x {element.label}",
        {
            "type": element.key,
            "xs": list(xs),
            "ys": list(ys),
            "rotation": float(rotation),
            "tag": tag,
        },
        lambda: ops.place_grid(
            be,
            element,
            list(xs),
            list(ys),
            variant=variant,
            params=params,
            rotation=float(rotation),
            tag=tag,
        ),
    )


@mcp.tool(annotations=_MUTATING)
def element_capture(
    steps: list[int],
    key: str,
    label: str = "",
    tag_prefix: str = "",
    layer: str = "",
    category: str = "captured",
    base_point: list[float] | None = None,
) -> dict[str, Any]:
    """Capture the geometry of journal steps as a reusable catalogue element.

    Draw once, check, capture: the entities the named `steps` drew become a named block
    definition and a catalogue entry, and from then on `place_element(type=key)` places
    them anywhere — a component that was drawn and verified once is never re-derived.
    `base_point` is the element's own reference point (its insertion origin); omitted, the
    bounding box's min corner. `tag_prefix` joins the auto-numbering (a door's "M").

    The block definition is document state, like the page setup: it is not journalled and
    a rollback never erases it. The catalogue entry travels inside the drawing's journal,
    so a captured element resolves on any machine that opens the file. Only the dxf
    backend creates definitions for now; elsewhere the refusal names the capability.
    """
    be = _state.require()
    if not getattr(be, "supports_block_create", False):
        raise BackendError(
            f"the {be.name} backend cannot create block definitions "
            "(capability: block_create) — capture on the dxf backend"
        )
    if not steps or not all(
        isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in steps
    ):
        raise ValueError(f"steps must be a non-empty list of step indices, got {steps!r}")
    with _auto_step(
        "element_capture",
        {
            "steps": [int(s) for s in steps],
            "key": key,
            "label": label,
            "tag_prefix": tag_prefix,
            "layer": layer,
            "category": category,
        },
        label=key,
    ):
        return {
            "ok": True,
            **capture_ops.capture(
                be,
                steps=[int(s) for s in steps],
                key=key,
                label=label,
                tag_prefix=tag_prefix,
                layer=layer,
                category=category,
                base_point=base_point,
            ),
        }


@mcp.tool(annotations=_MUTATING)
def element_publish(key: str, path: str = "") -> dict[str, Any]:
    """Publish a captured element, and its geometry, into the project library.

    Publishing is what makes the library *accumulate*: `element_capture` is drawing-local —
    the block definition lives in the drawing that captured it, and a fresh drawing has never
    seen it — while a capture written into a project file is something a new drawing can
    place. So two things are written: the catalogue entry, and a geometry fragment under
    ``<catalogue dir>/library/<key>.dxf`` holding the block definition and, in its journal,
    the element's own primitives. From then on `place_element(type=key)` works in a drawing
    that never saw the capture, because `insert_captured` imports the fragment when the
    definition is absent.

    The geometry has to come from the **current drawing's journal**: the definition must be
    one this drawing recorded, so open the drawing that captured it (`drawing_open`) and
    publish from there. A publish from a drawing that never captured the key is refused by
    name rather than writing an entry that resolves to nothing. Only applied capture steps
    count — a capture that was undone is no longer the drawing's to publish.

    `path` names the catalogue file to write; empty resolves to the first project catalogue
    file in `acadmcp.json`, and when neither exists `./elements.json` is created beside a
    `./acadmcp.json` naming it, reported in `created`. The target is **appended to, never
    overwritten or re-sorted**, so publishing twice under one key refuses rather than
    replacing (nothing in the library is ever silently overwritten). The reply is
    `{ok, file, fragment, created, key, digest}`, with both the file and the fragment path
    named so a file appearing in the project is never unannounced. Nothing is drawn, so this
    is not a journal step.
    """
    be = _state.require()
    element = _entry(key)
    # The definition's record in this drawing's journal is the canonical geometry, not the
    # block table — and the plugin keeps its journal in the DWG, so the finder is looked up
    # rather than assumed (a backend without one publishes no geometry, and publish_to says
    # so by name).
    finder = getattr(be, "find_block_primitives", None)
    fragment = finder(element.params.get("block", "")) if finder is not None else None
    if fragment:
        fragment["fingerprint"] = element.origin.get("fingerprint", "")
    # An undone capture and a definition this drawing never had both arrive as no fragment,
    # and their recoveries differ — publish_to cannot see the journal, so answer that here.
    capture_undone = fragment is None and any(
        row.get("op") == "element_capture"
        and row.get("state") == "undone"
        and str((row.get("params") or {}).get("key", "")).strip().lower() == key.strip().lower()
        for row in be.step_rows()
    )
    # publish_to forgets the settings cache after writing, so the catalogue in force is read
    # once here, before the write, and never touched again.
    return catalog.current().publish_to(
        path or None, element, fragment, capture_undone=capture_undone
    )


@mcp.tool(annotations=_READONLY)
def catalog_suggest(min_count: int = 3) -> SuggestReply:
    """Point at the repetition in this drawing, and the capture worth making of it.

    A plan drawn twice — five doors of one size, four identical runs — is a library element
    waiting to be captured, and the journal already records every one of them. This mines
    the current drawing's step rows and reports what repeats: each suggestion carries the
    `op`, how many times it occurred (`count`), the shared `params`, the `steps` to capture,
    and a `hint` naming the literal `element_capture` call (and the `element_publish` that
    would sediment it). Two steps group when they share an op and their parameters are equal
    after canonicalisation — keys sorted, numbers rounded to 0.1 mm — so two doors of a
    different width do not group, and neither do two of the same size at two hinges.

    Read-only and journal-scoped: it suggests, it captures nothing, and it acts on the
    drawing open now. `min_count` is at least 2 (a single step is not a repeat). Steps the
    drawing does not contain — undone, planned, failed — are left out, and a row it cannot
    read is counted in `skipped`, never raised.
    """
    be = _state.require()
    return {"ok": True, **suggest.suggestions(be.step_rows(), min_count=min_count)}


def _positions(start: Any, end: Any, count: int | None, spacing: float | None, inset: float):
    """The points a run would place at, for the step's label. Raises the same way the op does."""
    return catalog.positions_along(
        geom.xy(start), geom.xy(end), count=count, spacing=spacing, inset=float(inset)
    )


def _strict_tool_arguments() -> None:
    """Reject unknown tool arguments instead of dropping them silently.

    The SDK builds each tool's pydantic input model with the default
    extra="ignore", so a misspelt parameter is discarded and the tool runs on
    defaults — a stale server answering a newer client fails exactly that way,
    and the drawing comes out wrong with ok=True. Forbidding extras turns it
    into a validation error that names the field.
    """
    for tool in mcp._tool_manager.list_tools():
        model = getattr(getattr(tool, "fn_metadata", None), "arg_model", None)
        if model is None:
            continue
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)


_strict_tool_arguments()


def _surface_input_errors() -> None:
    """Let a tool's ValueError reach the client with its message intact.

    Bad geometry input — one distinct point for a wall, a room ring that
    self-intersects — is raised by geom/ops as a plain ValueError, and the SDK
    masks anything that is not a ToolError behind "Error executing tool
    <name>", so the model never learns what was wrong with its own arguments.
    Re-raising as BackendError (a ToolError) keeps the message after that
    prefix — the SDK re-wraps even ToolError, so the tool name appears twice
    on the wire, which is cosmetic and not worth fighting. Wrapped here, once,
    rather than in every tool body.
    """
    for tool in mcp._tool_manager.list_tools():
        fn = tool.fn

        def guarded(*args: Any, _fn: Any = fn, **kwargs: Any) -> Any:
            try:
                return _fn(*args, **kwargs)
            except ValueError as exc:
                raise BackendError(str(exc)) from exc

        tool.fn = guarded


_surface_input_errors()


def main() -> None:
    # basicConfig targets stderr, so the audit lines never touch stdout — that
    # channel is the JSON-RPC protocol itself. Import time stays silent on
    # purpose: a client embedding the module configures its own logging.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    mcp.run()


if __name__ == "__main__":
    main()
