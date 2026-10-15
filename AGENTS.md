# AGENTS.md

MCP server that lets an AI client drive AutoCAD to draw 2D architectural floor
plans — either into a live AutoCAD session over ActiveX/COM, or into a headless
DXF file. Published at https://github.com/LBurny/autocad.

## Layout

```
src/acadmcp/
  geom.py               pure 2D geometry: offsets, mitre joins, areas. No CAD imports.
  layers.py             drafting standard: layer names, ACI colours, text heights, mm sizes
  ops.py                architectural operations (walls, doors, rooms, cores, annotation)
  catalog.py            the element catalogue: entries, variants, identity
  catalog_ops.py        the parametric twins a catalogue entry draws where blocks cannot insert
  summary.py            derives a design summary (rooms, openings, elements) from the journal.
                        No CAD imports — it may import geom and layers and nothing else.
  verify.py             the drawing reconciled against the journal's own account of it.
                        No CAD imports, and none from this package either.
  critique.py           the components checked against the journal (doors on walls, tags,
                        masquerading text, sheet fit). Pure — geom, layers and sheet only.
  capture_ops.py        element capture: journal steps become a block definition plus a
                        catalogue entry, so a verified component is never re-derived.
  standards.py          the drafting standard as data: GB/T tables with sources, a lookup
                        that refuses by name, and check() over a drawing's snapshot. Pure —
                        layers and sheet only, no CAD.
  templates.py          the shipped templates: the standard, paper, scale and title-block
                        fields a drawing starts from. Pure, no CAD.
  suggest.py            repetition mining over the step rows: what was drawn the same way
                        twice is a library element waiting to be captured. Pure, no CAD.
  data/                 the shipped data: elements.json (the catalogue), standards.json
                        (GB/T tables) and templates.json (the two template specifications)
  terms.py              the tool vocabulary: the AutoCAD command and the Chinese phrase for
                        each tool. Data plus lookup; no CAD imports.
  prompts.py            the MCP prompt templates (floor_plan, headless_to_live,
                        drawing_review). Pure text; no CAD imports.
  sheet.py              the sheet: paper sizes, plot scales, frame and title-block geometry,
                        and the PDF paper-size read-back. Pure — it imports layers for the
                        measured character width and nothing else, and no CAD library.
  capture.py            Win32 window capture, and finding the AutoCAD frame
  server.py             the MCP tool surface (56 tools, each annotated by behaviour class),
                        the audit middleware and the server instructions
  journal.py            the backend-neutral step journal (undo/redo bookkeeping). No CAD imports.
  acad_plugin_client.py the socket client for the in-AutoCAD plugin
  steps.py              step-journal control, and the step_control action table
  backends/
    base.py             the Backend contract every driver implements
    recording.py        the batching layer: steps, undo/redo, replay — shared by all three drivers
    dxf.py              ezdxf driver — no AutoCAD needed, renders PNG; journals into the file
    com.py              ActiveX driver — draws into a running AutoCAD; journals in memory
    plugin.py           records primitives per step and hands them to the plugin
examples/floor_plan_demo.py
templates/              the committed template artefacts (gb_a3_arch.dxf, gb_a4_arch.dxf),
                        built by scripts/build_templates.py, never hand-drawn
scripts/check_doc_numbers.py   every quotable number, derived from the tree; non-zero on drift
scripts/build_templates.py     builds and gates the template artefacts (fingerprint, standard)
evaluations/            read-only QA pairs + fixture DXF + harness, per the mcp-builder guide
tests/                  test_geom.py, test_backends.py, test_journal.py, test_recording.py,
                        test_dxf_journal.py, test_plugin_backend.py, test_server_journal.py,
                        test_server_summary.py, test_plugin_client.py, test_com_backend.py,
                        test_catalog.py, test_catalog_ops.py, test_catalog_identity.py,
                        test_summary.py, test_element_tools.py,
                        test_tool_surface.py (annotations, naming, pagination),
                        test_verify.py (the journal's count against the drawing's),
                        test_terms.py (every tool has a vocabulary entry),
                        test_prompts.py (the prompt registry against the tool registry),
                        test_audit_middleware.py (tool-call logging, the instructions),
                        test_critique.py (component checks against the journal),
                        test_capture.py (element capture: block, catalogue, reopen),
                        test_finalize_gate.py (the three check/capture tools, wired),
                        test_standards.py (GB/T tables as data, and the conformance check),
                        test_templates.py (the template registry and the artefact gate),
                        test_publish.py (capture → publish → place in a fresh drawing),
                        test_suggest.py (repetition mined from the journal),
                        test_doc_numbers.py (the docs and the evaluation answers),
                        test_sheet.py (paper, scale, frame, title block, PDF read-back),
                        test_sheet_backend.py (the page setup and paperspace, read back),
                        test_sheet_plot.py (the plotted PDF: size, scale, refusals),
                        test_dxf_create_render.py (drawing_new create, theme colours, glyphs),
                        test_input_validation.py (caller mistakes surface as readable errors),
                        test_live_screenshot.py (no restore, no rename on export),
                        sandbox_driver.py (fresh-server sandbox for live drawing exercises),
                        sandbox_library_check.py (draw → check → suggest → capture → publish
                        → place in a fresh drawing, over the wire),
                        sandbox_prompts_check.py / sandbox_audit_check.py /
                        sandbox_correctness_check.py (what it drives: the prompts and
                        instructions over the wire, the audit log, and critique → finalize
                        → capture, refused and then passed),
                        live_rpc_check.py (needs AutoCAD, so pytest never runs it)
```

## Commands

