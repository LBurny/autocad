"""What the drawing contains, read back as data instead of looked at.

An AI client had two ways to check its own work: a screenshot, or counting entities on a
layer. Neither answers "how many doors, and what sizes". This module answers it from the
journal — the steps already record the op and the arguments that produced it — so the
answer needs no vision, works on every backend, and survives a reopen because the journal
persists inside the DXF.

Everything here is *derived, never remembered*. A room's area is recomputed from its
recorded boundary with the same ``geom.inner_boundary`` call ``draw_room`` makes, so the
summary cannot drift from the geometry: if this ever disagrees with ``draw_room``'s own
reply, **this module is wrong, not the drawing**.

CAD-free by construction: it may import ``geom`` and ``layers`` and nothing else. Never
ezdxf, never a backend, and the ``catalog`` argument is a plain dict passed in rather than
the catalogue module.

Handling rules, which are decisions rather than incidentals:

* **A row that cannot be read is counted and warned about, never raised on and never
  silently dropped** — no ``op``, no readable ``params``, or not a mapping at all. This is
  the rule the catalogue loader and the journal reader already follow, and these rows have
  survived a reload, a replay and possibly a hand edit.
* An **undone** step is counted in ``steps`` and warned about, and contributes nothing to
  ``rooms``/``openings``/``elements``, because the drawing does not contain it. The one
  exception is ``layers``: an undone row still *records* layers, and ``layers`` is defined
  as what the rows record.
* ``layers`` is every ``layer``/``*_layer`` value recorded in any step's params, sorted. The
  ops that take no layer parameter record none — a room and a wall run draw on the standard's
  own layers without naming them — so a plan made only of those reports no layers. That is
  the rows' limit, not a claim that the drawing has none.
* ``openings`` is every opening a step cut or drew: the openings of a wall run, and a door or
  window drawn on its own. A door drawn directly is still a door of a size, and "how many
  doors, and what sizes" is the question this module exists to answer.
* ``place_element``/``place_along``/``place_grid`` fill ``elements``; the repeating two also
  add their count to ``structure``. A run's individual placements are *not* reported under
  ``at`` — the row does not record the run's ``inset``, so where each one landed is not
  derivable, and ``at`` reports the run's own ends instead.
* ``structure`` counts ``draw_stair``/``draw_lift``/``draw_shaft``/``draw_equipment`` (whose
  ``kind="shaft"`` makes it a shaft, as it draws one) and placed catalogue elements whose key
  names one of those, confirmed against the catalogue's ``category`` when one was handed in.
* ``annotation`` counts ``add_dimension``/``add_text``/``add_leader_note``/``draw_axis_grid``/
  ``add_hatch``: **one per step**, not one per entity drawn, because a step is the unit the
  journal records. A dimension chain is one annotation in this count.
* ``catalog`` echoes the identity the caller handed in — which entries, which library root,
  a digest to compare against — or null when nobody said, since guessing which catalogue
  drew a drawing is exactly the silent wrong answer this package refuses elsewhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import geom, layers

#: The step states that mean the step is not in the drawing. Two vocabularies arrive: the
#: Python journal writes "undone" and the plugin's in-DWG journal writes "planned"/"failed"
#: (RpcServer.Row lowercases StepState's name). Reading only "undone" counted a live plugin's
#: planned steps as drawn. Anything else is read as applied, so that
#: `applied + undone == total` always holds whatever a hand-edited row says.
#: `tests/test_plugin_row_contract.py` pins this against the other three tables.
UNDONE_STATES = frozenset({"undone", "planned", "failed", "rejected"})

#: The `structure` buckets, in the order the summary reports them.
STRUCTURE_KEYS = ("columns", "stairs", "lifts", "shafts", "equipment")

#: The `annotation` counters, likewise.
ANNOTATION_KEYS = ("dimensions", "texts", "leaders", "axis_grids", "hatches")

#: Which bucket a catalogue element lands in, by its key. A project's own keys do not have
#: to be here: an element this cannot place is still listed in `elements` with its count.
_STRUCTURE_OF = {
    "column": "columns",
    "stair": "stairs",
    "lift": "lifts",
    "shaft": "shafts",
    "equipment": "equipment",
}


class _Unreadable(Exception):
    """A row whose recorded parameters cannot be summarised. Collected as a warning."""


@dataclass(frozen=True)
class _Step:
    """One journal row, read as far as it can be read."""

    index: int
    op: str
    label: str
    params: dict[str, Any]


# ---------------------------------------------------------------------------
# reading row values, tolerantly
# ---------------------------------------------------------------------------


def _float(value: Any, default: float | None = None) -> float | None:
    """A number, or the default. A missing value and a malformed one read the same."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _floats(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[float] = []
    for item in value:
        number = _float(item)
        if number is not None:
            out.append(number)
    return out


