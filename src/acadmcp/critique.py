"""Checking a drawing against its own journal, component by component.

An AI client's recurring failures are component failures — a door drawn beside its opening,
an opening measured past the end of its wall, a tag that is not the catalogue's — and they
repeat because nothing ever checks them. This module does: it reads the journal's own account
of what was drawn and reports what cannot be true about the drawing.

**Pure.** It imports ``geom``, ``layers`` and ``sheet`` from this package and nothing else:
no CAD library, no backend, and never the ``catalog`` module — the tag prefixes arrive as a
plain ``{kind: prefix}`` dict, the same way ``summary.py`` takes its catalogue as a plain dict.
``sheet`` is here rather than the maths being copied out of it because the sheet check must
measure a viewport with the very helpers ``drawing_sheet`` draws it with, and because ``sheet``
is itself CAD-free and imports only ``layers``.

**It reads the journal, never XDATA.** A stamp-based critique answers "0 issues" on a drawing
it did not stamp, which is a false negative of the worst kind — a clean verdict for a drawing
nobody checked. The journal is present on every drawing this server made, on every backend,
and it records the op and the arguments that produced each step, so a check can reason about
intent as well as geometry.

Handling rules, the same three the summary, the catalogue loader and the journal reader keep:

* **A row that cannot be read is counted, never raised on** — no readable ``params``, or not
  a mapping at all. ``run`` reports the count in ``unreadable``.
* **A step the drawing does not contain is invisible.** ``applied``/``done`` are in the
  drawing; ``undone``, ``rejected``, ``planned`` (the plugin's promise not yet run) and
  ``failed`` are not. A *state this module has never heard of* is read as applied, because a
  future journal that renames "applied" must not silently make every check go quiet.
* **A check that cannot be sure says nothing.** A missing hinge or an unreadable opening is
  skipped, not guessed at: for a verdict a caller gates a drawing on, a false one is worse
  than a missing one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import geom, layers, sheet

#: The severities an :class:`Issue` can carry. `error` gates a drawing; a `warning` is a
#: finding a person should look at; `info` records that a check could not be run.
SEVERITIES = ("error", "warning", "info")

#: Step states that mean the drawing does not contain the step, so every check skips them.
#: Two vocabularies arrive here: the Python journal writes "applied"/"undone", and the plugin's
#: in-DWG journal writes "done"/"planned"/"failed". "rejected" is the plugin palette's own word
#: for a step dropped from the plan, and is tolerated for the same reason.
INACTIVE_STATES = frozenset({"undone", "rejected", "planned", "failed"})

#: How far a door's hinge may be from a wall centreline and still be read as on it. Millimetres,
#: and tight on purpose: a door 2 mm off its wall is a door that was placed by hand.
DOOR_TOLERANCE = 1.0

#: Every op name the checks below read. The test suite pins this against the op names
#: ``server.py`` actually records, so a check keyed on an op no tool produces — the failure
#: ``summary.py``'s ``HANDLERS`` once had, reporting 0 axis grids for every drawing — fails
#: there instead of quietly finding nothing here.
READ_OPS = frozenset(
    {
        "draw_wall",
        "draw_wall_with_openings",
        "draw_room",
        "draw_door",
        "draw_window",
        "add_text",
        "drawing_sheet",
    }
)

#: The ops whose params record a wall centreline a door can stand on.
WALL_OPS = ("draw_wall", "draw_wall_with_openings")


@dataclass(frozen=True)
class Issue:
    """One finding: what is wrong, how badly, and which step recorded it.

    ``step`` is the step the finding is about, and ``detail`` is whatever a caller needs to
    act on it (the tag, the measured distance, the scale that would fit) as plain JSON types.
    """

    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    step: int | None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "step": self.step,
            "detail": dict(self.detail),
        }


# ---------------------------------------------------------------------------
# reading rows, tolerantly
# ---------------------------------------------------------------------------


def _float(value: Any, default: float | None = None) -> float | None:
    """A number, or the default. A missing value and a malformed one read the same."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_point(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and _float(value[0]) is not None
        and _float(value[1]) is not None
    )