```bash
uv run pytest -q                      # 611 tests, ~12s, no AutoCAD required
uv run pytest tests/test_terms.py -q  # adding a tool without a vocabulary entry fails here
uv run pytest tests/test_prompts.py -q  # a prompt naming a tool that does not exist fails here
uv run python scripts/check_doc_numbers.py   # docs and evaluation answers vs the tree
uv run ruff check . --output-format concise
uv run ruff format .
uv run python examples/floor_plan_demo.py           # DXF + PNG into examples/output/
uv run python examples/floor_plan_demo.py --com     # draw into a NEW AutoCAD drawing
`.venv/Scripts/python.exe tests/sandbox_driver.py path/to/script.py   # sandbox run, see below
```

`check_doc_numbers.py` derives the tool count, the collected test count, the journal payload
version and every `evaluation.xml` answer from the tree, and exits non-zero when the document
— or the source, which quotes the same counts in its own docstrings — disagrees.
`tests/test_doc_numbers.py` runs it as a gate, because this repository has no CI
and a test is the only thing that ever runs. It exists because one answer had silently gone
stale: the catalogue grew a fifth structure element and the evaluation kept answering 4.

`sandbox_driver.py` is how a live drawing exercise stays honest: it launches a
fresh `python -m acadmcp.server` from the current working tree for every run
(never a stale server started before the last edit), prints the git HEAD and
dirty-file count so a report can prove which code it tested, and gives each
runner its own process so concurrent exercises never share drawing state. The
script file defines `async def draw(tools)`; `await tools.call(tool, **args)`
returns parsed JSON and raises `ToolFailed` with the server's full error text.

The preview extra (matplotlib) is needed for any PNG output:
`uv sync --extra preview`.

## The plugin (`plugin/`)

A .NET 8 plugin that runs *inside* AutoCAD: a per-step journal, the engine that
applies and reverts steps, a loopback RPC port, and a docked palette. The geometry
stays in Python — the plugin is handed primitives (line, polyline, text, hatch,
circle, arc, dimension) and only creates entities, records their handles, and keeps
the log, so `geom.py` and `ops.py` remain the single source of coordinates.

Why the plugin exists at all: it puts each step in one transaction with its journal
write, so a rollback is one Ctrl+Z away from being undone as a unit, and the journal
travels inside the DWG — which is what lets the palette keep working with the MCP
server stopped.

```
plugin/
  src/AcadMcp.Core/           journal model, rollback arithmetic. No AutoCAD reference.
  src/AcadMcp.Plugin/         StepEngine, JournalStore (Xrecords), PrimitiveBuilder,
                              TextStyles, SelfTest, PluginEntry, PluginLog,
                              StepPanel + StepsControl (the docked palette)
  src/AcadMcp.Plugin/Rpc/     RpcServer (the socket), IdleQueue (main-thread dispatch),
                              DrawingApi (layers, units, model space, new document)
  bundle/AcadMcp.bundle/      PackageContents.xml is source; Contents/ is build output
  scripts/pack.sh             build and assemble the bundle
  scripts/selftest.sh         pack, install, launch a scratch AutoCAD, verify
  scripts/trust-autocad.ps1   put the plugin folder into TRUSTEDPATHS
  scripts/acad-state.ps1      is AutoCAD up, and is a modal dialog blocking it
  scripts/panel-shot.py       photograph a live window, for looking at the palette
  tools/AcadMcp.ApiProbe/     AutoCAD API signatures via MetadataLoadContext
