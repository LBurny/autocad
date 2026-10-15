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
  capture.py            Win32 window capture, and finding the AutoCAD frame
  server.py             the MCP tool surface (44 tools, each annotated by behaviour class)
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
evaluations/            read-only QA pairs + fixture DXF + harness, per the mcp-builder guide
tests/                  test_geom.py, test_backends.py, test_journal.py, test_recording.py,
                        test_dxf_journal.py, test_plugin_backend.py, test_server_journal.py,
                        test_plugin_client.py, test_catalog.py, test_element_tools.py,
                        test_tool_surface.py (annotations, naming, pagination),
                        live_rpc_check.py (needs AutoCAD, so pytest never runs it)
```

## Commands

```bash
uv run pytest -q                      # 179 tests, ~7s, no AutoCAD required
uv run ruff check . --output-format concise
uv run ruff format .
uv run python examples/floor_plan_demo.py           # DXF + PNG into examples/output/
uv run python examples/floor_plan_demo.py --com     # draw into a NEW AutoCAD drawing
```

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

**Screenshotting the live AutoCAD window** needs `PrintWindow` with
`PW_RENDERFULLCONTENT`; AutoCAD renders through DirectX and a plain capture comes
back black. A minimised window is restored first. This only works in an
interactive desktop session.

**PNG canvas sizing.** ezdxf's `finalize()` resets the figure to its own 6.3×4.8
default, so `fig.set_size_inches` must run *after* `draw_layout`. The canvas is
then fitted to the framed region's aspect rather than a fixed rectangle, because
letterboxing a portrait plan shrinks it until labels are unreadable.

**MCP SDK version.** `FastMCP` was renamed `MCPServer` in mcp 2.x. `server.py`
imports whichever is present; keep that compat shim if you touch the imports.

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
to the DWG journal so the palette never disagrees. The dxf journal persists inside the
file (root dictionary `ACADMCP`, one XRecord, JSON `ensure_ascii`-escaped and chunked
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