def _points(value: Any) -> list[list[float]]:
    """Point-likes normalised to plain ``[x, y]`` pairs; what cannot be read is left out.

    Lists rather than tuples because these rows have usually been through JSON on their way
    here — the journal is a JSON payload inside the DXF — and a shape that changes across a
    save and reopen is a shape a client cannot rely on.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[list[float]] = []
    for point in value:
        if isinstance(point, dict):
            x, y = _float(point.get("x")), _float(point.get("y"))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x, y = _float(point[0]), _float(point[1])
        else:
            continue
        if x is not None and y is not None:
            out.append([x, y])
    return out


def _recorded_layers(params: Any) -> list[str]:
    """The layer names a step's params record — `layer`, and the `*_layer` variants.

    Both, because a tool that draws on a second layer says so under its own key
    (`bound_layer`, `name_layer`), and a caller asking which layers a plan uses means those
    too.
    """
    if not isinstance(params, dict):
        return []
    out: list[str] = []
    for key, value in params.items():
        if (key == "layer" or key.endswith("_layer")) and isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


# ---------------------------------------------------------------------------
# one function per op family
# ---------------------------------------------------------------------------


def _opening(
    step: _Step, kind: str, opening: dict[str, Any], *, swing_default: str | None = None
) -> dict[str, Any]:
    """One opening, in one shape, whoever drew it.

    `swing_default` is what the op draws when the opening does not say — "left" for a door,
    because that is the door `draw_wall_with_openings` and `draw_door` themselves draw — and
    None for a window, which has no swing at all and must not be given one.
    """
    swing = opening.get("swing")
    return {
        "step": step.index,
        "kind": kind,
        "tag": opening.get("tag"),
        "width": _float(opening.get("width")),
        "swing": swing_default if swing is None else str(swing),
        "element": opening.get("element"),
    }


def _draw_room(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    """A room, measured the way the room tool measures it.

    `thickness` defaults to the op's own default when the row never recorded one: the tool
    always records it, so a row without it is damaged rather than a 0-thick room, and the
    default is the number the op would have drawn with.
    """
    boundary = _points(step.params.get("boundary"))
    if len(boundary) < 3:
        raise _Unreadable("no room boundary to measure, so the area is unknown")
    thickness = float(_float(step.params.get("thickness"), layers.WALL_THICKNESS) or 0.0)
    inner = geom.inner_boundary(boundary, thickness)
    out["rooms"].append(
        {
            "step": step.index,
            "label": step.label,
            "name": str(step.params.get("name") or ""),
            "area_m2": round(geom.area(inner) / 1_000_000.0, 3),
            "boundary": boundary,
            "thickness": thickness,
        }
    )


def _draw_wall_with_openings(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    """The openings a wall run cut. A run with none to record lists none."""
    openings = step.params.get("openings")
    if not isinstance(openings, (list, tuple)):
        # This op always records its openings, so a row without a readable list is damaged
        # rather than a wall with nothing in it — and the doors it cut are unknown.
        raise _Unreadable("no readable 'openings' list, so what the run cut is unknown")
    for opening in openings:
        if not isinstance(opening, dict):
            continue
        kind = str(opening.get("kind") or "door")
        out["openings"].append(_opening(step, kind, opening, swing_default="left"))


def _draw_door(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    out["openings"].append(_opening(step, "door", step.params, swing_default="left"))


def _draw_window(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    # A window has no swing, and the row does not carry one: null, rather than a "left"
    # the drawing was never asked for.
    out["openings"].append(_opening(step, "window", step.params))


def _structure(out: dict[str, Any], bucket: str, how_many: int = 1) -> None:
    out["structure"][bucket] = out["structure"].get(bucket, 0) + how_many


def _draw_column(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    _structure(out, "columns")


def _draw_stair(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    _structure(out, "stairs")


def _draw_lift(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    _structure(out, "lifts")


def _draw_equipment(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    """Equipment — or a service riser, which is what `kind="shaft"` draws.

    There is no `draw_shaft` tool: `draw_equipment(kind="shaft")` records the op
    `draw_equipment`, so this one handler covers both and a `"draw_shaft"` key here
    would be a line nothing ever reaches.
    """
    _structure(out, "shafts" if step.params.get("kind") == "shaft" else "equipment")


def _add_dimension(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    out["annotation"]["dimensions"] += 1


def _add_text(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    out["annotation"]["texts"] += 1


def _add_leader_note(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    out["annotation"]["leaders"] += 1


def _draw_axis_grid(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    out["annotation"]["axis_grids"] += 1


def _add_hatch(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    out["annotation"]["hatches"] += 1


def _placement_points(step: _Step) -> list[list[float]]:
    """The points a placement call names.

    One for a single placement, the run's two ends for `place_along`, every intersection for
    `place_grid`. A run's *individual* placements are not recoverable — the row does not
    record the run's `inset` — so what is reported is what the row actually holds and
    `count` says how many landed in between.
    """
    params = step.params
    if "at" in params:
        at = params.get("at")
        # `at` is one point — the tool records `geom.xy(at)`, a pair of numbers — but a row
        # that somehow holds a list of points is reported as one rather than dropped.
        if isinstance(at, (list, tuple)) and at and isinstance(at[0], (list, tuple, dict)):
            return _points(at)
        return _points([at])
    if "start" in params or "end" in params:
        return _points([params.get("start"), params.get("end")])
    return [[x, y] for y in _floats(params.get("ys")) for x in _floats(params.get("xs"))]


def _placement_count(step: _Step) -> int:
    """How many elements the call placed, as far as the row can say."""
    params = step.params
    if "xs" in params or "ys" in params:
        return len(_floats(params.get("xs"))) * len(_floats(params.get("ys")))
    if params.get("count") is not None:
        count = _float(params.get("count"), 0.0) or 0.0
        return int(count) if count >= 1 else 0
    spacing = _float(params.get("spacing"))
    if spacing:
        start, end = _points([params.get("start"), params.get("end")])
        if len(start) == 2 and len(end) == 2:
            span = geom.dist(start, end)
            return int(span / spacing + 1e-9) + 1
    return 1 if "at" in params else 0


def _structure_bucket(key: str, by_key: dict[str, str]) -> str:
    """The `structure` bucket a placed element's key belongs in, or "".

    The catalogue is the authority on what an entry *is*, so an entry it reclassifies as
    something other than structural is not counted as a column however its key reads. With
    no catalogue to ask, the key is all there is.
    """
    bucket = _STRUCTURE_OF.get(key)
    if not bucket:
        return ""
    category = by_key.get(key)
    return bucket if category in (None, "", "structure") else ""


def _place_element(out: dict[str, Any], step: _Step, by_key: dict[str, str]) -> None:
    """`place_element`, `place_along` and `place_grid`: an element, and its count.

    `rotation` is null unless the row recorded one. `place_element` does not put its
    rotation in the step's params, so a 0.0 here would be a fact the drawing never promised.
    """
    key = str(step.params.get("type") or "").strip()
    if not key:
        raise _Unreadable("no element 'type' recorded, so nothing was placed by name")
    count = _placement_count(step)
    out["elements"].append(
        {
            "step": step.index,
            "type": key,
            "variant": step.params.get("variant"),
            "at": _placement_points(step),
            "rotation": _float(step.params.get("rotation")),
            "label": step.label,
            "count": count,
        }
    )
    bucket = _structure_bucket(key.strip().lower(), by_key)
    if bucket:
        _structure(out, bucket, count)


#: Which function reads which op. **The single table**: adding an op is adding a line here,
#: and an op with no line still counts toward `steps` and the layers it records — the two
#: things every row contributes to whatever it drew.
HANDLERS = {
    "draw_room": _draw_room,
    "draw_wall_with_openings": _draw_wall_with_openings,
    "draw_door": _draw_door,
    "draw_window": _draw_window,
    "draw_column": _draw_column,
    "draw_stair": _draw_stair,
    "draw_lift": _draw_lift,
    "draw_equipment": _draw_equipment,
    "add_dimension": _add_dimension,
    "add_text": _add_text,
    "add_leader_note": _add_leader_note,
    "draw_axis_grid": _draw_axis_grid,
    "add_hatch": _add_hatch,
    "place_element": _place_element,
    "place_along": _place_element,
    "place_grid": _place_element,
}


# ---------------------------------------------------------------------------
# the catalogue behind the element names
# ---------------------------------------------------------------------------


def _catalog_entries(payload: Any) -> list[Any]:
    """The entries of a catalogue payload, in either shape it arrives in.

    The identity a drawing records lists entries flat under ``elements``; the report
    ``Catalog.describe()`` groups them under ``categories``. Both are accepted so a caller
    does not have to know which it was handed.
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get("elements")
    if isinstance(entries, list):
        return entries
    categories = payload.get("categories")
    if not isinstance(categories, dict):
        return []
    return [item for items in categories.values() if isinstance(items, list) for item in items]