tests/AcadMcp.Core.Tests/     xunit
```

```bash
cd plugin
dotnet test                     # journal arithmetic only, no AutoCAD needed
./scripts/selftest.sh           # both suites; refuses to run while AutoCAD is open
./scripts/selftest.sh --engine  # the in-AutoCAD self-test only
./scripts/selftest.sh --rpc     # the live RPC exchange only
./scripts/selftest.sh --force   # the edit-build-test loop, ~25s for both suites
./scripts/selftest.sh --engine --shot palette.png   # and photograph the palette
```

`selftest.sh` installs the bundle into the user-level `ApplicationPlugins` folder,
launches one scratch AutoCAD, and runs up to two suites in it:

- **engine** — the plugin tests itself at startup (`ACADMCP_SELFTEST=1`), logging to
  `%TEMP%\acadmcp-plugin.log` and ending with `AUTO-SELFTEST-DONE`.
- **rpc** — `tests/live_rpc_check.py` drives the running plugin over the real
  socket with this repository's own client, backend and journal control.

### The RPC port

Loopback only, 127.0.0.1, `ACADMCP_PORT` or 9880. One JSON object per line, one
response per line carrying the request's `id`; `ping` comes back as
`{"pong": true, "queue": "attached"}`. `{"ok": false, "blocked": true}` means the
plugin was there but AutoCAD could not run the call in time, and the message says
what was in the way. The Python side is `acad_plugin_client.py`; `steps.py` wraps
the journal actions, and its `CONTROL_ACTIONS` table is the single list the MCP
tool dispatches through.

The port is unauthenticated, so any local process can drive the drawing. That is
the same trust level the AutoCAD instance already has — the binding to loopback is
what keeps it off the network. Do not widen it.


## Architecture rules

The split is deliberate; keep it when editing.

- **`geom.py` imports nothing CAD-related.** It is the only place geometry maths
  lives, so it stays unit-testable and both backends get identical numbers. A plan
  drawn through COM and the same plan written to DXF contain the same coordinates,
  which is a property worth not breaking.
- **`ops.py` is backend-agnostic.** Every op takes a `Backend` and does its
  geometry through `geom`. Ops never touch an AutoCAD or ezdxf object directly.
- **Backends only render.** If you are computing architecture inside a backend,
  it belongs in `ops.py` instead.
- **A catalogue entry may name a parametric twin as well as a library block.**
  `Element.op` names an adapter in `catalog_ops.CATALOG_OPS`, `width_param` names
  the block parameter a *measured* width goes under, and `op_params` states the
  twin's own vocabulary where it differs from the block's (the two vocabularies do
  differ). Which half is drawn is the backend's capability's to decide, through
  `Element.placement_for` — never by trying `insert` and catching the refusal.
- **`catalog_ops.py` normalises; it computes no geometry.** Every adapter calls the
  op in `ops.py` that already draws the thing, so a catalogue element drawn by an
  adapter lands exactly where the matching tool draws it. The signature is uniform —
  `adapter(be, at, *, width=None, rotation=0.0, layer="", **params)` — and
  `tests/test_catalog_ops.py` pins it by introspection, because a catalogue entry
  names any of them and is called one way. `at` is the element's *own* reference
  point (a column's centre, a door's hinge, a lift's corner), which each adapter
  documents; the four rectangle adapters refuse a non-zero rotation rather than draw
  an upright rectangle where a rotated one was asked for.
- **`summary.py` derives, it never remembers, and imports nothing CAD-related.** It
  may import `geom` and `layers` and nothing else — never a backend, and the
  catalogue reaches it as a plain dict rather than the catalogue module. A room's
  area is recomputed with the same `geom.inner_boundary` call `draw_room` makes, so
  a summary that disagrees with `draw_room`'s own reply is the summary's bug.
- **`server.py` is thin.** Each tool parses JSON-ish input, calls one op or one
  backend method, and returns a dict. No logic lives here. Every drawing tool
  body runs inside `_auto_step`, so one tool call is one journal step — that is
  what makes a wrong coordinate cost one `drawing_undo` instead of a redraw.
  Every tool also picks one of the five annotation sets (`_READONLY` …
  `_DESTRUCTIVE_IDEMPOTENT`) defined under the server object — the client reads
  them to decide what it may auto-approve, and `test_tool_surface.py` fails on a
  tool without them.
- **All three backends journal.** `backends/recording.py` owns the step
  lifecycle (record primitives → apply as one batch → keep the handles);
  `journal.py` owns the undo bookkeeping. A backend implements only
  `_apply(primitives) -> handles` and `_erase(handles)`. The plugin backend is
  the exception in one place: its journal lives in the DWG, so its undo/redo
  delegate to the plugin's own rollback rather than to a local erase.
- Both backends must implement the full `Backend` protocol in `backends/base.py`.
  When you add a method, add it to the protocol and to both drivers.

## Conventions

- **Millimetres everywhere.** `INSUNITS` is set to 4.
- **Degrees, counter-clockwise from +X.**
- Room boundaries always mean the **wall centreline ring**; reported areas are
  measured to the inner face, so a 4000×3000 ring with 200 mm walls reports
  3.8×2.8 m. `geom.inner_boundary` is winding-independent — do not "fix" it.
- Room names and area labels are **shrunk to fit the room** (`layers.fit_text_height`
  against the ring's inner width), the same rule the equipment and shaft captions
  follow. A fixed height ran a six-character 无障碍卫生间 through three rooms.
- Layers follow GB/T 50001: `A-WALL`, `A-DOOR`, `A-WINDOW`, `A-ROOM-NAME`,
  `A-DIM`, `A-AXIS`, `A-STAIR`, `A-LIFT`, `A-SHAFT`.
- Comments explain constraints the code cannot show. Match the existing density:
  they are used where a line is non-obvious (why a workaround exists), not to
  narrate what the next line does.

## Gotchas

**Chinese text in AutoCAD.** A stock drawing's `Standard` style is `txt.shx` with
no big font, and an SHX font without a big font has no CJK glyphs — every Chinese
character renders as `?`. Text drawn through the COM backend therefore goes on an
`HZ` style using `gbenor.shx` + `gbcbig.shx`. Measured 2026-10-15, and this
**corrects an earlier claim here**: the *dxf* backend's `HZ` style carries
`simsun.ttc`, a CJK TrueType face, and AutoCAD 2026 renders Chinese from it
correctly — opening a 429-entity plan written by `drawing_save` to a .dxf path gave legible
`生产车间`, `卷帘门 JLM4245 洞口 4200×4500` and the rest. So "a CJK TrueType face
does not work in AutoCAD" was true of `simhei.ttf` specifically, not of TrueType as
a class; do not repeat it as a blanket rule. What does still hold: repointing a
style's font does **not** repaint existing text — `set_text_style` issues
`doc.Regen(1)` for that reason.

**Area unit.** Use `m²` (U+00B2). The single-codepoint `㎡` (U+33A1) measures as
`?` under every font tested, including `gbcbig.shx`, `simhei.ttf` and `simsun.ttc`.

**COM is not reliable without retries.** AutoCAD answers `RPC_E_CALL_REJECTED`
while mid-command or repainting, which a few-hundred-entity run hits constantly.
Every COM call must go through `_call(fn, *args)` in `com.py`, never straight at
the COM object. For property reads inside loops use `operator.attrgetter`, not a
bare `lambda` (ruff B023).

**A COM object belongs to the thread that made it, and the MCP server uses whichever
thread is free.** This cost the whole AutoCAD path: the first tool call after a
restart worked — it built the `ComBackend` and even created layers — and *every*
later one failed, whatever it was. `acad_connect` twice, `draw_line`, the
screenshot: all of them. AutoCAD itself was fine (`scripts/acad-state.ps1` says the
window is enabled; a fresh process reads `ActiveDocument` over COM without
trouble). The cause is the apartment: `import pythoncom` initialises COM for the
thread that imports it and no other, `backends/com.py` is imported lazily by
`make_backend("com")`, so the first call's worker thread got an apartment and every
later call's thread did not. Off-apartment, win32com's dynamic dispatch loses the
object's type and raises `AttributeError: <unknown>.AddLine` — which is *not* a
`com_error`, so the retry wrapper never saw it and the tool layer reported a bare
"failed". `_ComThread` now owns every COM call: it initialises COM once and all work
is marshalled onto it, so the objects stay in the apartment they were made in.
Two rules follow when editing `com.py`:

- Pass a **zero-argument callable** when the target is reached through an attribute:
  `_call(lambda: self.msp.AddLine(a, b))`, never `_call(self.msp.AddLine, a, b)`.
  The second form resolves the attribute on the *calling* thread, which is the whole
  bug. `_call(setattr, entity, "Layer", name)` and `attrgetter` are fine — they do
  their lookup when they run.
- `info()` no longer hides a lost link behind `entities: -1` and
  `document: "<unnamed>"`, and `entities()` no longer returns `[]`: an empty entity
  list reads as "no tags in this drawing", which silently restarts tag numbering at 1.

**`uv sync` fails while the MCP server is running.** The client holds
`.venv/Scripts/acadmcp.exe` open, so uv cannot rewrite it ("another program is
using this file"). The package is installed **editable**, so edits under `src/`
take effect without reinstalling — run
`.venv/Scripts/python.exe -m pytest` to test, and only sync when the server is
stopped. A running server holds the old code in memory, so new tools need a
client restart.

This bites the *client config* too, which is how it was finally diagnosed: a
server entry of `uv run acadmcp` makes uv reinstall the editable package on every
launch (`Uninstalled 1 package / Installed 1 package`), rewriting `acadmcp.exe`
each time — so while an old server is still alive, the client's attempt to start
a new one dies with `failed to remove file ... acadmcp.exe` and the server shows
as errored, permanently until that process is killed. Measured 2026-10-15 after
an orphaned 10:32 server blocked every restart for hours. The entry therefore
launches the module instead of the console script:

```json
"acadmcp": {"type": "stdio", "command": "uv",
  "args": ["run", "--no-sync", "--directory", "H:/My_Software/AutoCAD-MCP",
           "python", "-m", "acadmcp.server"]}