def _points(value: Any) -> list[list[float]]:
    """Point-likes normalised to plain ``[x, y]`` pairs; what cannot be read is left out.

    A bare ``[x, y]`` reads as one point, which is the shape the tools record for a hinge or
    a position; a list of them reads as a polyline. Lists rather than tuples because these
    rows have been through JSON (the journal is a JSON payload inside the DXF), and a shape
    that changes across a save and reopen is a shape a caller cannot rely on.
    """
    if isinstance(value, dict):
        x, y = _float(value.get("x")), _float(value.get("y"))
        return [[x, y]] if x is not None and y is not None else []
    if not isinstance(value, (list, tuple)):
        return []
    if _is_point(value):
        return [[float(value[0]), float(value[1])]]
    out: list[list[float]] = []
    for point in value:
        if isinstance(point, dict):
            x, y = _float(point.get("x")), _float(point.get("y"))
        elif _is_point(point):
            x, y = _float(point[0]), _float(point[1])
        else:
            continue
        if x is not None and y is not None:
            out.append([x, y])
    return out


def _floats(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return []
    return [number for number in (_float(item) for item in value) if number is not None]


def _index_of(row: dict[str, Any], position: int) -> int:
    """The step's own number, falling back to where it sits in the list."""
    index = row.get("index", position)
    try:
        return int(index)
    except (TypeError, ValueError):
        return position


def _read_rows(rows: Sequence[dict] | None) -> tuple[list[dict[str, Any]], int]:
    """The rows the drawing contains, and how many could not be read.

    Applied rows are handed on as ``{"index", "op", "params"}`` dicts — the same shape the
    journal reports, so a check is a plain function over plain data.
    """
    applied: list[dict[str, Any]] = []
    unreadable = 0
    for position, raw in enumerate(rows or [], start=1):
        if not isinstance(raw, dict):
            unreadable += 1
            continue
        if str(raw.get("state", "")).strip().lower() in INACTIVE_STATES:
            continue
        params = raw.get("params")
        if not isinstance(params, dict):
            unreadable += 1
            continue
        op = raw.get("op")
        applied.append(
            {
                "index": _index_of(raw, position),
                "op": op.strip() if isinstance(op, str) else "",
                "params": params,
            }
        )
    return applied, unreadable


def _polyline(row: dict[str, Any], key: str, closed: bool) -> list[list[float]]:
    """A run's centreline, with a closing leg when the row says the run is a ring.

    The closing leg is added here for the same reason ``ops`` adds it when it measures the
    run: a ring recorded as six corners is a wall of six legs, and an opening at the end of
    the last one is still on the wall.
    """
    points = _points(row["params"].get(key))
    if len(points) < 2:
        return []
    if closed and not geom.is_closed(points):
        points = [*points, points[0]]
    return points


def _along(p: Sequence[float], points: Sequence[Sequence[float]]) -> tuple[float, float]:
    """(distance from `p` to the polyline, distance along it to the nearest point)."""
    best_distance = math.inf
    best_along = 0.0
    travelled = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        segment = geom.dist(a, b)
        distance = geom.dist_point_segment(p, a, b)
        if distance < best_distance:
            ax, ay = geom.xy(a)
            bx, by = geom.xy(b)
            px, py = geom.xy(p)
            dx, dy = bx - ax, by - ay
            t = 0.0
            if segment > 0:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (segment * segment)))
            best_distance, best_along = distance, travelled + t * segment
        travelled += segment
    return best_distance, best_along


# ---------------------------------------------------------------------------
# 1. a door and the opening it belongs in
# ---------------------------------------------------------------------------


def _wall_lines(rows: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], list[list[float]]]]:
    """Every recorded wall centreline a door could stand on, with the step that drew it."""
    out: list[tuple[dict[str, Any], list[list[float]]]] = []
    for row in rows:
        if row["op"] in WALL_OPS:
            line = _polyline(row, "points", bool(row["params"].get("closed")))
        elif row["op"] == "draw_room" and row["params"].get("walls", True):
            line = _polyline(row, "boundary", True)
        else:
            continue
        if len(line) >= 2:
            out.append((row, line))
    return out