def _by_key(payload: Any) -> dict[str, str]:
    """``{element key: category}``, for telling a structural element from a fixture."""
    out: dict[str, str] = {}
    for entry in _catalog_entries(payload):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip().lower()
        if key:
            out[key] = str(entry.get("category", "")).strip().lower()
    return out


def _catalog_report(payload: Any) -> dict[str, Any] | None:
    """Which catalogue drew the drawing, or null when nobody said.

    Only what a caller acts on: how many entries it had, which library root they resolve
    against, and a digest to compare with the catalogue loaded now. The entries themselves
    travel inside the drawing, and repeating them in every summary would bury the answer.
    """
    if not isinstance(payload, dict) or not payload:
        return None
    return {
        "count": payload.get("count"),
        "digest": payload.get("digest"),
        "library": payload.get("library", ""),
    }


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------


def _index_of(row: dict[str, Any], position: int) -> int:
    """The step's own number, falling back to where it sits in the list."""
    index = row.get("index", position)
    try:
        return int(index)
    except (TypeError, ValueError):
        return position


def _mid_edit_warning(undone: list[int]) -> list[str]:
    """One warning for the undone tail, however long it is.

    Undo and rollback both take from the end, so the undone steps are contiguous and a range
    is the truth about them. One line rather than one per step because a caller reading a
    hundred-step history is being told one thing: what is missing.
    """
    if not undone:
        return []
    if len(undone) == 1:
        return [f"step {undone[0]} is undone: the drawing is mid-edit"]
    return [
        f"steps {undone[0]}-{undone[-1]} are undone ({len(undone)} steps): the drawing is mid-edit"
    ]


