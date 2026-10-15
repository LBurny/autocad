"""The MCP prompt templates: workflow, conventions and verification gates as text.

Pure text — this module imports nothing CAD-related and nothing from the package,
the same rule `terms.py` follows. A prompt that names a tool is checked against
the live tool registry in `tests/test_prompts.py`, so a renamed tool breaks the
suite rather than a client's session.
"""

from __future__ import annotations

from typing import Any


def floor_plan(building_name: str = "Building A", scale: str = "1:100") -> str:
    """Workflow for an architectural floor plan, from envelope to verified sheet."""
    return f"""You are drawing an architectural floor plan for '{building_name}' at {scale}
with this server's high-level ops. Millimetres everywhere; degrees counter-clockwise
from +X. One tool call is one journal step, so a mistake costs one drawing_undo —
never redraw what an undo can take back.

WORKFLOW:
1. drawing_new(backend="dxf") — draft headless even when AutoCAD is available;
   drawing_replay(backend="plugin") carries the finished plan into AutoCAD later.
   Pass template="gb_a3_arch" to start on the shipped A3 architectural sheet instead
   of a bare drawing: the template sets the standard's layers, the page setup and the
   frame as one step, and template_list names the templates available.
2. Building envelope: ONE call of draw_wall_with_openings(points=<ring>,
   closed=True, thickness=200, openings=[...]). Never four separate wall runs —
   each run has square-cut ends and leaves a half-wall notch at every corner.
   An opening is {{"start": mm along the ring, "width": mm,
   "kind": "door"|"window"|"opening", "swing": "left"|"right", "tag": "M0921"}}.
3. Interior partitions: draw_wall(thickness=120), or draw_wall_with_openings
   again when the partition carries doors.
4. Rooms: draw_room(boundary=<wall CENTRELINE ring>, name="...") per room. The
   boundary is the centreline, not the inner face; the reported area is measured
   to the inner face, so a 4000x3000 ring with 200 mm walls reports 3.8x2.8 m.
   When the walls exist already, pass walls=False so only the label is drawn.
   Room names may be Chinese (生产车间, 办公室) — the HZ text style carries CJK
   and the label shrinks to fit the room.
5. Columns and fixtures: place_element(type="column", at=[x, y]); element_types
   lists what the catalogue offers, and the catalogue places on every backend.
6. Cores: draw_stair, draw_lift, draw_equipment(kind="shaft").
7. draw_axis_grid(...) for the structural grid, then add_dimension(points=[...])
   for the dimension chains.
8. drawing_sheet(paper="A3", scale="{scale}") — read plan_mm and fits in the
   reply; when fits is false, the note names the largest scale that holds the
   plan whole. A sheet narrower than the building is a design choice, not an
   error — but say so in your report.

VERIFY — all five, every time:
- drawing_screenshot() and LOOK at it; zoom into regions with
  drawing_screenshot(window=[min_x, min_y, max_x, max_y]) until the room labels
  are legible in the shot.
- drawing_verify() — the verdict must be "matches"; "unaccounted" means
  something drew outside a step, "missing" means entities were lost.
- drawing_summary() — rooms with areas, opening and element counts; read it
  back against the brief you were given.
- drawing_critique() — the components against the journal: a door off its wall
  or on an uncut one, an opening past its run, a tag breaking convention, a
  plan the viewport cannot hold.
- drawing_check_standard() — the drawing against the drafting standard: the unit,
  the layers in use, every text height against the standard's nominal series, and
  that the sheet states its 比例. It reports, it never blocks — but read it.

Then drawing_finalize() — the done-gate. It refuses while verify or critique
find errors, so "finished" is never a claim you make unverified.

RECOVERY: drawing_history() lists the steps; drawing_undo() takes back the last
call; drawing_rollback_to(index) erases everything after a named step.
"""


def headless_to_live() -> str:
    """Draft headless in DXF, review the PNGs, then replay into live AutoCAD."""
    return """This server draws the same plan into a headless DXF file or into a live
AutoCAD session, and a plan drafted headless is replayed into AutoCAD without
being redrawn. Prefer that route whenever the drawing is more than a few calls.

WORKFLOW:
1. drawing_new(backend="dxf", path="...") and draw the whole plan headless —
   every drawing tool works without AutoCAD.
2. drawing_screenshot() after each stage and look at it; the preview renders
   CJK text natively, so a Chinese title reads the same as it will in AutoCAD.
3. drawing_save() — the step journal travels inside the .dxf, so a later
   drawing_open(path) keeps every step undoable across sessions.
4. acad_connect(mode="plugin") to attach to the running AutoCAD. The plugin is
   installed once per machine with plugin_install; it puts each step and its
   journal write in one transaction, so one Ctrl+Z in AutoCAD undoes one step.
5. drawing_replay(backend="plugin") re-draws the applied journal step by step.
   Steps the target cannot apply — a library insert onto the dxf backend, going
   the other way — are skipped and REPORTED in the reply's skipped list, never
   silently dropped. Read that list.
6. drawing_verify() on the live side must say "matches", and
   drawing_screenshot() now shows the AutoCAD window itself.

RULES:
- Never redraw by hand what a replay can carry; redrawing doubles the journal.
- The headless draft is disposable; the replayed drawing is the deliverable, so
  the verification gate runs on the live side, not only on the draft.
"""


def drawing_review() -> str:
    """Review the current drawing: summary, journal reconciliation, screenshots."""
    return """Review the current drawing and report what is wrong with it. Never claim
"no issues" without running all four checks — a review that ran one says
nothing, and "I could not check" is reported as such, never treated as fine.

CHECKS:
1. drawing_summary() — rooms with areas (recomputed from the recorded boundary,
   the same geometry the room tool used), openings, elements, and annotation
   counts. Rows it cannot read are counted in unreadable, never skipped
   silently; a non-zero unreadable is itself a finding.
2. drawing_verify() — the journal reconciled against the drawing. "matches"
   means every applied step's entities are present; "unaccounted" means the
   drawing holds more than any step recorded (something drew outside a step);
   "missing" means fewer (a save dropped them, or an erase went too far).
3. drawing_critique() — the components against the journal: doors off walls or
   on uncut ones, openings past their run, tags breaking convention, text
   masquerading as a dimension, a sheet too small for the plan. An error here
   is a finding even when verify says "matches": verify counts entities, the
   critique checks they are right.
4. drawing_screenshot() — then zoom into each region with
   drawing_screenshot(window=[min_x, min_y, max_x, max_y]) until the room labels
   are legible in the shot; a label too small to read at review scale is a
   finding. When a sheet was laid out, drawing_status reports the paper and
   scale, and the sheet call's own reply said whether the plan fits the
   viewport — a plan cut off by its viewport is a finding.

REPORT: one line per finding with its layer and location, then the counts from
drawing_summary(). Separate "the drawing is wrong" from "I could not check
this"; both belong in the report, and the second is never silently dropped.
"""


def register(mcp: Any) -> None:
    """Attach every prompt in this module to the server object."""
    mcp.prompt(
        name="floor_plan",
        description="Workflow for an architectural floor plan: envelope, rooms, "
        "elements, sheet, and the verification gates.",
    )(floor_plan)
    mcp.prompt(
        name="headless_to_live",
        description="Draft headless in DXF, review the PNGs, then replay into "
        "live AutoCAD without redrawing.",
    )(headless_to_live)
    mcp.prompt(
        name="drawing_review",
        description="Review the current drawing: summary, journal reconciliation, "
        "screenshots — and an honest report.",
    )(drawing_review)