def _wall_openings(row: dict[str, Any]) -> list[tuple[float, float]]:
    """The ``(start, width)`` openings a wall row cut, in millimetres along the run."""
    if row["op"] != "draw_wall_with_openings":
        return []
    raw = row["params"].get("openings")
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[float, float]] = []
    for opening in raw:
        if not isinstance(opening, dict):
            continue
        start, width = _float(opening.get("start")), _float(opening.get("width"))
        if start is not None and width is not None:
            out.append((start, width))
    return out


def _overlaps(along: float, door: float, start: float, opening: float) -> bool:
    """Whether a door hinged at ``along`` lies in an opening at ``start``.

    The hinge sits at the opening's jamb, so what has to fall inside the opening is the door's
    own span: two intervals overlap when their midpoints are within half their total length of
    each other. The ``<=`` keeps a door hinged on the far jamb (or drawn from the wrong end)
    counted as in its opening, which is a modelling choice this package does not police.
    """
    return abs(along + door / 2.0 - (start + opening / 2.0)) <= (door + opening) / 2.0


def _check_door_without_opening(
    rows: Sequence[dict[str, Any]], tag_prefixes: dict[str, str]
) -> list[Issue]:
    """A door is drawn on a wall, into an opening that wall run cut.

    Two failures, told apart by severity. A hinge that is on no recorded wall is an **error**:
    the door is standing in space, whatever the drawing looks like. A hinge on a wall that
    records no opening there is a **warning** — the geometry is plausible and the opening was
    never cut, which is exactly the misdrawn door that survived a session.
    """
    walls = _wall_lines(rows)
    issues: list[Issue] = []
    for row in rows:
        if row["op"] != "draw_door":
            continue
        params = row["params"]
        hinge = next(iter(_points(params.get("hinge"))), None)
        if hinge is None:
            continue
        width = _float(params.get("width"), layers.DOOR_WIDTH) or layers.DOOR_WIDTH
        nearest: tuple[float, float, dict[str, Any]] | None = None
        for wall, line in walls:
            distance, along = _along(hinge, line)
            if nearest is None or distance < nearest[0]:
                nearest = (distance, along, wall)
        detail = {"hinge": hinge, "width": width}
        if nearest is None or nearest[0] > DOOR_TOLERANCE:
            detail["distance_mm"] = None if nearest is None else round(nearest[0], 3)
            issues.append(
                Issue(
                    "door_without_opening",
                    "error",
                    f"step {row['index']} draws a door whose hinge {hinge} is on no recorded "
                    "wall centreline: draw the wall first (draw_wall or draw_wall_with_openings)",
                    row["index"],
                    detail,
                )
            )
            continue
        distance, along, wall = nearest
        if not any(
            _overlaps(along, width, start, opening) for start, opening in _wall_openings(wall)
        ):
            detail.update({"wall_step": wall["index"], "along_mm": round(along, 3)})
            issues.append(
                Issue(
                    "door_without_opening",
                    "warning",
                    f"step {row['index']} draws a door on an uncut wall: step {wall['index']} "
                    f"records no opening at {along:.0f} mm along the run, so the opening belongs "
                    "in draw_wall_with_openings, which cuts the wall and draws the symbol in "
                    "one step",
                    row["index"],
                    detail,
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 2. an opening measured along its run
# ---------------------------------------------------------------------------


def _check_opening_fits_run(
    rows: Sequence[dict[str, Any]], tag_prefixes: dict[str, str]
) -> list[Issue]:
    """An opening is measured along its run, from its start, and has to land on it.

    An opening past the end of the wall — or starting before it — is an error rather than a
    warning because ``draw_wall_with_openings`` clamps an opening it cannot cut, so a recorded
    one that does not fit says the opening the caller asked for is not the one in the drawing.
    """
    issues: list[Issue] = []
    for row in rows:
        if row["op"] != "draw_wall_with_openings":
            continue
        line = _polyline(row, "points", bool(row["params"].get("closed")))
        if len(line) < 2:
            continue
        length = geom.polyline_length(line)
        for opening in row["params"].get("openings") or []:
            if not isinstance(opening, dict):
                continue
            start, width = _float(opening.get("start")), _float(opening.get("width"))
            if start is None or width is None:
                continue
            tag = opening.get("tag")
            named = f"tagged {tag!r} " if isinstance(tag, str) and tag.strip() else ""
            end = start + width
            # A hair of tolerance: a run is measured through the same floating-point distance
            # the opening's end is, so an opening ending exactly at the end must not be an error.
            if start < 0 or end > length + 1e-6:
                issues.append(
                    Issue(
                        "opening_fits_run",
                        "error",
                        f"step {row['index']}: {named}opening starts at {start:g} mm and is "
                        f"{width:g} mm wide, so it ends at {end:g} mm — outside the "
                        f"{length:g} mm run",
                        row["index"],
                        {
                            "tag": tag if isinstance(tag, str) else None,
                            "start": start,
                            "width": width,
                            "run_length": round(length, 3),
                        },
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# 3 and 4. tags
# ---------------------------------------------------------------------------

#: What a tag looks like when the catalogue has no entry for the kind. The catalogue's own
#: prefix is preferred; this is the shape every catalogue in this package follows anyway.
_TAG_FALLBACK = re.compile(r"^[A-Z]+\d{4}$")


def _explicit_tags(rows: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], str, str]]:
    """``(row, kind, tag)`` for every tag a row wrote by hand — not one this package invented."""
    out: list[tuple[dict[str, Any], str, str]] = []
    for row in rows:
        params = row["params"]
        if row["op"] in ("draw_door", "draw_window"):
            tag = params.get("tag")
            if isinstance(tag, str) and tag.strip():
                kind = "door" if row["op"] == "draw_door" else "window"
                out.append((row, kind, tag.strip()))
        elif row["op"] == "draw_wall_with_openings":
            for opening in params.get("openings") or []:
                if not isinstance(opening, dict):
                    continue
                tag = opening.get("tag")
                if isinstance(tag, str) and tag.strip():
                    kind = str(opening.get("kind") or "door").strip().lower()
                    out.append((row, kind, tag.strip()))
    return out


def _tag_pattern(kind: str, tag_prefixes: dict[str, str]) -> re.Pattern[str]:
    prefix = str((tag_prefixes or {}).get(kind, "") or "").strip()
    return re.compile(rf"^{re.escape(prefix)}\d{{4}}$") if prefix else _TAG_FALLBACK


def _check_tag_format(rows: Sequence[dict[str, Any]], tag_prefixes: dict[str, str]) -> list[Issue]:
    """A tag is the catalogue's prefix for its kind plus exactly four digits.

    ``place_element`` derives its tags from the drawing and never stores a counter precisely
    so they cannot drift; a tag a caller typed has nothing behind it but this check.
    """
    issues: list[Issue] = []
    for row, kind, tag in _explicit_tags(rows):
        pattern = _tag_pattern(kind, tag_prefixes)
        if pattern.match(tag):
            continue
        source = (
            f"the catalogue's {tag_prefixes[kind]!r} for a {kind}"
            if tag_prefixes.get(kind)
            else f"a {kind} tag"
        )
        issues.append(
            Issue(
                "tag_format",
                "warning",
                f"step {row['index']}: tag {tag!r} is not {source} followed by four digits "
                f"(expected {pattern.pattern!r})",
                row["index"],
                {"tag": tag, "kind": kind, "pattern": pattern.pattern},
            )
        )
    return issues


def _and_list(values: Sequence[int]) -> str:
    """``[1, 5]`` as "1 and 5", so a message reads as a sentence."""
    parts = [str(value) for value in values]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _check_tag_duplicates(
    rows: Sequence[dict[str, Any]], tag_prefixes: dict[str, str]
) -> list[Issue]:
    """One tag names one component, so the same tag on two applied rows is a finding."""
    by_tag: dict[str, list[int]] = {}
    for row, _kind, tag in _explicit_tags(rows):
        steps = by_tag.setdefault(tag, [])
        if row["index"] not in steps:
            steps.append(row["index"])
    return [
        Issue(
            "tag_duplicates",
            "warning",
            f"tag {tag!r} is recorded on steps {_and_list(steps)}: one tag names one "
            "component, so one of these is a duplicate",
            steps[0],
            {"tag": tag, "steps": steps},
        )
        for tag, steps in by_tag.items()
        if len(steps) > 1
    ]


# ---------------------------------------------------------------------------
# 5. text that is pretending to be a dimension
# ---------------------------------------------------------------------------

#: The shapes a measured thing has — diameter, radius, tolerance, a two-part size, a
#: millimetre value — written as text rather than dimensioned.
_DIMENSION_LIKE = re.compile(
    r"(Ø|⌀|∅|%%c|%%C|R\d|±|%%p|\d+\s*[×x]\s*\d+|\d+\.\d+\s*mm)", re.IGNORECASE
)

#: A standard-part designation names a part rather than measuring anything, so ``ISO 4014 -
#: M12x60`` is a note about a bolt, not a dimension. Idea borrowed from the rival's validator,
#: with GB added, because that is the standard this package's drawings are annotated to.
_PART_DESIGNATION = re.compile(
    r"\b(?:ISO|DIN|GB)\s*\d+(?:-\d+)?\s*-?\s*M\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?",
    re.IGNORECASE,
)


def _check_dimension_like_text(
    rows: Sequence[dict[str, Any]], tag_prefixes: dict[str, str]
) -> list[Issue]:
    """Text that measures something belongs in ``add_dimension``, not in ``add_text``."""
    issues: list[Issue] = []
    for row in rows:
        if row["op"] != "add_text":
            continue
        text = row["params"].get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stripped = _PART_DESIGNATION.sub(" ", text)
        match = _DIMENSION_LIKE.search(stripped)
        if match is None:
            continue
        issues.append(
            Issue(
                "dimension_like_text",
                "warning",
                f"step {row['index']}: text {text!r} masquerades as a dimension ({match.group(1)!r}); "
                "use add_dimension, which measures the drawing rather than restating it",
                row["index"],
                {"text": text, "matched": match.group(1)},
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 6. the plan against the sheet's own viewport
# ---------------------------------------------------------------------------

#: The keys a row records its model geometry under. `points` covers a line, a polyline and a
#: wall run; `boundary` a room; `at`/`start`/`end` a placement; `center` a circle or a column;
#: `corner1`/`corner2` a rectangle — a rect, a stair, a lift, a piece of equipment.
_EXTENT_KEYS = (
    "points",
    "boundary",
    "p1",
    "p2",
    "corner1",
    "corner2",
    "center",
    "at",
    "hinge",
    "pos",
    "start",
    "end",
)


def _plan_extent(rows: Sequence[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    """``(min_x, min_y, max_x, max_y)`` of everything the applied rows drew, or None.

    The sheet's own rows are skipped: a frame and a title block are paper, not plan, and
    measuring them would make every sheet look like a plan the size of its page.
    """
    points: list[list[float]] = []
    for row in rows:
        if row["op"] == "drawing_sheet":
            continue
        params = row["params"]
        for key in _EXTENT_KEYS:
            if key in params:
                points.extend(_points(params.get(key)))
        if row["op"] == "draw_axis_grid":
            extent = _floats(params.get("extent"))
            if len(extent) == 4:
                points.extend([[extent[0], extent[1]], [extent[2], extent[3]]])
        xs, ys = _floats(params.get("xs")), _floats(params.get("ys"))
        points.extend([[x, y] for y in ys for x in xs])
    return geom.bbox(points) if points else None


def _check_sheet_viewport_fit(
    rows: Sequence[dict[str, Any]], tag_prefixes: dict[str, str]
) -> list[Issue]:
    """Whether the plan is wholly inside the sheet's viewport, measured with ``sheet``'s own
    helpers so this check cannot drift from what ``drawing_sheet`` drew.

    A plan larger than the viewport is a **warning**: the sheet and the scale are both correct
    and both useless together, and a detail sheet is drawn that way on purpose. The scale that
    would hold the whole plan is named, because working it out is the one thing a caller cannot
    do from the sheet's own reply.
    """
    sheet_row = next((row for row in reversed(rows) if row["op"] == "drawing_sheet"), None)
    if sheet_row is None:
        return []
    params = sheet_row["params"]
    try:
        paper = sheet.resolve_paper(
            str(params.get("paper") or "A3"), str(params.get("orientation") or "landscape")
        )
        scale = sheet.resolve_scale(str(params.get("scale") or "1:100"))
    except ValueError as exc:
        return [
            Issue(
                "sheet_viewport_fit",
                "info",
                f"step {sheet_row['index']}: the sheet's paper and scale could not be read "
                f"({exc}), so the plan was not measured against it",
                sheet_row["index"],
                {},
            )
        ]
    extent = _plan_extent(rows)
    if extent is None or sheet.fits_on_sheet(paper, scale, extent):
        return []
    window = sheet.model_window(paper, scale)
    largest = sheet.largest_scale_that_fits(paper, extent)
    ratio = f"{scale[0]}:{scale[1]}"
    return [
        Issue(
            "sheet_viewport_fit",
            "warning",
            f"the plan is {extent[2] - extent[0]:.0f} x {extent[3] - extent[1]:.0f} mm and "
            f"{paper.name} at {ratio} shows {window[0]:.0f} x {window[1]:.0f} mm, so the sheet "
            f"holds a detail rather than all of it — the largest scale that holds it whole is "
            f"{largest}",
            sheet_row["index"],
            {
                "paper": paper.name,
                "scale": ratio,
                "plan_mm": [
                    round(extent[2] - extent[0], 3),
                    round(extent[3] - extent[1], 3),
                ],
                "viewport_model_mm": [round(window[0], 3), round(window[1], 3)],
                "largest_scale_that_fits": largest,
            },
        )
    ]


# ---------------------------------------------------------------------------
# the registry and the entry point
# ---------------------------------------------------------------------------

#: Check code → check function. **The single table**: what a caller can focus on is what is
#: here, and a check is a plain function over the applied rows and the tag prefixes.
CHECKS: dict[str, Callable[[Sequence[dict[str, Any]], dict[str, str]], list[Issue]]] = {
    "door_without_opening": _check_door_without_opening,
    "opening_fits_run": _check_opening_fits_run,
    "tag_format": _check_tag_format,
    "tag_duplicates": _check_tag_duplicates,
    "dimension_like_text": _check_dimension_like_text,
    "sheet_viewport_fit": _check_sheet_viewport_fit,
}


def run(
    rows: Sequence[dict],
    tag_prefixes: dict[str, str] | None = None,
    focus: str = "",
) -> dict[str, Any]:
    """Every check, or the one named by `focus`, over the journal's own rows.

    `rows` is the journal's steps in the shape both owners report them — ``index``, ``op``,
    ``label``, ``params``, ``state``. `tag_prefixes` maps an opening kind to the catalogue's
    tag prefix (``{"door": "M"}``); a kind with no entry falls back to letters plus four
    digits. `focus` runs one check by its code, which is how a caller asks a single question
    without the rest of the report — an unknown focus is refused by name rather than ignored.

    Returns ``{"issues": [...], "counts": {"error", "warning", "info"}, "rows_checked",
    "unreadable", "focus"}``.
    """
    if focus and focus not in CHECKS:
        raise ValueError(f"unknown focus {focus!r}; the checks are {', '.join(sorted(CHECKS))}")
    applied, unreadable = _read_rows(rows)
    prefixes = {str(k): str(v) for k, v in (tag_prefixes or {}).items()}
    issues: list[Issue] = []
    for code in [focus] if focus else list(CHECKS):
        issues.extend(CHECKS[code](applied, prefixes))
    counts = dict.fromkeys(SEVERITIES, 0)
    for issue in issues:
        if issue.severity in counts:
            counts[issue.severity] += 1
    return {
        "issues": [issue.to_dict() for issue in issues],
        "counts": counts,
        "rows_checked": len(applied),
        "unreadable": unreadable,
        "focus": focus,
    }