def summarise(rows: Sequence[dict], *, catalog: dict | None = None) -> dict[str, Any]:
    """The plan as data: rooms, openings, elements, counts, layers, warnings.

    `rows` is the journal's steps in the shape both owners report them — ``index``, ``op``,
    ``label``, ``params``, ``state`` — which is what ``step_rows()`` returns and what the
    plugin's ``step_list`` produces. `catalog` is the catalogue identity the drawing
    recorded, when there is one; it is a plain dict, never the catalogue module.
    """
    out: dict[str, Any] = {
        "steps": {"total": 0, "applied": 0, "undone": 0, "next_index": 1},
        "rooms": [],
        "openings": [],
        "elements": [],
        "structure": dict.fromkeys(STRUCTURE_KEYS, 0),
        "annotation": dict.fromkeys(ANNOTATION_KEYS, 0),
        "layers": [],
        "catalog": _catalog_report(catalog),
        "warnings": [],
    }
    by_key = _by_key(catalog)
    warnings: list[str] = []
    recorded_layers: set[str] = set()
    undone: list[int] = []
    numbered: list[int] = []

    for position, raw in enumerate(rows or [], start=1):
        out["steps"]["total"] += 1
        if not isinstance(raw, dict):
            warnings.append(f"step {position} is not a step row, so nothing could be read")
            continue
        index = _index_of(raw, position)
        numbered.append(index)
        params = raw.get("params")
        # Layers are collected before anything else, so an op this module has no handler for
        # still says which layers the drawing uses.
        recorded_layers.update(_recorded_layers(params))

        if str(raw.get("state", "")).strip().lower() in UNDONE_STATES:
            out["steps"]["undone"] += 1
            undone.append(index)
            continue
        out["steps"]["applied"] += 1

        op = raw.get("op")
        op = op.strip() if isinstance(op, str) else ""
        if not op:
            warnings.append(f"step {index} recorded no 'op', so it could not be summarised")
            continue
        if not isinstance(params, dict):
            warnings.append(f"step {index} ({op}) recorded no readable parameters")
            continue

        handler = HANDLERS.get(op)
        if handler is None:
            continue
        step = _Step(index=index, op=op, label=str(raw.get("label") or ""), params=params)
        try:
            handler(out, step, by_key)
        except Exception as exc:  # noqa: BLE001 - tolerance is this module's contract
            # A handler that cannot read a row names why in its own words; catching anything
            # else here keeps one damaged row from taking the whole summary down, which is
            # the same trade the catalogue loader and the journal reader make.
            warnings.append(f"step {index} ({op}): {exc}")

    out["steps"]["next_index"] = (max(numbered) if numbered else out["steps"]["total"]) + 1
    out["layers"] = sorted(recorded_layers)
    out["warnings"] = [*_mid_edit_warning(undone), *warnings]
    return out
