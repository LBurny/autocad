"""One description per tool parameter, injected into the served inputSchema.

The SDK builds each tool's input schema from the bare signature, so a parameter's
type reaches the client but its meaning does not — a caller meeting ``draw_room``'s
``boundary`` for the first time is told it is an array, not that it is the wall
centreline ring in millimetres. This table is the meaning, keyed by tool name and
then parameter name, and ``apply`` writes it into each tool's served schema.

The table is data, like ``terms.py`` and ``replies.py``, and the same rule keeps it
honest: ``tests/test_param_descriptions.py`` fails on a parameter with no
description and on an entry that names no tool or parameter, so the table cannot
drift from the surface it documents. Application at import is tolerant (drift is
returned, never raised) because a stale table must not take the server down.

No imports, no CAD — descriptions only.
"""

from __future__ import annotations

DESCRIPTIONS: dict[str, dict[str, str]] = {
    "drawing_help": {
        "term": "A word to look up: an AutoCAD command name ('HATCH', '-INSERT'), an "
        "English phrase, or a Chinese one ('承重柱', '房间面积'). Omit for the whole surface.",
    },
    "drawing_new": {
        "backend": "'dxf' (default) for a headless document, 'com' for a running AutoCAD.",
        "path": "File the drawing belongs to; written on drawing_save, not read now.",
        "template": "A key from template_list (e.g. 'gb_a3_arch') to start on a named "
        "sheet with its frame and title block; empty for a bare drawing.",
    },
    "drawing_open": {
        "path": "Path to an existing .dxf file to edit.",
    },
    "drawing_sheet": {
        "paper": "ISO A-series paper name, 'A0' through 'A4'.",
        "scale": "Plot scale as a ratio string, e.g. '1:100'.",
        "orientation": "'landscape' (default) or 'portrait'.",
        "title": "图名, written into the title block.",
        "number": "图号, written into the title block.",
        "sheet_of": "张次, written into the title block.",
        "style_sheet": "The office's plot style (.ctb) name, when there is one.",
    },
    "acad_connect": {
        "mode": "'com' draws live over ActiveX; 'plugin' draws through the in-AutoCAD "
        "step plugin, so every step is reviewable in the palette.",
        "new_document": "True starts a new drawing instead of attaching to the active one.",
        "visible": "True shows the AutoCAD window when it has to be launched.",
    },
    "acad_zoom": {
        "corner1": "One corner of the rectangle to frame, [x, y] in mm. "
        "Omit both corners to fit the whole drawing.",
        "corner2": "The opposite corner, [x, y] in mm.",
    },
    "drawing_save": {
        "path": "Target path — .dwg or .dxf as the suffix says. "
        "Omit to save to the drawing's own path.",
    },
    "drawing_undo": {
        "count": "How many steps to take back, newest first. Omit for just the last one.",
        "to_step": "Erase everything drawn AFTER this step instead — 0 empties the "
        "drawing. drawing_history lists the indices. Give count or to_step, not both.",
    },
    "drawing_redo": {
        "count": "How many undone steps to re-draw, oldest first.",
    },
    "drawing_check": {
        "check": "Which read-only lens to run: 'verify' (the drawing against the "
        "journal's own account), 'critique' (the components against the journal), "
        "'standard' (the GB/T drafting standard), or 'all' (the default) for the "
        "three in one call. An unknown name is refused, naming the valid ones.",
        "focus": "Critique lens only: run one check by its code; empty runs them all. "
        "Refused when the critique lens does not run.",
        "standard": "Standard lens only: name one of the loaded GB/T standards; empty "
        "uses the standard in force. Refused when the standard lens does not run.",
        "limit": "Verify lens only: maximum entities to walk; a walk stopped here "
        "makes the count a floor and the verdict 'walk_truncated'.",
    },
    "drawing_replay": {
        "backend": "Target backend; 'plugin' is how a headless session moves into AutoCAD.",
        "path": "Target file when the target backend is 'dxf'.",
    },
    "drawing_screenshot": {
        "path": "PNG file to write; with inline=False this path is the whole reply.",
        "window": "[min_x, min_y, max_x, max_y] in mm to frame a region rather than "
        "the whole drawing — zoom in when room labels need reading.",
        "width": "Image width in pixels.",
        "height": "Image height in pixels.",
        "theme": "'dark' (white-on-dark, default) or 'light' (black-on-white).",
        "inline": "False writes the PNG and returns only its path, for scripts that "
        "need a file on disk rather than a picture to look at.",
    },
    "drawing_plot_pdf": {
        "path": "PDF file to write.",
        "theme": "'light' (default) or 'dark'.",
    },
    "listing_entities": {
        "layer": "Only list this layer; omit for the whole drawing, paper layouts "
        "included. An unknown layer name is reported in layer_known.",
        "limit": "Maximum entities to return in this page.",
        "offset": "Where the page starts — a previous reply's next_offset.",
    },
    "layer_create": {
        "name": "Layer name. Omit to create the full standard architectural set "
        "(A-WALL, A-DOOR, ...).",
        "color": "ACI colour index.",
        "linetype": "Linetype name.",
    },
    "set_text_style": {
        "name": "The text style to create or repoint (default 'HZ'). Repointing does "
        "not repaint existing text — a regen is issued for that.",
        "font": "Font file or TrueType family; simsun.ttc carries CJK glyphs.",
        "big_font": "Big font file for an SHX style, e.g. gbcbig.shx — an SHX font "
        "without one renders every Chinese character as '?'.",
    },
    "draw_line": {
        "p1": "Start point, [x, y] in mm.",
        "p2": "End point, [x, y] in mm.",
        "layer": "Target layer.",
        "linetype": "Linetype name; omit for the layer's own.",
    },
    "draw_polyline": {
        "points": "Vertices as a list of [x, y] in mm.",
        "closed": "True closes the ring.",
        "layer": "Target layer.",
    },
    "draw_rect": {
        "corner1": "One corner, [x, y] in mm.",
        "corner2": "The opposite corner, [x, y] in mm.",
        "layer": "Target layer.",
    },
    "draw_circle": {
        "center": "Centre point, [x, y] in mm.",
        "radius": "Radius in mm.",
        "start_deg": "Arc start in degrees, counter-clockwise from +X. "
        "Omit both angles for a full circle.",
        "end_deg": "Arc end in degrees, counter-clockwise from +X.",
        "layer": "Target layer.",
    },
    "draw_wall": {
        "points": "Wall centreline as a list of [x, y] in mm.",
        "thickness": "Wall thickness in mm; 120 makes a light internal partition.",
        "closed": "True reads points as a closed ring, mitring the corners — the way "
        "to draw a room envelope.",
        "hatch": "True pochés the wall.",
        "layer": "Target layer.",
    },
    "draw_wall_with_openings": {
        "points": "Wall centreline run — or the envelope ring with closed=True — as a "
        "list of [x, y] in mm.",
        "openings": "List of {'start': mm along the run, 'width': mm, 'kind': "
        "'door'|'window'|'opening', 'swing': 'left'|'right', 'tag': 'M1522'}; an opening "
        "may also name an 'element' to place into it, with the measured width authoritative.",
        "thickness": "Wall thickness in mm.",
        "hatch": "True pochés the wall.",
        "closed": "True reads points as the building envelope ring, openings measured "
        "from the first vertex — this is how an envelope gets mitred corners instead "
        "of a square-capped notch at each one.",
    },
    "draw_room": {
        "boundary": "The wall centreline ring as a list of [x, y] in mm; the reported "
        "area is measured to the inner face.",
        "name": "Room name; the label is shrunk to fit the room.",
        "thickness": "Wall thickness in mm — must match the walls actually drawn, or "
        "the reported area shrinks silently.",
        "hatch": "True pochés the walls.",
        "show_area": "False labels the room without the m² figure.",
        "bound_layer": "Also draw the inner face the area is measured to, on this layer "
        "(e.g. 'A-ROOM-BOUND').",
        "walls": "False when the walls exist already — only the label (and the optional "
        "bound face) is drawn.",
        "label_offset": "Raise the label along +Y in mm when a stair or equipment "
        "occupies the middle of the room.",
    },
    "draw_opening": {
        "kind": "'door' or 'window' — a misspelt kind is refused by name.",
        "at": "Door: the hinge-side jamb on the wall centreline, [x, y] in mm. "
        "Window: the opening's starting jamb, [x, y] in mm.",
        "width": "Door leaf / window opening width in mm; omit for the standard "
        "default (900 door, 1500 window).",
        "wall_thickness": "Thickness of the wall it sits in, mm.",
        "rotation": "The wall direction in degrees, counter-clockwise from +X.",
        "swing": "Door only: 'left' or 'right' relative to the wall direction. "
        "Refused for a window.",
        "open_angle": "Door only: how open the leaf is drawn, in degrees. Refused for a window.",
        "tag": "Door tag like 'M0921', window tag like 'C1521'; omit for none.",
    },
    "draw_column": {
        "center": "Section centre, [x, y] in mm.",
        "size": "Section width in mm.",
        "depth": "Section depth in mm when the section is not square.",
        "rotation": "Degrees, counter-clockwise from +X.",
        "hatch": "False draws the outline instead of a filled section.",
    },
    "draw_core": {
        "kind": "'stair', 'lift', 'equipment', or 'shaft' (a service riser: "
        "强电井/弱电井/水井 — the captioned rectangle plus a diagonal cross, on "
        "A-SHAFT). A misspelt kind is refused by name.",
        "corner1": "One corner of the bounding rectangle, [x, y] in mm.",
        "corner2": "The opposite corner, [x, y] in mm.",
        "steps": "Stair only: tread count. Refused for the other kinds.",
        "axis": "Stair only: travel direction, 'x' or 'y'. Refused for the other kinds.",
        "up_label": "Stair only: the 上 label; an empty string omits it. "
        "Refused for the other kinds.",
        "down_label": "Stair only: the 下 label; an empty string omits it. "
        "Refused for the other kinds.",
        "tag": "Lift only: the lift tag; omit for none. Refused for the other kinds.",
        "name": "Equipment and shaft only: the caption, shrunk to fit the rectangle. "
        "Refused for stair and lift.",
    },
    "draw_axis_grid": {
        "x_positions": "Vertical grid line coordinates in mm.",
        "y_positions": "Horizontal grid line coordinates in mm.",
        "extent": "[min_x, min_y, max_x, max_y] in mm; the bubbles are drawn just beyond it.",
        "x_labels": "Axis labels for the vertical lines; defaults to 1, 2, 3, ...",
        "y_labels": "Axis labels for the horizontal lines; defaults to A, B, C, ...",
        "bubble_r": "Bubble radius in mm; defaults to 4 mm of paper at the sheet's "
        "plot scale (400 mm at 1:100).",
    },
    "add_text": {
        "pos": "Anchor point, [x, y] in mm.",
        "text": "The string to place.",
        "height": "Model height in mm; defaults to 3.5 mm of paper at the sheet's "
        "plot scale — pass a value only to override that deliberately.",
        "rotation": "Degrees, counter-clockwise from +X.",
        "align": "'left', 'center', 'right' or 'top-center'.",
        "layer": "Target layer.",
    },
    "add_leader_note": {
        "anchor": "Where the leader points, [x, y] in mm — anchor on the boundary of "
        "what is being noted; an anchor inside a solid hatch disappears under the fill.",
        "text": "The note at the leader's far end.",
        "direction": "Leader angle in degrees, counter-clockwise from +X.",
        "length": "Leader length in mm.",
        "height": "Text height in mm; defaults to 3.5 mm of paper at the sheet's scale.",
    },
    "add_dimension": {
        "points": "Two points for a single dimension, three or more for a chain along "
        "the run plus the overall; each [x, y] in mm.",
        "offset": "Sideways offset in mm. The dimension line lands on the LEFT of the "
        "travel direction for a positive value, so a south edge dimensioned east-to-west "
        "takes a positive offset to fall outside the building.",
    },
    "add_hatch": {
        "points": "The closed boundary as a list of [x, y] in mm.",
        "pattern": "One of the standard acad.pat names (ANSI31...ANSI38, AR-*, NET, ...); "
        "a misspelt name raises rather than drawing a pattern that does not exist.",
        "scale": "Multiplies the pattern's own base spacing, which differs per pattern: "
        "ANSI31's base is about 5 mm, so 40 gives wall poché.",
        "angle": "Added to the pattern's own angle, in degrees — ANSI31 is already 45°, "
        "so angle=45 makes it vertical.",
        "solid": "True for a solid fill.",
        "layer": "Target layer.",
    },
    "plugin_status": {
        "with_steps": "False only pings (answers even while AutoCAD is busy); true "
        "includes the journal rows.",
    },
    "plugin_install": {
        "force": "True closes a running AutoCAD first — a loaded assembly cannot be "
        "replaced while AutoCAD holds it.",
    },
    "step_begin": {
        "op": "The operation name the step records.",
        "label": "Human decoration on the step.",
        "params": "The arguments that produced it, recorded for read-back and replay.",
    },
    "step_commit": {
        "planned": "True only records the step, to be drawn later by run_next; false "
        "(default) draws it now and journals it as applied.",
    },
    "step_control": {
        "action": "run_next | run_all | run_to | rollback_to | reject | accept | "
        "unaccept | note | update | snapshot | clear_plan | reset | reload.",
        "index": "The step index the action aims at.",
        "force": "True overrides a rollback refused because an accepted step stands "
        "in the way — the result says so.",
        "note": "Annotation text, for action='note'.",
        "patch": "Params, label or note changes, for action='update'.",
    },
    "library_list": {
        "source": "A library .dwg path, opened read-only on the side; omit for the "
        "block definitions in the open drawing.",
        "limit": "Maximum blocks to return in this page.",
        "offset": "Where the page starts — a previous reply's next_offset.",
    },
    "element_types": {
        "category": "Only list one category.",
        "check_library": "False skips re-checking the catalogue's block names against "
        "the library — faster, but 'could not check' is not the same as fine.",
    },
    "place_element": {
        "type": "The catalogue element key, from element_types.",
        "at": "Single placement: the element's OWN reference point, [x, y] in mm — "
        "a door's hinge (the jamb end of the opening, not its centre), a window's "
        "centre, a column's centre, a lift's corner. Give exactly one of at, "
        "start+end (a run), or xs+ys (a grid).",
        "start": "Run placement: run start, [x, y] in mm.",
        "end": "Run placement: run end, [x, y] in mm.",
        "count": "Run placement: that many elements, evenly spread, ends included. "
        "Give count or spacing, not both.",
        "spacing": "Run placement: one every this many mm, starting at start. "
        "Give count or spacing, not both.",
        "inset": "Run placement: pull the ends in by this many mm — what keeps a "
        "column off a corner.",
        "xs": "Grid placement: axis x coordinates in mm — the same positions the "
        "axis grid was drawn to.",
        "ys": "Grid placement: axis y coordinates in mm; an element is placed at "
        "every intersection.",
        "variant": "A size name from the catalogue ('900' for a 900 mm door).",
        "params": "Per-property overrides on top of the variant.",
        "rotation": "Degrees, counter-clockwise from +X. On a run it defaults to "
        "the run's direction, so a row of doors lies in the wall.",
        "scale": "Uniform scale factor; single placement only.",
        "tag": "False places without a tag; the default numbers from the tags already "
        "in the drawing.",
        "tag_offset": "Raise the tag along +Y in mm; single placement only.",
    },
    "element_capture": {
        "steps": "The journal step indices whose entities become the element — "
        "drawing_history lists them. Draw once, check, then capture.",
        "key": "The element's catalogue key; place_element(type=key) places it from then on.",
        "label": "Human-readable name for the catalogue entry.",
        "tag_prefix": "Joins the auto-numbering — a door's 'M'.",
        "layer": "Layer the captured entities sit on when placed.",
        "category": "Catalogue category for the entry.",
        "base_point": "The element's own insertion origin, [x, y] in mm; omit for the "
        "bounding box's min corner.",
    },
    "element_publish": {
        "key": "The captured element key to publish into the project library.",
        "path": "Catalogue file to append to; empty resolves the project's own, "
        "creating ./elements.json when there is none. Appended to, never overwritten.",
    },
    "catalog_suggest": {
        "min_count": "A step shape must repeat at least this many times to be "
        "suggested; at least 2 — a single step is not a repeat.",
    },
}


def apply(tools) -> list[str]:
    """Write the descriptions into each tool's served schema; return the drift.

    `tools` is the registry's own listing (each carrying ``name`` and the
    ``parameters`` dict the SDK serves as inputSchema). The reply lists every
    problem found, as lines a person can act on: an entry naming a tool or
    parameter that does not exist (the table outlived a rename), and a served
    parameter no entry describes (a tool grew an argument). Empty means the
    table and the surface agree.
    """
    issues: list[str] = []
    by_name = {t.name: t for t in tools}
    for tool_name, table in DESCRIPTIONS.items():
        tool = by_name.get(tool_name)
        if tool is None:
            issues.append(f"params table names unknown tool {tool_name!r}")
            continue
        props = tool.parameters.get("properties") or {}
        for param, text in table.items():
            if param not in props:
                issues.append(f"{tool_name}: table names unknown parameter {param!r}")
                continue
            props[param]["description"] = text
    for tool in tools:
        for param, prop in (tool.parameters.get("properties") or {}).items():
            if not prop.get("description"):
                issues.append(f"{tool.name}: parameter {param!r} has no description")
    return issues
