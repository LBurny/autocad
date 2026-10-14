# AutoCAD-MCP

An MCP server that lets an AI client drive AutoCAD to draw 2D architectural floor
plans — walls, doors, windows, rooms with computed areas, stair cores, lifts,
riser shafts, grids and dimensions — either into a **live, running AutoCAD
session** or into a **headless DXF file**.

It is the AutoCAD counterpart to [cadpilot](https://github.com/LBurny/cadpilot):
same idea, same "the AI issues modelling operations" shape, but the target is a
2D drawing rather than a 3D model.

```
AI client  ──MCP──▶  acadmcp  ──┬── COM (pywin32) ──▶  running AutoCAD 2026
                                └── ezdxf         ──▶  .dxf file (+ PNG preview)
```

## Why two backends

| | `dxf` | `com` |
|---|---|---|
| Needs AutoCAD running | no | yes |
| Speed | milliseconds | AutoCAD round-trips |
| Live editing afterwards | after you open the file | immediately, entities appear as the AI works |
| Screenshot | clean re-render, no UI | photographs the AutoCAD window |
| Good for | generating a plan from scratch, batch work, CI | interactive design, editing an existing drawing |

All geometry is computed once in `acadmcp/geom.py`, so a plan drawn through COM
and the same plan written to DXF contain identical coordinates.

## Install

```bash
uv sync --extra preview      # or: pip install -e ".[preview]"
```

`preview` pulls in matplotlib, which is only needed for PNG rendering.

## Run the server

```bash
uv run acadmcp                # stdio transport
```

Register it with your MCP client, e.g. in ZCode's `config.json`:

```json
{
  "mcp": {
    "servers": {
      "acadmcp": {
        "type": "stdio",
        "command": "uv",
        "args": ["--directory", "/path/to/AutoCAD-MCP", "run", "acadmcp"]
      }
    }
  }
}
```

## Try it without an AI client

```bash
uv run python examples/floor_plan_demo.py            # DXF + PNG preview
uv run python examples/floor_plan_demo.py --com      # draw into running AutoCAD
```

## Tool surface

**Session** — `drawing_help`, `drawing_status`, `drawing_new`, `drawing_open`,
`acad_connect`, `acad_zoom_extents`, `acad_zoom_window`, `drawing_save`,
`drawing_export_dxf`, `drawing_preview`, `drawing_screenshot`, `listing_entities`

**Layers and text** — `layer_create`, `layer_create_standard`, `set_text_style`

**Primitives** — `draw_line`, `draw_polyline`, `draw_rect`, `draw_circle`, `draw_arc`

**Architecture** — `draw_wall`, `draw_wall_with_openings`, `draw_room`,
`add_room_label`, `draw_partition`, `draw_door`, `draw_window`, `draw_column`

**Core and services** — `draw_stair`, `draw_lift`, `draw_shaft`, `draw_equipment`

**Annotation** — `draw_axis_grid`, `add_text`, `add_leader_note`,
`add_dimension`, `add_dimension_chain`, `add_hatch`

### The two calls that matter most

`draw_wall_with_openings` is the workhorse for reproducing a real plan. Instead of
drawing wall faces and then trying to place door symbols on top of them, you give
it a wall run and a list of openings measured in millimetres along that run, and
it splits the wall, inserts the symbols and keeps everything consistent:

```python
draw_wall_with_openings(
    points=[(0, 0), (12000, 0)],
    thickness=240,
    openings=[
        {"start": 1200, "width": 1500, "kind": "door", "swing": "left", "tag": "FM乙1521"},
        {"start": 4200, "width": 1200, "kind": "window", "tag": "C1212"},
    ],
)
```

`draw_room` walls a ring in and reports the floor area it actually encloses —
measured to the inner face of the walls, not the centreline, so the number is
usable in a room schedule:

```python
draw_room(boundary=[(0, 0), (4000, 0), (4000, 3000), (0, 3000)], name="无菌室", thickness=200)
# -> {"name": "无菌室", "area_m2": 10.64, "centroid": (2000.0, 1500.0)}
```

## Looking at the drawing

`drawing_screenshot` returns an **image content block**, so a multimodal model
sees the drawing directly rather than being told a file path:

```python
drawing_screenshot()  # the whole drawing
drawing_screenshot(window=[0, 3600, 7000, 8000])  # just the lab suite
```

`window` is `[min_x, min_y, max_x, max_y]` in millimetres. Use it. A whole floor
plan scaled into a 1600 px image leaves the room labels as unreadable smudges;
framing the region you changed is what makes a screenshot worth taking.

On the `dxf` backend the image is a clean re-render with no UI around it. On the
`com` backend it is a photograph of the AutoCAD window — the true current view
and zoom, with AutoCAD's ribbon and panels included. A minimised AutoCAD window
is restored automatically before the capture. `acad_zoom_window` frames the live
view explicitly, which is what you want before a capture if the model needs to
read text:

```python
acad_zoom_window(corner1=[0, 3600], corner2=[7000, 8000])
drawing_screenshot()
```

`drawing_preview` still exists but only returns a path — it is for scripts that
want a file, not for looking.

## Conventions

- **Units are millimetres** everywhere. `INSUNITS` is set to 4.
- **Angles are degrees**, counter-clockwise from +X.
- Layers follow the GB/T 50001 scheme: `A-WALL`, `A-DOOR`, `A-WINDOW`,
  `A-ROOM-NAME`, `A-DIM`, `A-AXIS`, `A-STAIR`, `A-LIFT`, `A-SHAFT`, …
- Room boundaries always mean the **wall centreline ring**; areas are reported for
  the inner face, so a 4000×3000 ring with 200 mm walls reports 3.8×2.8 m.

## Notes and limits

- The COM backend needs AutoCAD installed and, for the first call, running. It
  attaches with `GetActiveObject("AutoCAD.Application")` and falls back to
  launching one. Verified against AutoCAD 2026 (release 25.1).
- AutoCAD answers `RPC_E_CALL_REJECTED` while it is mid-command or repainting,
  which a few-hundred-entity run will hit. Every COM call is retried with
  backoff, so a drawing run does not die on a transient.
- Saving through the COM backend uses `SaveAs`, which **renames** the open
  drawing. Prefer `drawing_export_dxf` to write a copy.
- The `mcp` SDK renamed `FastMCP` to `MCPServer` in 2.x; the server imports
  whichever is present, so both the 1.x and 2.x SDKs work.
- `draw_wall_with_openings` handles straight runs and polyline runs; openings are
  measured along the run from its start, and one that runs off the end is
  trimmed and reported in `clamped_openings`.
- Door and window symbols are drawn as plain line/arc geometry rather than block
  references, so they behave identically on both backends. If you need blocks
  with attributes for scheduling, that is the next thing to add.

- Screenshotting the `com` backend needs an interactive desktop session: it
  captures the window through `PrintWindow` with `PW_RENDERFULLCONTENT`, because
  AutoCAD renders through DirectX and a plain capture comes back black. It will
  not work from a service session with no visible desktop.

### Chinese text

Getting CJK to show up in AutoCAD rather than as `?` took three findings, all
measured on AutoCAD 2026 here:

- A stock drawing's `Standard` style is `txt.shx` with **no big font**. An SHX
  font without a big font has no CJK glyphs, so every Chinese character renders
  as `?`. Text drawn through the COM backend goes on an `HZ` style this package
  creates, set to `gbenor.shx` + `gbcbig.shx`. That pair is what actually worked;
  a CJK TrueType face did not — `simhei.ttf` still drew `?` in AutoCAD, even
  though it renders correctly in the PNG preview.
- Repointing a style's font does **not** repaint text already in the drawing.
  `set_text_style` issues a regen for exactly that reason.
- Superscript-two (`m²`, U+00B2) renders under every font tested. The single
  codepoint square-metre sign (`㎡`, U+33A1) renders under **none** of them — not
  under `gbcbig.shx`, `simhei.ttf` or `simsun.ttc`. Areas are therefore quoted
  as `m²`.

The DXF backend uses `simsun.ttc` for its own style, because SimSun has both CJK
and the superscript-two glyph, while SimHei has CJK but no superscript two.

## Layout

```
src/acadmcp/
  geom.py             pure 2D geometry: offsets, areas, mitre joins (no CAD deps)
  layers.py           drafting standard: layer names, ACI colours, text heights
  ops.py              architectural operations built on a backend
  capture.py          screen capture of a top-level window (for the com backend)
  server.py           the MCP tool surface
  backends/
    base.py           the backend contract
    dxf.py            ezdxf driver, renders to PNG
    com.py            ActiveX driver for a live AutoCAD
examples/floor_plan_demo.py
tests/
```
