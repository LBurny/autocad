# AutoCAD-MCP

An MCP server that lets an AI client draw 2D architectural floor plans in
AutoCAD: walls, doors, windows, rooms with computed areas, stair cores, lifts,
riser shafts, grids and dimensions. Draw into a live AutoCAD session, or write a
headless DXF file when there is no AutoCAD to draw into. It is the 2D
counterpart to [cadpilot](https://github.com/LBurny/cadpilot).

![An AI client drives acadmcp over MCP; acadmcp reaches a running AutoCAD 2026
through COM (pywin32), or writes a .dxf file with a PNG preview through
ezdxf](docs/flow.png)

## Why three backends

| | `dxf` | `com` | `plugin` |
|---|---|---|---|
| Needs AutoCAD | no | yes | yes + plugin installed |
| Entities appear | on file open | live, as the AI works | live, one step at a time |
| Undo | per step, journaled in the .dxf | per step, in memory | per step, journaled in the .dwg + palette |
| Catalogue element | parametric twin | parametric twin | library block, inserted |
| Good for | batch generation, CI | editing an existing drawing | reviewing a plan as it is drawn |

The geometry is computed once, in `geom.py`, so all three drive the same
coordinates: a plan written to DXF and the same plan drawn through COM hold the
same numbers. Every tool call is one journal step, so a wrong coordinate costs
one `drawing_undo()` instead of a redraw, and a headless plan replays into live
AutoCAD with one `drawing_replay()`.

The catalogue places on all three backends. `place_element` inserts the library
block wherever the backend can insert one, and draws a parametric twin where it
cannot, so one element name means the same thing headless and live. The drawing
keeps the catalogue that drew it, so reopening it elsewhere resolves the same
elements rather than silently different ones.

The plugin backend is the one for watching a plan appear: its journal lives in
the DWG and rolls back from a docked palette, with or without this server
running. See [docs/plugin.md](docs/plugin.md).

## Install

```bash
uv sync --extra preview      # matplotlib, needed for any PNG output
```

Register the server with your MCP client. The entry is the same stdio command
everywhere, `uv --directory /path/to/AutoCAD-MCP run acadmcp`, so only the file
and the key it goes under differ.

Claude Code, in the project's `.mcp.json` (or through `claude mcp add`):

```json
{
  "mcpServers": {
    "acadmcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/path/to/AutoCAD-MCP", "run", "acadmcp"]
    }
  }
}
```

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.acadmcp]
type = "stdio"
command = "uv"
args = ["--directory", "/path/to/AutoCAD-MCP", "run", "acadmcp"]
```

OpenCode, in `opencode.json`:

```json
{
  "mcp": {
    "acadmcp": {
      "type": "local",
      "command": ["uv", "--directory", "/path/to/AutoCAD-MCP", "run", "acadmcp"],
      "enabled": true
    }
  }
}
```

ZCode and every other client that speaks stdio MCP take the same entry under
their own key.

## Try it without an AI client

```bash
uv run python examples/floor_plan_demo.py            # DXF + PNG preview
uv run python examples/floor_plan_demo.py --com      # draw into running AutoCAD
```

## Tool surface

**Session**: `drawing_help`, `drawing_status`, `drawing_new`, `drawing_open`,
`acad_connect` (`mode="com"|"plugin"`), `acad_zoom`, `drawing_save`,
`drawing_screenshot` (`inline=False` for just a PNG path), `listing_entities`

**Sheet and plot**: `drawing_sheet`, `drawing_plot_pdf`

**Journal**: `drawing_undo` (by count, or `to_step=` aimed at a step),
`drawing_redo`, `drawing_history`, `drawing_replay`, `drawing_summary`

**Checks**: `drawing_check` (the `verify`/`critique`/`standard` lenses) and
`drawing_finalize`, the gate that refuses a drawing until the first two are clean

**Layers and text**: `layer_create`, `set_text_style`

**Primitives**: `draw_line`, `draw_polyline`, `draw_rect`, `draw_circle`

**Architecture**: `draw_wall`, `draw_wall_with_openings`, `draw_room`,
`draw_opening` (`kind="door"|"window"`), `draw_column`

**Core and services**: `draw_core` (`kind="stair"|"lift"|"equipment"|"shaft"`)

**Annotation**: `draw_axis_grid`, `add_text`, `add_leader_note`,
`add_dimension`, `add_hatch`

**Library elements**: `library_list`, `element_types`, `place_element`
(single, run and grid placements), `element_capture`

**Standards, templates and the accumulating library**: `template_list`,
`element_publish`, `catalog_suggest`

**Prompts**: `floor_plan`, `headless_to_live`, `drawing_review`

### The calls that matter most

`draw_wall_with_openings` is the workhorse. Give it a wall run and openings
measured in millimetres along it, and it splits the wall and inserts the symbols
consistently:

```python
draw_wall_with_openings(
    points=[(0, 0), (12000, 0)],
    thickness=240,
    openings=[
        {"start": 1200, "width": 1500, "kind": "door", "swing": "left", "tag": "M1521"},
        {"start": 4200, "width": 1200, "kind": "window", "tag": "C1212"},
    ],
)
```

`place_element` also repeats a library block along a run or across a grid, sized
through its dynamic parameters and numbered from the tags already in the drawing.
The whole run lands as one undoable step:

```python
place_element("stud", xs=[0, 6000, 12000], ys=[0, 6000])  # a frame of columns
place_element("window", start=[0, 0], end=[12000, 0], spacing=3000, variant="1200")
```

The block behind each element is a plain JSON catalogue, and `element_types`
shows it and flags entries whose block your library has not got. A project points
it at its own library and layers with `acadmcp.json`.

`draw_room` walls a ring in and reports the area to the inner face of the walls,
so the number is usable in a room schedule:

```python
draw_room(boundary=[(0, 0), (4000, 0), (4000, 3000), (0, 3000)], name="Meeting room", thickness=200)
# -> {"name": "Meeting room", "area_m2": 10.64, "centroid": (2000.0, 1500.0)}
```

## Looking at the drawing

`drawing_screenshot` returns an image content block, so a multimodal model sees
the drawing directly. Pass `window=[min_x, min_y, max_x, max_y]` to frame a
region, because a whole plan at 1600 px leaves the room labels unreadable.

```python
acad_zoom(corner1=[0, 3600], corner2=[7000, 8000])  # frame the live view first
drawing_screenshot()
```

On `dxf` that is a clean re-render of the file; on `com` a photograph of the
AutoCAD window.

## Putting it on paper

A plan is a plan. A drawing is a plan on paper of a named size, at a named scale,
inside a frame with a title block. `drawing_sheet` does all of that in one call:

```python
drawing_sheet(paper="A3", scale="1:100", title="Production hall plan", number="JS-01")
drawing_plot_pdf("sheet.pdf")
```

The page setup goes onto the drawing's own layout, so a DXF from here opens in
AutoCAD with the sheet already configured. The frame, the title block and the
viewport are drawn at 1:1 in paper millimetres, and the title block always states
its scale. The reply also says whether the plan fits: at 1:100 an A3 shows
38 × 22.1 m, so a 40 m bay is cut off, and you get `fits: false` and the largest
scale that holds it rather than finding out at the printer.

`drawing_plot_pdf` writes a PDF the size of the paper and then reads the size back
out of the PDF itself, answering `verified` from the file's own `/MediaBox`. Both
tools are `dxf`-only for now: the live backends have no paperspace path and refuse
by name.

## Conventions

- Millimetres everywhere (`INSUNITS` = 4). Angles are degrees, counter-clockwise
  from +X.
- Layers follow GB/T 50001: `A-WALL`, `A-DOOR`, `A-WINDOW`, `A-ROOM-NAME`,
  `A-DIM`, `A-AXIS`, `A-STAIR`, `A-LIFT`, `A-SHAFT`, and the rest.
- A room boundary is the wall centreline ring, and the area is measured to the
  inner face, so a 4000×3000 ring with 200 mm walls reports 3.8×2.8 m.
- Steps are the unit of review. `step_begin` and `step_commit` group drawing calls
  into one step, which is what the palette and `drawing_undo(to_step=...)` act on.

## Notes and limits

- COM attaches with `GetActiveObject("AutoCAD.Application")` and launches AutoCAD
  if it is not already running, verified against AutoCAD 2026 (release 25.1). It
  answers `RPC_E_CALL_REJECTED` while busy, so every call retries with backoff and
  a long run does not die on a transient.
- Saving through COM renames the open drawing, because it goes through `SaveAs`.
  Pass `drawing_save` a .dxf path to get a copy instead.
- `draw_wall_with_openings(closed=True)` takes a building envelope as a ring and
  mitres the corners. Drawn as four separate runs, every corner steps in by half
  the wall thickness. An opening past the end of a run is trimmed and reported in
  `clamped_openings`.
- Door and window symbols are plain line and arc geometry rather than block
  references, so they behave identically on every backend.

## Layout

```
src/acadmcp/
  geom.py        pure 2D geometry (no CAD deps)     server.py     the 48-tool MCP surface
  ops.py         backend-agnostic operations        journal.py    step bookkeeping
  backends/      dxf / com / plugin drivers         catalog.py    the element catalogue
  catalog_ops.py the parametric twins               summary.py    design summary from the journal
  steps.py       journal control actions            acad_plugin_client.py
plugin/          the in-AutoCAD step plugin (docs/plugin.md)
examples/        floor_plan_demo.py                 evaluations/  read-only QA harness
tests/           pytest, no AutoCAD needed
```
