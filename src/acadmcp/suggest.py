"""Repetition the journal already holds, and the capture worth making of it.

A plan drawn twice — five doors of one size, four identical window runs — is a library
element waiting to be captured, and the journal already records every one of them. This
module reads the step rows and reports the repeats, so nobody has to notice them by eye. It
*suggests* and nothing more: `element_capture` is explicit and a person's decision (the
plan's D5/D7), and the hint names the call rather than making it.

Read-only and journal-scoped (D7): it mines the rows it is handed — the *current* drawing's
steps — and knows nothing of any other drawing. Cross-drawing accumulation needs a
project-level usage log this wave does not have.

CAD-free by construction, the rule `summary.py` and `critique.py` keep: no ezdxf, no backend,
not even `geom`. The rows arrive as plain dicts and so does the answer.

**What groups with what.** Two steps group when they share an ``op`` *and* their parameters
are equal after canonicalisation: mapping keys sorted, every number — int or float — rounded
to 0.1 mm, lists and tuples kept in order, nested mappings recursed. Positions are part of
the key, so two identical doors at two hinges do not group; what `element_capture` takes is a
step's geometry, and two rows that differ in *where* they are would suggest a capture of a
size and a place the row does not have.

**The bookkeeping keys left out** are named in :data:`IGNORED_PARAM_KEYS`, and each is
excluded because it describes the step rather than what it drew:

* ``label`` is human decoration on the step, not a parameter (:func:`_auto_step`'s own words:
  the history renders ``"{op} {label}"``). Whatever it decorates is already in the params
  under its own name — a room's ``name``, an element's ``key`` — so dropping it changes no
  geometry.
* ``steps`` is the *indices a capture read*, not what the capturing step drew. Two captures
  over different spans of identical geometry are the same element, so they must group.

**What counts as repetition.** A step the drawing does not contain is not a repeat of one it
does. The inactive states are the ones `critique.py` already names — ``undone``, ``planned``
(the plugin's promise not yet run), ``failed`` and ``rejected`` — and they are left out. A
row whose ``state`` is missing is skipped and counted in the answer's ``skipped``, as is a
row that is not a step row at all or records no ``op``. Everything else is applied: the
Python journal writes ``applied`` and the plugin's in-DWG journal writes ``done`` for the
same fact, so both count rather than making the plugin backend find nothing.

**Ops whose repetition earns nothing** are named in :data:`IGNORED_OPS` and dropped before
grouping, and they are *not* counted in ``skipped`` — a row that read fine is not a row that
could not be read. A placement (``place_element``/``place_along``/``place_grid``) drew a
catalogue element and an ``element_capture`` made one, so both are already reusable: the
derived key would be the placed element's own key, and the printed ``element_capture`` call
would fail verbatim. Only the primitive tools that drew ad-hoc geometry are mined.

**The answer.** ``{"suggestions": [...], "skipped": int}``, the groups ordered by count
descending (ties in the order they first appeared). Each group is ``{"op", "count", "params",
"steps", "hint"}`` — the shared parameters canonicalised, the step indices, and a ``hint`` of
the literal ``element_capture`` call and the ``element_publish`` that would sediment them.

**The suggested key is derived, never stored** (the rule `catalog.next_tag` follows). The
name is the step's own subject — the ``type`` when the parameters name one, else the op with
its verb dropped (``draw_door`` → ``door``, ``add_text`` → ``text``) — and a size token is
appended from the first of :data:`SIZE_PARAMS` the parameters carry, so five 900 mm doors
suggest ``door_900`` and a 600 column suggests ``column_600``. The whole thing is slugged to
``[a-z0-9_]``. The hint says the derived key may already be taken, because a name this module
invents cannot know what the catalogue holds.

`min_count` is at least 2: the premise is repetition, and a single step is not a repeat.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

#: Param keys that describe a step's bookkeeping rather than the geometry it drew. Named so
#: the decision is one place to change; the module docstring argues each one.
IGNORED_PARAM_KEYS = frozenset({"label", "steps"})

#: Ops whose repetition earns no suggestion, because the thing they drew is *already* reusable.
#: ``place_element``/``place_along``/``place_grid`` placed a catalogue element, and
#: ``element_capture`` made one — repeating either needs no capture, and the key a capture would
#: derive is the placed element's own key, so the printed hint call would fail verbatim. Only the
#: primitive tools that drew ad-hoc geometry (``draw_door``, ``draw_window``) are worth mining.
IGNORED_OPS = frozenset({"place_element", "place_along", "place_grid", "element_capture"})

#: Step states that mean the drawing does not contain the step, so it is not a repeat. The
#: vocabulary `critique.py` keeps: the Python journal writes "applied"/"undone", the plugin's
#: in-DWG journal writes "done"/"planned"/"failed", and "rejected" is the palette's own word.
INACTIVE_STATES = frozenset({"undone", "planned", "failed", "rejected"})

#: The parameter that names a thing's size, in the order to prefer when a step records more
#: than one: a catalogue placement's `variant`, then the width a door or window is cut to,
#: then a column's own size and a circle's radius.
SIZE_PARAMS = ("variant", "width", "size", "radius")

#: One decimal place, i.e. 0.1 mm — the resolution at which two steps are "the same thing".
_MM = 1

_SLUG = re.compile(r"[^0-9a-z]+")


def _canonical(value: Any) -> Any:
    """`value` with the decisions that make two equal-enough params equal made explicit.

    Mappings are key-sorted and recursed (with :data:`IGNORED_PARAM_KEYS` dropped — the only
    place they are); lists and tuples keep their order, since a ring drawn the other way is a
    different ring; every number becomes a float rounded to 0.1 mm, so an int 900 and a float
    900.0 group as one; anything else is itself.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {
            key: _canonical(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
            if str(key) not in IGNORED_PARAM_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (int, float)):
        # `+ 0.0` folds a negative zero into a positive one: round(-0.0, 1) stays -0.0, and
        # json.dumps prints the sign, so two windows at x=-0.0 and x=0.0 would never group.
        return round(float(value), _MM) + 0.0
    return value


def _group_key(op: str, params: dict[str, Any]) -> str:
    """The canonical parameters as a hashable key, so two rows sort into one bucket together.

    JSON rather than a frozen tuple because the params nest, and JSON is already the shape
    these values travelled in — the journal is a JSON payload inside the DXF.
    """
    return json.dumps(_canonical(params), ensure_ascii=False, sort_keys=True)


def _slug(text: Any) -> str:
    return _SLUG.sub("_", str(text).strip().lower()).strip("_")


def _subject(op: str) -> str:
    """The thing a step drew, from its op name alone: the verb prefix dropped."""
    for verb in ("draw_", "add_", "place_"):
        if op.startswith(verb):
            return op[len(verb) :]
    return op


def _size_token(params: dict[str, Any]) -> str:
    """The first of :data:`SIZE_PARAMS` the params carry, as a slug — "" when there is none."""
    for name in SIZE_PARAMS:
        value = params.get(name)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return f"{round(float(value), _MM) + 0.0:g}"
        token = _slug(value)
        if token:
            return token
    return ""


def _key_and_noun(op: str, params: dict[str, Any]) -> tuple[str, str]:
    """The suggested element name and the noun the hint reads, both derived from the step."""
    thing = str(params.get("type") or "").strip() or _subject(op)
    noun = thing.replace("_", " ").strip() or op
    token = _size_token(params)
    key = _slug(f"{thing}_{token}") if token else _slug(thing)
    return key or _slug(op), noun


def _index_of(row: dict[str, Any], position: int) -> int:
    """The step's own number, falling back to where it sits in the list."""
    try:
        return int(row.get("index", position))
    except (TypeError, ValueError):
        return position


def _report(op: str, params: dict[str, Any], steps: list[int]) -> dict[str, Any]:
    """One suggestion: what repeated, how often, where, and the call worth making.

    The hint carries the steps as the literal list the group's own ``steps`` holds, so a
    reader can paste it straight into `element_capture` without counting.
    """
    key, noun = _key_and_noun(op, params)
    count = len(steps)
    hint = (
        f"these {count} steps drew the same {noun} — "
        f"element_capture(steps={steps}, key={key!r}) makes it a library element; "
        f"element_publish puts it in the project catalogue — pick another key if that one "
        f"is taken"
    )
    return {"op": op, "count": count, "params": params, "steps": steps, "hint": hint}


def suggestions(step_rows: Sequence[dict], min_count: int = 3) -> dict[str, Any]:
    """The repeated things in the journal, as ``{"suggestions": [...], "skipped": int}``.

    `step_rows` is the journal in the shape both owners report it — ``index``, ``op``,
    ``label``, ``params``, ``state`` — which is what ``Backend.step_rows()`` returns. The
    module docstring states the grouping and the key-derivation rules.
    """
    if min_count < 2:
        raise ValueError(
            f"min_count must be at least 2, got {min_count!r}: a suggestion is about "
            "repetition, and a single step is not a repeat"
        )
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    skipped = 0
    for position, raw in enumerate(step_rows or [], start=1):
        if not isinstance(raw, dict):
            skipped += 1
            continue
        state = raw.get("state")
        if not isinstance(state, str) or not state.strip():
            skipped += 1
            continue
        if state.strip().lower() in INACTIVE_STATES:
            continue
        op = raw.get("op")
        if not isinstance(op, str) or not op.strip():
            skipped += 1
            continue
        op = op.strip()
        if op in IGNORED_OPS:
            # Already reusable: a placement drew a catalogue element, a capture made one. No
            # suggestion — and not counted in `skipped`, which is for rows that cannot be read.
            continue
        params = raw.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            skipped += 1
            continue
        try:
            key = _group_key(op, params)
        except (TypeError, ValueError):
            # A param value JSON cannot hold is skipped and counted, never raised — the rule
            # the catalogue loader and the journal reader already keep.
            skipped += 1
            continue
        group = groups.get(key)
        if group is None:
            group = {"op": op, "params": _canonical(params), "steps": []}
            groups[key] = group
            order.append(key)
        group["steps"].append(_index_of(raw, position))

    found = [
        _report(groups[key]["op"], groups[key]["params"], list(groups[key]["steps"]))
        for key in order
        if len(groups[key]["steps"]) >= min_count
    ]
    # Stabilised on count alone, so ties keep first-appearance order and the answer is
    # deterministic without a second sort key nobody asked for.
    found.sort(key=lambda group: -group["count"])
    return {"suggestions": found, "skipped": skipped}