```

`python -m` never touches the generated `acadmcp.exe`, so no launch can lock it —
verified by starting a server this way and then moving `acadmcp.exe` aside, which
succeeded. Use `--no-sync` as well, or uv re-syncs on every launch and the same
lock returns. The same shape is already used by the `symkit` entry.

**Screenshotting the live AutoCAD window** needs `PrintWindow` with
`PW_RENDERFULLCONTENT`; AutoCAD renders through DirectX and a plain capture comes
back black. Occlusion is fine — `PrintWindow` renders the window's own content, so
one behind other windows captures identically (measured: mean pixel 45.33 in
front, 45.34 behind). A **minimised** window is the one case that cannot be
photographed: Windows gives it a 160×28 off-screen stub rect, so there is nothing
to render. The capture used to restore it, which yanked AutoCAD in front of
whatever the user had moved on to — for an AI drawing in the background while
somebody works elsewhere, that is unusable. `capture_window` now raises
`WindowUnavailable` instead of touching the screen, and `ComBackend` answers it by
asking AutoCAD to **export** the drawing and rendering that; the plugin backend
cannot export yet and says the window has to be up. This only works in an
interactive desktop session.

**`Document.Export` copies; `SaveAs` and `DXFOUT` do not.** Measured 2026-10-15 on
AutoCAD 2026, chasing why a background screenshot was hard to take:

- `SaveAs("x.dxf")` writes **`x.dxf.dwg`** — AutoCAD keeps its own output format
  and appends the extension it knows, so the `.dxf` in the name is a lie — and it
  **renames the open document** to the target either way. Pass a format constant
  (`SaveAs(path, 25)` = ac2018_dxf) to get a real DXF; 17 and 21 were refused as
  invalid here, 13 and 25 worked.
- `DXFOUT` on a *never-saved* drawing also renamed it (empty `Drawing1.dwg` became
  the exported name). Do not use it to take a copy.
- `Document.Export(stem, kind, selection)` writes `stem`.`KIND` — upper-cased —
  and leaves the document alone. `kind` may be `DXF`, `BMP`, `WMF`, `EPS`; `SVG`
  is refused. It needs a selection set (`SelectionSets.Add` + `Select(5)` for
  acSelectionSetAll). The BMP is a full raster of the view, which is what the
  minimised-window screenshot falls back to.

So `com.save()` routes a `.dxf` target through `Export` — it is a copy, which is
what the tool's docstring always promised — and only `.dwg` goes through `SaveAs`.

**PNG canvas sizing.** ezdxf's `finalize()` resets the figure to its own 6.3×4.8
default, so `fig.set_size_inches` must run *after* `draw_layout`. The canvas is
then fitted to the framed region's aspect rather than a fixed rectangle, because
letterboxing a portrait plan shrinks it until labels are unreadable. Both themes
force every entity to the theme's foreground colour (`ColorPolicy.WHITE` /
`BLACK`) — the layer palette is a drafting convention, and either background
makes half of it illegible; the earlier `LayoutProperties.set_colors(fg)` call
passed the colour into the *background* slot and never forced anything.

**MCP SDK version.** `FastMCP` was renamed `MCPServer` in mcp 2.x. `server.py`
imports whichever is present; keep that compat shim if you touch the imports.
Two more SDK behaviours are load-bearing, both found by driving the tools as a
client: the SDK masks any exception that is not a `ToolError` behind a bare
`Error executing tool <name>`, which is why `BackendError` subclasses
`ToolError` (the message is the recovery signal, so it must survive); and its
input models default to `extra="ignore"`, so a misspelt parameter used to be
dropped silently — `_strict_tool_arguments()` flips every tool's arg model to
`forbid` at import, and a stale server answering a newer client now fails
validation instead of drawing the wrong thing with `ok: true`.

**Caller mistakes raise ValueError, never a raw KeyError/IndexError/ZeroDivisionError.**
geom/ops validate at the boundary (enum values like `swing`/`axis`/`align`,
opening dicts without `start`/`width`, a coincident dimension pair, a
self-intersecting room ring — `geom.ring_self_intersects`, which the room tools
run before labelling because a bowtie's centroid lands outside its walls), and
`_surface_input_errors()` wraps every tool so the ValueError re-raises as
`BackendError` and its message reaches the client. Only ValueError gets that
conversion: anything else escaping a tool body is a programming bug, and it
*should* come back masked and loud. Validations were all born from sandbox
exercises — `tests/test_input_validation.py` pins each one.

**The preview renders CJK natively, not through ezdxf's glyph pipeline.** ezdxf
turns every glyph into a filled path (`draw_filled_paths(..., detect_holes=True)`),
and that conversion corrupts some CJK outlines: SimSun's U+56FE (图) comes out as
an ink blob — in drawing titles, the commonest character there is. The font,
matplotlib's own text engine, and AutoCAD all render the same glyph correctly, and
the corruption shows only at *some* canvas scales, which is why it survived a
first round of testing. `DxfBackend.screenshot` therefore subclasses `Frontend`
and keeps any text containing a character above U+2E7F out of the ezdxf path
(`_has_cjk`), drawing those with matplotlib instead (`_draw_cjk_texts`). Two
things that bit on the way: matplotlib resolves a font *collection* by family
name to a face it then cannot pull a CJK glyph from ("failed to get glyph"), so
the font is chosen by file (`_cjk_font_properties`); and the deferred texts still
have to contribute to the data limits, or a title below the plan is cropped out
of a full shot. The sizing is `points = height_mm * (figure_height_points /
span_y)` — the axes fills the figure, so data units convert through the vertical
span, not through the DPI. `tests/test_dxf_create_render.py` asserts the
mechanism (CJK never reaches `Frontend.draw_text_entity`, ASCII still does),
because a pixel threshold could not separate the blob from a stroke reliably:
measured ink density of the densest title cell was 37-45% across scales and the
two ranges overlapped.

**Tests do not need AutoCAD.** They cover the DXF backend and `geom`. The one COM
test skips itself when a live session is present. Do not add tests that require
AutoCAD to pass.

**A loaded .NET assembly cannot be unloaded from AutoCAD.** Every plugin code
change therefore costs an AutoCAD restart to try. That is why
`tools/AcadMcp.ApiProbe` exists — it reads the AutoCAD assemblies as metadata
with AutoCAD closed, so an API mistake does not cost a restart — and why the
self-test runs itself unattended.

**Unsigned DLLs in ApplicationPlugins still prompt.** That folder is *not*
trusted unconditionally: with `SECURELOAD=1` and an empty `TRUSTEDPATHS` (this
machine's state) AutoCAD raises "Security - Unsigned Executable" on every load.
"Always Load" does not help, because each rebuild produces a new file and the
prompt returns. While the prompt is up the plugin never loads, which shows up
only as a log file that never appears — that is what silently broke the
self-test. `scripts/trust-autocad.ps1` writes the folder into `TRUSTEDPATHS`, and
it must run with AutoCAD **closed**: AutoCAD writes its profile back to the
registry on exit and would overwrite it.

**There is no managed API for undo grouping.** `Document`, `Database` and
`TransactionManager` expose no undo members (`AddUndoMarker`/`SetUndoMark` in
acmgd are native exports). So the plugin depends on measured behaviour instead:
mutating the database and rewriting the journal in one transaction really does
produce **one** undo record. The self-test proves it — after a rollback, one
`_.UNDO 1` restores all six entities *and* the journal's handle lists. Keep any
new mutation and its journal write in the same transaction, or that is lost.

**`Editor.Command` throws `eInvalidInput` outside a command context.** From an
idle handler the only way to run a command is `SendStringToExecute`, which queues
it; a test that needs a command to have run must therefore finish from a later
idle tick. The self-test's undo probe is split into two phases for this reason.

**Reads must not open for write.** Opening the journal dictionary `ForWrite`
turns a read into a database write, which AutoCAD rejects with `eLockViolation`
unless the caller holds a document lock. `JournalStore.OpenDictionary(tr, create)`
opens strictly read-only when `create` is false, and `StepEngine.Reload` takes a
document lock. Both mistakes surfaced as a lock violation in a *reader*.

**`Application.Idle` fires while a command is waiting for input.** So draining the
queue on every idle tick would slip our edits inside somebody else's command.
`IdleQueue` checks `Document.CommandInProgress` first and waits its turn. The same
property is why the queue can describe a stall: it is written on the main thread
and read from the socket thread, so a blocked request gets "AutoCAD is in the LINE
command" instead of a bare timeout. Idle *not* firing is the signal for a modal
dialog, which no API can report off-thread.

**An SHX font goes on `TextStyleTableRecord.FileName`, not `.Font`.** `.Font` takes
a TrueType family name, and assigning it a `.shx` name throws `eInvalidInput` — that
is how the first version of `TextStyles` failed. `.BigFontFileName` is where the
big font goes. This matters because an SHX font with no big font renders every
Chinese character as `?`, which is also why the self-test checks the style and the
font files of the text it drew, not just the characters it stored.

**The socket client must not retry a drawing call.** If the connection dies, the
request may already have been carried out, so re-sending `apply` would draw the
step twice. `RETRY_SAFE` in `acad_plugin_client.py` lists the read-only methods
allowed a second attempt and nothing else. `tests/test_plugin_client.py` pins this
down with a fake socket, including the case where the fake deliberately drops the
connection.

**The journal's write path is simple on purpose, and the numbers are known.** A save
rewrites every record; a writer that only wrote what changed would have to be told what
changed, and a miss is a silent permanent loss. Measured with the self-test's scale note
(`scripts/selftest.sh --engine` prints it): at 400 steps one more step costs 10.9 ms, a
full re-read 11 ms, and a journal-only change 9 ms — the growth is in the geometry path,
not in the journal write, so incremental writes would not fix it. Both optimisations were
planned, measured and dropped; re-measure before resurrecting either. Step numbers are
positions, and a record that cannot be read is counted in `journal.Unreadable` and
reported, not silently skipped.

**Every drawing tool call is one journal step.** `_auto_step` in `server.py` wraps
each `draw_*` tool; an explicit `step_begin` overrides it (while one is open, drawing
records into it and nothing commits). Undo/redo are backend-neutral
(`drawing_undo`/`drawing_redo`/`drawing_history`); on the plugin backend they delegate
to the DWG journal so the palette never disagrees. `drawing_rollback_to(index)` aims the
same undo at a named step rather than making a caller count — `Journal.rollback_to`
existed with no tool in front of it — and `drawing_history` now returns machine-readable
`steps` rows beside its rendered text, so an index can be picked without counting. On the
plugin an *accepted* step blocks a rollback and the refusal is reported, not swallowed;
`step_control(action='rollback_to', index=..., force=True)` overrides it. The dxf journal
persists inside the
file (root dictionary `ACADMCP_JOURNAL`, one XRecord, JSON `ensure_ascii`-escaped and chunked
at 1900 chars because a group-code-1 string tops out at 2049) and survives
`drawing_open` — handles are stable across ezdxf save/load, so an old session's steps
stay undoable. Handles change on redo: `RecordingBackend.redo` stores the fresh ones
back into the step, so never cache a handle across a redo. `drawing_replay` re-draws
the applied journal onto another backend (dxf→plugin is how a headless session moves
into AutoCAD without redrawing); steps a target cannot apply — a library insert onto
dxf — are skipped and reported, never silently dropped.

**A SplitContainer starts out 150x100.** Setting `Panel2MinSize` to anything useful in
the constructor throws before the control has ever been laid out, and the message it
throws talks about `SplitterDistance`, which sends you looking in the wrong place.
`StepsControl.SizeSplit` therefore applies the minimums and the splitter position only
once the container has real dimensions, and sizes against the smaller of its height
and width because WinForms checks one and names the other in the message. The
in-AutoCAD self-test builds and opens the palette as one of its checks, which is how
this was caught — otherwise the user's first click would have produced a .NET error
dialog over their drawing.

**A floating PaletteSet is not a child of the AutoCAD frame.** Screenshotting the
frame misses it completely, and once the user docks it the window has no title of its
own either. `panel-shot.py` therefore tries the palette by title and falls back to the
frame; `scripts/acad-state.ps1` lists the windows when it is unclear which is which.
AutoCAD also *remembers* a palette's size and dock position by its id, so a palette
restored small stays small — set `MinimumSize` accordingly.

**AutoCAD opens its own drawing at startup.** An unattended run therefore has an empty
`Drawing1` active while the self-test draws into its own scratch document. The palette
reads whichever drawing is *active*, so the test mode leaves its scratch drawing open
and active rather than restoring the previous one, and the self-test logs which is
which: `self-test finished: drew in 'Drawing2.dwg', active is 'Drawing2.dwg'`.

**Importing a block definition: use a wblock clone, not `Database.Insert`.** Three things
were measured before this was settled, each one costing a wrong version: `Insert`'s second
argument must be the source *path* — passing the block's own name there, which the
parameter's name invites, refuses the whole import with `eSelfReference`, and
`preserveSourceDatabase: false` refuses with `eInvalidOwnerObject`. Even when it succeeds it
copies the library's *entire* block table into the drawing (nine extra blocks from AutoCAD's
sample library) plus a nameless definition. `db.WblockCloneObjects` with
`DuplicateRecordCloning.Replace` brings exactly the definition asked for. Resolve the
definition by name from the target's own block table afterwards rather than trusting the
`ObjectId` the clone call was handed.

**A dynamic block reference can resolve to an anonymous `*U` record.** Set a dynamic block's
parameters to a state it has no named size for — a "custom" state — and AutoCAD bakes that
state into an anonymous definition: `BlockReference.Name` and its definition both read
`*U3`, and the original block name is nowhere on the reference. An element's identity has to
come from the step's record (`Primitive.Block`), not from the reference; the `entities` call
reports both `block` and `definition` and neither is guaranteed to match what was asked for.
A dynamic block's parameters *are* readable without opening anything interactively — a
throwaway `BlockReference` that is never appended exposes the whole collection including
`GetAllowedValues()` — which is how `blocks` describes a library.

**A block definition is document state, not a step's to own.** Rollback erases only the
handles a step recorded, so it never touches a definition; if step 1 imported the block and
step 2 placed another copy, rolling step 1 back must leave the definition standing or step
2's reference would dangle. `EnsureBlock` is therefore idempotent and never journalled.
`Ctrl+Z` of the importing step *does* remove the definition, together with that step's
reference and journal record — correctly, since all three were one transaction.

**The element catalogue is data, and it is checked against the library, not trusted.** Every
block name in `src/acadmcp/data/elements.json` was read off AutoCAD's libraries with
`library_list`; `element_types` re-checks the names at call time and reports entries whose
block the library has not got, and a library it could not read, because "could not check" is
not "fine". Loading is tolerant (a broken entry is skipped and *counted*, never raised, the
same rule the journal follows) but a *named variant that does not exist* raises rather than
falling back: placing the wrong size silently is the same failure as setting a dynamic
property the block has not got. Tags are numbered from the tags already in the drawing and
never from a stored counter. Layer names, model numbers and wall thicknesses come from
`acadmcp.json` and the catalogue file, never from `layers.py` — a standard is the drawing
office's, not this package's.

**A catalogue entry has two ways to reach the paper, and the backend picks — declared, not
probed.** `Element.placement_for(can_insert)` takes the library block when the backend can
insert one (the plugin does; `dxf` and `com` do not) and the parametric twin otherwise.
`Backend.supports_insert` is a **class attribute** — `False` on `RecordingBackend`, `True` on
`PluginBackend` — and not a probe, because inside a step the `dxf` and `com` refusals are
deferred to the commit, so a caller cannot find out by trying, and which half to draw has to
be decided before anything is drawn. This is what made `place_element`/`place_along`/
`place_grid` work headless: before it, all three began with `_require_plugin()` and an AI
writing a plan in DXF could not place a single door. The shipped catalogue's `door` and
`window` therefore carry twins, and `column` is **op-only** — a 承力柱 is a filled rectangle
whose size is its whole identity, so it needs no library and works on every backend.

**An entry that names both keeps the two vocabularies apart, and a measured width is the
opening's.** Where an entry names a block *and* a twin, the block's parameter names stay in
`params` and the twin's go under `op_params` (element-level and per-variant), because the two
differ — the door block calls its width 门尺寸 while `door_symbol` takes `width`.
`Element.twin_settings()` is the accessor, and it **refuses, not defaults**, a variant that
states something for the block and nothing for the twin: reaching an adapter as its own
defaults is a right-looking element of the wrong size. `"op_params": {}` means "the twin
deliberately needs nothing", which is a different answer from absent, and `Variant.op_params`
is `None` when absent so the reader can tell them apart. In `draw_wall_with_openings` an
opening may name an `element`, and the opening's measured `width` is authoritative — passed
to the element as an override, so a nominal 900 block cannot be placed into a measured 1500
hole. A failure is collected in the result's `element_openings.skipped`, never raised, so the
opening still gets cut — the same trade `clamped_openings` makes.

**A sheet is a page setup on the file's own LAYOUT, and the scale is written in three places that
must agree.** `ezdxf.new("R2010")` sets none of the page-setup fields, so a drawing from this
package used to open on whatever the reader's default was. `DxfBackend.set_page_setup` writes
`paper_size`/`paper_width`/`paper_height`, the four margins, and the scale — and the scale has to
go to `scale_numerator`/`scale_denominator` *and* `unit_factor` (DXF group 147, what a plot falls
back to) *and* `standard_scale_type` (group 75, AutoCAD's enum: 1:100 is 7, 1:200 is 16). A file
whose 75 says 1:10 while its 147 says 1.0 plots at 1:1 and claims 1:10, which is the failure
`STANDARD_SCALE_TYPES` exists to prevent; a ratio with no row there is written to 147 alone
rather than given an invented 75.

**"`ezdxf.new` sets none of the page-setup fields" is true in memory and false after a save/load
round trip.** A round trip materialises ezdxf's own defaults onto every paper layout: a `Layout1`
comes back as `paper_size` "A3", 420×297, `plot_type` 5, margins 7.5/20, `unit_factor` 1.0. So the
paper *fields* alone cannot tell a real sheet from a drawing that never had one — and a reopened
sheetless drawing therefore reported a phantom A3 at 1:1, which let `drawing_plot_pdf` plot a
frameless file and quietly bypassed the assumed-scale fallback in `drawing_check_standard`.
`_sheet_from_layout` now adopts a layout only when it carries the *evidence* of a setup: its name
matches its own paper name (case-insensitive) — because `set_page_setup` renames the layout to the
paper name, so a real sheet reopens as "A3" while the phantom keeps "Layout1" — or, for a layout a
caller named something else, `plot_type == 1` ("plot the layout's contents"), which the setup writes
and ezdxf's default is not. Measured 2026-10-15 on ezdxf 1.4.4; `test_sheet_backend.py` pins the
round trip both directions.

**Use `Layouts.rename`, not `Layout.rename`, and know why.** The object method sets the LAYOUT
entity's name and leaves the `ACAD_LAYOUT` dictionary key alone, so the saved file carries a tab
reading "A3" under an entry called "Layout1" — and ezdxf keys its own layout table by that entry
(measured on 1.4.4: `[l.name for l in doc.layouts]` says A3 while `doc.layouts.get("A3")` raises
`KeyError`). Which is also why the document's existing paper layout is renamed rather than added
to: two paper layouts in one file is how a reader plots the wrong one.

**The sheet geometry follows the standards, and the viewport wraps the title block in an L.**
The margins are GB/T 14689's: a 25 mm bound edge on every size, 10 mm on the other three for
A0–A2 and 5 mm for A3/A4 (a uniform 10 would eat a tenth of an A4's width). The title block is
180 x 56 mm in the frame's bottom-right corner — GB/T 10609.1's size — with four rows, so the
比例 the tool always adds never evicts a caller's field; a sheet whose title block does not
state its scale fails the sheet's own point. The viewport takes the strip *above* the title
block, not the strip left of it: on a landscape sheet that is the larger of the two rectangles
the corner leaves and the landscape shape a floor plan usually is, and on a portrait A4 the
left-of strip is 210 - 180 - margins = negative — measured as a negative-width VIEWPORT before
this was fixed. `SCALES` spans 2:1 to 1:1000 from GB/T 50001's tables (details at 1:5/1:10,
site plans at 1:250/1:1000); it is the menu `largest_scale_that_fits` picks from, while
`resolve_scale` accepts any ratio.

**A sheet's frame is drawn at 1:1 in paper millimetres, so it is paperspace content.** The frame,
the title block and the viewport are recorded as ordinary journal primitives carrying
`paper: True`, and `DxfBackend._apply_primitive` swaps the destination around the same `_draw_*`
methods the model uses — the drawing code stays destination-agnostic. Three consequences that
were each a wrong version first:

- **The viewport's view centre is resolved before the destination is swapped.** `_plan_center`
  reads `self.msp`, and on the sheet that is the layout: resolved after the swap it measures the
  frame and the title block, and the viewport frames the *sheet* instead of the plan on it. Both
  coordinates came back as the frame's centre, in every drawing, which is how it was caught.
- **Erase routes through the layout that owns the entity** (`doc.layouts.get_layout_for_entity`).
  `msp.delete_entity(paper_entity)` raises `ValueError: list.remove(x): x not in list`, so
  aiming every erase at the modelspace would leave an undone sheet's frame standing and report it
  missing.
- **`entities()` lists the paper layouts as well as the model.** A sheet is part of the drawing,
  so a listing that stopped at the modelspace reported a frame the journal had recorded as
  missing and a sheet's text as never drawn.
- **The page setup is document state and outlives an undo**, exactly as a block definition does:
  one `drawing_undo` takes back the frame and the viewport, not the paper.

**The plot is composed as two axes, and neither computes a transform by hand.** The figure's size
*is* the paper, so the PDF's own `/MediaBox` is the paper by construction and can be measured
against what was asked for — which is what `drawing_plot_pdf`'s `verified` does, reading the bytes
of the file it just wrote rather than checking its own arithmetic. The frame and title block go
into an axes spanning the whole page in paper millimetres at 1:1; the plan goes into an axes
sitting exactly on the viewport rectangle, with its data limits set to the model window that
viewport shows, so the axes' own limits *are* the plot scale and matplotlib's own clipping is the
viewport's edge. The vertical span is `sheet.view_height_model` and the horizontal follows from
the viewport's proportions — the rule AutoCAD plots by. Matching both spans to the box instead
prints a plan of the wrong proportions, which is the one error a reader would not catch by eye.
The VIEWPORT entity is filtered out of the sheet pass: it is a window onto the model, not
something a plot draws. CJK text is deferred to matplotlib in both passes (`_cjk_frontend_class`,
shared with `screenshot`) or 图 comes out as an ink blob, and each pass sizes its deferred text by
its own scale.

**A second `drawing_sheet` on the same drawing is refused, not drawn.** The frame is drawn
content rather than a setting, so `set_page_setup` reusing the layout is not enough — the second
call would put a second frame and a second viewport on the same layout. Measured: two calls left
24 lines on `A-SHEET` where there should be 12. The tool refuses and names the recovery
(`drawing_undo`, then call again with the paper and scale wanted), rather than replacing the old
frame in place: erasing another step's entities is not this step's to do, and an undo that cannot
put them back would be worse than a refusal. The page setup is document state and outlives the
undo, so that sequence ends where the caller wanted to be.

**`drawing_sheet` says whether the plan fits, because a caller cannot work it out.** At 1:100 an
A3 viewport is 380 x 221 mm and shows 38 x 22.1 m, so a 40 m bay is cut off — the plan is
correctly at 1:100 and the page correctly 420 mm wide, and both are useless together. The reply
carries `plan_mm`, `fits`, and when it does not fit a `note` naming the largest scale this package
offers that holds it whole. Reported, never raised: a sheet narrower than the building is a design
choice, and a detail sheet is drawn that way on purpose. A sheet on an empty drawing reports no
`plan_mm` at all rather than a zero one.

**The drawing records the catalogue that drew it.** The catalogue is otherwise ambient — a
package file plus whichever `acadmcp.json` is found by walking up from the working directory —
so reopening a drawing elsewhere silently resolved its doors against something else.
`Journal.meta["catalog"]` now carries the identity **and the entries** (`Catalog.identity()`
produces it, `Catalog.adopt()` consumes it), because a drawing that records only a file path
resolves to something else on the next machine. Persistence: the DXF keeps it in the same
`ACADMCP_JOURNAL` XRecord as the journal; the COM backend writes a `<drawing>.<ext>.acadmcp.json`
**sidecar** beside a save, because ActiveX exposes no way to write an XRecord (that is the
plugin's route) — `drawing_save`/`drawing_status` name the sidecar, so a file appearing next
to the drawing is never unannounced. The plugin backend is the exception: its journal lives
in the DWG, which does not yet carry the catalogue, so its stamp sits in the local journal
only. Precedence on open: the project's own files, then the
copy embedded in the drawing, then the package's starter; a difference is reported as a
`catalog_warning`, never a silent redraw. `Journal.from_dict` refuses a payload version newer
than it writes (1), and the reason is specific: every `from_dict` failure lands in the dxf
reader's `journal_unreadable`, which is *reported rather than raised*, so without the guard a
future format would silently degrade to "no journal".

**`summary.py` derives, it never remembers.** A room's area is recomputed from the recorded
boundary with the same `geom.inner_boundary` call `draw_room` makes, so a summary that
disagrees with `draw_room`'s own reply is the summary's bug, not the drawing's. It reads the
journal's recorded intent, so it works after a reopen and on every backend, and it is what the
read-only `drawing_summary` tool wraps. A row it cannot read — no `op`, no readable `params`,
an undone step — is counted and warned about, never raised, the rule the catalogue loader and
the journal reader already follow. `annotation` counts one per *step*, not per entity: a
dimension chain is one annotation here.

**A handler keyed on an op name no tool produces reports a number nobody can move, and a
test can keep it that way.** `HANDLERS` was keyed `"add_axis_grid"` while `server.py` records
`draw_axis_grid`, so `annotation.axis_grids` was 0 for every real drawing — and
`tests/test_summary.py` fed the handler that same impossible op name, so the suite stayed
green. `HANDLERS` also carried a `"draw_shaft"` key for a tool that does not exist
(`draw_equipment(kind="shaft")` records `draw_equipment`). The fix is not just the two keys:
the test now reads the op names out of `server.py` with a regex over `_auto_step("…")` and
`_placement(be, "…")`, so the table cannot drift from its producers again. The same shape of
bug is worth looking for wherever a dict is keyed by a string a human typed.

**`drawing_verify` reconciles the journal against the drawing, and reading it found a
contract two backends were not both meeting.** `Backend.step_rows` documents a row carrying
"how many entities it drew", and `RecordingBackend` does — but `PluginBackend` handed back
the plugin's own rows verbatim, which carry `handles` and `primitives` and no `entities` at
all. Nothing had depended on the key before, so nothing noticed; `reconcile` treating a
missing count as zero would have reported every correct plugin drawing as holding entities
no step recorded. The row is now normalised where it is produced (`PluginBackend.step_rows`
derives the count from `len(handles)`), and `verify.reconcile` warns rather than assuming
zero when a row still cannot be counted — for a check like this, a *false* verdict is worse
than saying nothing. The invariant it rests on is measured, not assumed: a mitred envelope
with a door, a room label, an axis grid, a dimension and a placed element totalled 38
entities and the drawing held 38, through draw, save, reopen, undo and redo. It is a count,
not a handle list, so it says the total is wrong without saying which entity is.

**A client that passes metres used to draw a 4 mm room and be told it succeeded.**
`geom.plausible(value, what, low, high)` now refuses a dimension outside a deliberately wide
range with a message naming the metre/millimetre slip: wall thickness 20–2000 mm, a room's
inner width 500–200000 mm (measured on the *inner* span after `ring_self_intersects`, because
that is the number a unit slip destroys), a column 50–3000 mm. The bounds catch a unit slip,
not a design — a guard that refuses a legitimate 300 mm partition would be worse than no
guard, so widen a bound rather than adjust a caller if a real design trips it.

**`evaluations/fixtures/floor_plan.dxf` is not byte-reproducible, so `git status` is not a
backwards-compatibility check.** The file embeds a timestamp, random GUIDs, and occasionally a
`BLOCK_RECORD` ordering that hash randomisation swaps, so it is dirty after every regeneration
even when nothing moved. The check that works is a geometry fingerprint —
`(dxftype, layer, text, height, insert/points)` per entity, as a `Counter` — compared between
the demo's output on a pristine `git archive HEAD` tree and on the working tree; the two must
be equal. That check found the committed fixture differed from HEAD's own output in exactly two
`A-ROOM-AREA` text heights (250.0 → 240.0), a staleness rather than a regression: the fixture
was last written in `e3d76f1`, while `layers.fit_text_height` landed in `7da48c5`, one commit
later. The fixture has been refreshed, and the entity count is unchanged at 247 — so the
answers in `evaluation.xml`, which reference entity counts and never a text height or an area
value, still hold.

**A queued zoom is useless before a screenshot.** `ZOOM` cannot be run from the plugin's idle
handler (`Editor.Command` throws `eInvalidInput` outside a command context) and queueing it
via `SendStringToExecute` means the picture is taken of the view as it was *before*. That is
why `zoom_extents` sets the current view record synchronously (`Editor.GetCurrentView` /
`SetCurrentView` on the `ViewTableRecord`, which lives in **accoremgd**, not acmgd or
acdbmgd) and measures the extents off the entities rather than reading `Extmin`/`Extmax`,
which AutoCAD only refreshes on its own terms.

**A building envelope needs `draw_wall_with_openings(closed=True)`.** Openings can only
be punched along a run, and a run has two square-capped ends, so drawing an envelope as
four runs puts a cap on a corner: the outline steps in by half the wall thickness at
every corner and both walls' poché overlaps in the 240 mm corner square. Measured on a
48×24 m envelope: the mitred face reaches `(-120, -120)`, the four-run version never
does. `closed=True` reads `points` as a ring, measures openings along it from the first
vertex, and starts the run where its first opening starts — which puts both caps inside
that opening (where a cap belongs) and leaves every corner mid-segment, where
`offset_pair` mitres it. A ring with no openings is drawn as one closed wall.

**An opening's jamb is drawn once, by the symbol that owns it.** `draw_wall_run`'s end
cap and a door or window symbol's jamb tick are the same line across the same thickness;
drawing both put each jamb in the drawing twice, on A-WALL and on A-DOOR at once — 40 of
the 166 line segments of that envelope. Segments are now drawn with `cap_ends=False` and
capped explicitly: at the run's own ends, and around a bare `opening`, which draws no
symbol of its own.

**A half-made COM hatch is worse than no hatch.** The ActiveX hatch API wants boundary
*objects*, so `com.py` makes a temporary polyline, hatches it and deletes the boundary.
If any step fails, the survivors are a hatch still at pattern scale 1.0 — a solid blob
across a 240 mm wall — and a naked polyline, both on layer 0 and both invisible, because
`draw_wall_run` suppresses a hatch failure so a decorative fill cannot take a wall down.
The failure path now deletes both halves and the boundary is created on the target layer.
Measured on a live envelope: one of four segments failed this way before, none did after.

**A Chinese character is 1.42 x its text height wide, not 1.0.** Measured both sides of
the same drawing: `生产车间` at height 350 is 1991 mm in AutoCAD and 1973 mm in this
package's PNG preview, i.e. 497.8 mm a character — so the preview is a faithful proxy for
text width, and anything that sizes a caption to sit inside a box has to use
`layers.fit_text_height`, or a four-character shaft name is wider than a 1500 mm shaft.

## Environment


- AutoCAD 2026 (release 25.1) is at `F:\AutoCAD\AutoCAD 2026` — **not** under
  `C:\Program Files\Autodesk`, and registry lookups for that path come back empty.
- The venv runs Python 3.14; `requires-python` is `>=3.10`.
- This package is registered as an `acadmcp` MCP server in the ZCode client
  config at `%USERPROFILE%\.zcode\cli\config.json`.
- AutoCAD's security and path settings live per profile under
  `HKCU\Software\Autodesk\AutoCAD\<release>\<product>\Profiles\<profile>\Variables`
  (`SECURELOAD`, `TRUSTEDPATHS`). The default profile's folder name is localised —
  `<<未命名配置>>` here — so scripts discover it by walking the tree instead of
  naming it.
- The plugin targets `net8.0-windows` and needs the .NET 8 **SDK** (only the
  runtime was installed to begin with). AutoCAD's own assemblies are referenced
  from `ACAD_DIR` with `Private=false`.

## Read before changing

- `README.md` — backend comparison, the two most important tools
  (`draw_wall_with_openings`, `draw_room`), and the "Chinese text" section, which
  records what was measured rather than guessed.
- `backends/com.py` — the `_call` retry wrapper and why it exists.

