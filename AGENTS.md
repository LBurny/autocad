# AGENTS.md

MCP server that lets an AI client drive AutoCAD to draw 2D architectural floor
plans — either into a live AutoCAD session over ActiveX/COM, or into a headless
DXF file. Published at https://github.com/LBurny/autocad.

## Layout

```
src/acadmcp/
  geom.py        pure 2D geometry: offsets, mitre joins, areas. No CAD imports.
  layers.py      drafting standard: layer names, ACI colours, text heights, mm sizes
  ops.py         architectural operations (walls, doors, rooms, cores, annotation)
  capture.py     Win32 window capture, used only by the COM backend
  server.py      the MCP tool surface (38 tools)
  backends/
    base.py      the Backend contract every driver implements
    dxf.py       ezdxf driver — no AutoCAD needed, renders PNG
    com.py       ActiveX driver — draws into a running AutoCAD
examples/floor_plan_demo.py
tests/           test_geom.py, test_backends.py
```

## Commands

```bash
uv run pytest -q                      # 36 tests, <1s, no AutoCAD required
uv run ruff check . --output-format concise
uv run ruff format .
uv run python examples/floor_plan_demo.py           # DXF + PNG into examples/output/
uv run python examples/floor_plan_demo.py --com     # draw into a NEW AutoCAD drawing
```

The preview extra (matplotlib) is needed for any PNG output:
`uv sync --extra preview`.

## The plugin (`plugin/`)

A .NET 8 plugin that runs *inside* AutoCAD: a per-step journal, the engine that
applies and reverts steps, and a docked palette. The geometry stays in Python —
the plugin is handed primitives (line, polyline, text, hatch, circle, arc) and
only creates entities, records their handles, and keeps the log, so `geom.py`
and `ops.py` remain the single source of coordinates.

```
plugin/
  src/AcadMcp.Core/           journal model, rollback arithmetic. No AutoCAD reference.
  src/AcadMcp.Plugin/         StepEngine, JournalStore (Xrecords), PrimitiveBuilder,
                              SelfTest, PluginEntry, StepPanel, PluginLog
  bundle/AcadMcp.bundle/      PackageContents.xml is source; Contents/ is build output
  scripts/pack.sh             build and assemble the bundle
  scripts/selftest.sh         pack, install, launch a scratch AutoCAD, print the report
  scripts/trust-autocad.ps1   put the plugin folder into TRUSTEDPATHS
  scripts/acad-state.ps1      is AutoCAD up, and is a modal dialog blocking it
  tools/AcadMcp.ApiProbe/     AutoCAD API signatures via MetadataLoadContext
tests/AcadMcp.Core.Tests/     xunit
```

```bash
cd plugin
dotnet test                     # journal arithmetic only, no AutoCAD needed
./scripts/selftest.sh           # refuses to run while AutoCAD is open
./scripts/selftest.sh --force   # the edit-build-test loop, ~15-40s
```

`selftest.sh` installs the bundle into the user-level `ApplicationPlugins`
folder, launches a scratch AutoCAD with `ACADMCP_SELFTEST=1`, and prints the
report. The plugin runs its checks at startup, logs them to
`%TEMP%\acadmcp-plugin.log`, and ends with `AUTO-SELFTEST-DONE`.

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
  backend method, and returns a dict. No logic lives here.
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
character renders as `?`. Text goes on an `HZ` style using `gbenor.shx` +
`gbcbig.shx`. A CJK **TrueType face does not work** in AutoCAD here, even though
it renders fine in the ezdxf PNG. Also: repointing a style's font does **not**
repaint existing text — `set_text_style` issues `doc.Regen(1)` for that reason.

**Area unit.** Use `m²` (U+00B2). The single-codepoint `㎡` (U+33A1) measures as
`?` under every font tested, including `gbcbig.shx`, `simhei.ttf` and `simsun.ttc`.

**COM is not reliable without retries.** AutoCAD answers `RPC_E_CALL_REJECTED`
while mid-command or repainting, which a few-hundred-entity run hits constantly.
Every COM call must go through `_call(fn, *args)` in `com.py`, never straight at
the COM object. For property reads inside loops use `operator.attrgetter`, not a
bare `lambda` (ruff B023).

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
