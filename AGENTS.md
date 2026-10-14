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

## Environment

- AutoCAD 2026 (release 25.1) is at `F:\AutoCAD\AutoCAD 2026` — **not** under
  `C:\Program Files\Autodesk`, and registry lookups for that path come back empty.
- The venv runs Python 3.14; `requires-python` is `>=3.10`.
- This package is registered as an `acadmcp` MCP server in the ZCode client
  config at `%USERPROFILE%\.zcode\cli\config.json`.

## Read before changing

- `README.md` — backend comparison, the two most important tools
  (`draw_wall_with_openings`, `draw_room`), and the "Chinese text" section, which
  records what was measured rather than guessed.
- `backends/com.py` — the `_call` retry wrapper and why it exists.
