"""Turning verified geometry into a reusable catalogue element.

A misdrawn component that has been fixed once should stay fixed. The fix for that is not a
longer chat history — it is code and data: the geometry that was drawn and checked becomes a
block definition in the drawing, and a catalogue entry that places it by name. That is what
this module does.

It follows the :mod:`acadmcp.ops` rule: backend-agnostic, and the geometry it does is through
:mod:`acadmcp.geom`. What it reads is the journal's own account of what was drawn — the
primitives each step recorded — so a capture works on any backend that records steps, and
never on entities somebody edited by hand. A row it cannot read is refused rather than
guessed at, because capturing the wrong geometry is exactly the mistake this exists to stop.

`create_block` is called *directly*, never through a recorded draw method: a block definition
is document state, like the page setup, so no step owns it and a rollback never erases it.
When a step is open — which it is inside the tool that wraps this — the capture records its
intent into that step so the call is one undoable step like every other; only the block
reference placed later is an ordinary journalled entity.
"""

from __future__ import annotations

from typing import Any

from . import catalog, geom
from .backends.base import Backend, BackendError
from .backends.recording import unflatten_points


def capture(
    be: Backend,
    *,
    steps: list[int],
    key: str,
    label: str = "",
    tag_prefix: str = "",
    layer: str = "",
    category: str = "captured",
    base_point: list[float] | None = None,
) -> dict[str, Any]:
    """Capture the geometry named steps recorded as a reusable catalogue element.

    `steps` are journal step numbers, which is what makes the capture honest: it takes what
    the drawing *says* it drew, not what happens to be lying about afterwards. `key` names the
    element and is the name it is placed by; `label`, `tag_prefix` and `layer` are the entry's
    own fields and are the caller's to choose.

    `base_point` is the reference point every later placement is measured from — a door's
    hinge, a component's corner — and it becomes the definition's origin. With none given it
    is the min corner of the captured geometry's bounding box, which is the neutral choice: a
    rectangle's lower-left.

    Returns the definition's name, the key it was registered under, how many primitives it
    holds and the base point as placed. Refuses, loudly and by name: an empty gathering, a step
    that is missing or undone, sheet furniture (paper millimetres are not model geometry), a
    key the catalogue already has, and a backend that cannot write a definition at all.
    """
    wanted = [int(step) for step in (steps or [])]
    if not wanted:
        raise ValueError(
            "a capture needs at least one step to gather geometry from; none was given"
        )
    key = str(key or "").strip().lower()
    if not key:
        raise ValueError("a capture needs a key: it is the name the element is placed by")

    if not getattr(be, "supports_block_create", False):
        raise BackendError(
            f"backend {getattr(be, 'name', '?')!r} cannot create a block definition: capturing "
            "an element writes a block into the drawing's own block table, which only the dxf "
            "driver does this wave. Draw the element into a DXF drawing "
            "(drawing_new(backend='dxf')) and capture it there"
        )

    primitives = be.step_primitives(wanted)
    if not primitives:
        raise ValueError(
            f"steps {wanted} recorded no geometry, so there is nothing to capture; name the "
            "steps that drew the element"
        )
    for prim in primitives:
        if prim.get("paper"):
            raise ValueError(
                f"step geometry includes a {prim.get('kind', '?')} primitive drawn on the "
                "sheet: a frame is paper millimetres and a block is model geometry. Capture the "
                "model steps only"
            )

    loaded = catalog.current()
    if loaded.get(key) is not None:
        raise ValueError(
            f"the catalogue already has an entry '{key}'; a capture adds an element rather "
            "than replacing one — choose another key"
        )

    points = [p for prim in primitives for p in unflatten_points(prim.get("points", []))]
    if base_point is not None:
        origin_x, origin_y = geom.xy(base_point)
    elif points:
        min_x, min_y, _, _ = geom.bbox(points)
        origin_x, origin_y = min_x, min_y
    else:
        raise ValueError(
            "no base point was given and the captured primitives carry no points to derive one "
            "from; pass base_point"
        )

    name = f"ACADMCP_{key.upper()}"
    made = be.create_block(name, primitives, (origin_x, origin_y))
    if getattr(be, "open_step", ""):
        # One tool call is one step and a step that recorded nothing cannot be committed, so
        # the capture records the definition it made. The definition itself stays document
        # state: applying the intent only ensures it is there, and a rollback leaves it.
        be.record_block_intent(name, primitives, (origin_x, origin_y))

    element = loaded.register(
        {
            "key": key,
            "label": label or key,
            "category": category,
            "layer": layer,
            "tag_prefix": tag_prefix,
            "op": "insert_captured",
            # Op-only: the definition lives in the drawing, not in a library, so there is no
            # `block`/`source` to name. An op-only entry's `params` are already the twin's
            # vocabulary — the shape the `column` entry uses — and `insert_captured` reads the
            # definition's name from there.
            "params": {"block": name},
        }
    )
    return {
        "block": name,
        "key": element.key,
        "entities": int(made.get("entities", len(primitives))),
        "base_point": [float(origin_x), float(origin_y)],
    }
