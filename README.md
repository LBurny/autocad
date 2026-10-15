# AutoCAD-MCP

An MCP server that lets an AI client drive AutoCAD to draw 2D architectural floor
plans — walls, doors, windows, rooms with computed areas, stair cores, lifts,
riser shafts, grids and dimensions — into a **live AutoCAD session** or a
**headless DXF file**. It is the 2D counterpart to
[cadpilot](https://github.com/LBurny/cadpilot).

```
AI client  ──MCP──▶  acadmcp  ──┬── COM (pywin32) ──▶  running AutoCAD 2026
                                └── ezdxf         ──▶  .dxf file (+ PNG preview)
```

## Why three backends

| | `dxf` | `com` | `plugin` |
|---|---|---|---|
| Needs AutoCAD | no | yes | yes + plugin installed |
| Entities appear | on file open | live, as the AI works | live, one step at a time |
| Undo | per step, journaled in the .dxf | per step, in memory | per step, journaled in the .dwg + palette |
| Catalogue element | parametric twin | parametric twin | library block, inserted |
| Good for | batch generation, CI | editing an existing drawing | reviewing a plan as it is drawn |

Geometry is computed once in `geom.py`, so every backend produces identical
coordinates. Every tool call is one journal step, so a mistake costs
`drawing_undo()`, not a redraw — and a headless plan replays into live AutoCAD
with one `drawing_replay()`. The plugin backend is for reviewing as you draw:
its journal lives in the DWG and rolls back from a docked palette, with or
without this server running. See [docs/plugin.md](docs/plugin.md).

The catalogue places on all three backends: `place_element` inserts the library
block wherever the backend can insert one (the plugin), and draws a parametric
twin where it cannot (`dxf`, `com`) — so one element name works headless and in
AutoCAD. The drawing keeps the catalogue that drew it, so reopening it elsewhere
resolves the same elements rather than silently different ones.

## Install

```bash
uv sync --extra preview      # the preview extra is matplotlib, for PNG output
```

Register with your MCP client, e.g. in ZCode's `config.json`:

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
`acad_connect` (`mode="com"|"plugin"`), `acad_zoom` (corners optional), `drawing_save`
(.dwg or .dxf), `drawing_screenshot` (`inline=False` for just a PNG path),
`listing_entities`

**Journal** — `drawing_undo`, `drawing_redo`, `drawing_history`, `drawing_rollback_to`,
`drawing_replay`, `drawing_summary`

**Layers and text** — `layer_create` (no name = the standard set), `set_text_style`

**Primitives** — `draw_line`, `draw_polyline`, `draw_rect`, `draw_circle`
(angles make it an arc)

**Architecture** — `draw_wall` (thickness=120 for a partition),
`draw_wall_with_openings`, `draw_room` (`walls=False` to label only), `draw_door`,
`draw_window`, `draw_column`

**Core and services** — `draw_stair`, `draw_lift`, `draw_equipment`
(`kind="shaft"` for a riser)

**Annotation** — `draw_axis_grid`, `add_text`, `add_leader_note`,
`add_dimension` (2 points or a chain), `add_hatch`

**Library elements** — `library_list`, `element_types`, `place_element`,
`place_along`, `place_grid`

### The calls that matter most

`draw_wall_with_openings` is the workhorse: give it a wall run and openings in
millimetres along it, and it splits the wall and inserts the symbols
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

`place_along` / `place_grid` repeat a library block at every position, sized
through its dynamic parameters and numbered from the drawing's existing tags —
the whole run lands as one undoable step:

```python
place_grid("stud", xs=[0, 6000, 12000], ys=[0, 6000])  # a frame of columns
place_along("window", start=[0, 0], end=[12000, 0], spacing=3000, variant="1200")
```

The block behind each element is a plain JSON catalogue (`element_types` shows
it and flags entries whose block your library lacks); a project points it at
its own library and layers with `acadmcp.json`. An entry may also name a
parametric twin, so an element that has no block to insert — or a backend that
cannot insert one — still draws it, and one element name means the same thing on
all three backends.

`draw_room` walls a ring in and reports the area to the *inner* face of the
walls, so the number is usable in a room schedule:

```python
draw_room(boundary=[(0, 0), (4000, 0), (4000, 3000), (0, 3000)], name="Meeting room", thickness=200)
# -> {"name": "Meeting room", "area_m2": 10.64, "centroid": (2000.0, 1500.0)}
```

## Looking at the drawing

`drawing_screenshot` returns an image content block, so a multimodal model sees
the drawing directly. Pass `window=[min_x, min_y, max_x, max_y]` to frame a
region — a whole plan at 1600 px leaves room labels unreadable. On `dxf` it is
a clean re-render; on `com` a photograph of the AutoCAD window (needs an
interactive desktop: the capture goes through `PrintWindow` with
`PW_RENDERFULLCONTENT`).

```python
acad_zoom(corner1=[0, 3600], corner2=[7000, 8000])  # frame the live view first
drawing_screenshot()
```

## Conventions

- **Millimetres everywhere** (`INSUNITS` = 4); **degrees** counter-clockwise from +X.
- Layers follow GB/T 50001: `A-WALL`, `A-DOOR`, `A-WINDOW`, `A-ROOM-NAME`,
  `A-DIM`, `A-AXIS`, `A-STAIR`, `A-LIFT`, `A-SHAFT`, …
- Room boundaries are the **wall centreline ring**; areas are measured to the
  inner face (a 4000×3000 ring with 200 mm walls reports 3.8×2.8 m).
- Steps are the unit of review: `step_begin` … `step_commit` groups drawing
  calls into one step the palette and rollback act on.

## Notes and limits

- The COM backend attaches with `GetActiveObject("AutoCAD.Application")`,
  launching AutoCAD if needed. Verified against AutoCAD 2026 (release 25.1).
  AutoCAD answers `RPC_E_CALL_REJECTED` while busy; every COM call retries with
  backoff, so a long run does not die on a transient.
- Saving through COM uses `SaveAs`, which **renames** the open drawing — pass
  `drawing_save` a .dxf path for a copy.
- `draw_wall_with_openings(closed=True)` takes the envelope as a ring and mitres
  the corners; drawn as four separate runs, each corner steps in by half the
  wall thickness. An opening past the end is trimmed and reported in
  `clamped_openings`.
- Door and window symbols are plain line/arc geometry, not block references, so
  they behave identically on every backend.
- The `mcp` SDK renamed `FastMCP` to `MCPServer` in 2.x; both are imported, so
  either SDK generation works.

## Layout

```
src/acadmcp/
  geom.py        pure 2D geometry (no CAD deps)     server.py     the 47-tool MCP surface
  ops.py         backend-agnostic operations        journal.py    step bookkeeping
  backends/      dxf / com / plugin drivers         catalog.py    the element catalogue
  catalog_ops.py the parametric twins               summary.py    design summary from the journal
  steps.py       journal control actions            acad_plugin_client.py
plugin/          the in-AutoCAD step plugin (docs/plugin.md)
examples/        floor_plan_demo.py                 evaluations/  read-only QA harness
tests/           pytest, no AutoCAD needed
```
