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

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog, geom
from .backends.base import Backend, BackendError
from .backends.recording import unflatten_points


def _round_tenth(value: Any) -> float:
    """A coordinate rounded to 0.1 mm — close enough that two captures of the same geometry
    agree, and precise enough that moving a door a millimetre is a different geometry."""
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


def _canonical_primitive(prim: dict[str, Any]) -> tuple:
    """One primitive in the shape the fingerprint hashes.

    Every geometric field a primitive can carry is here, each read with a default so the
    tuple's shape is the same for every kind and a wire that omits a field hashes as its
    neutral value rather than as a different shape. The points and the text are not enough on
    their own: two concentric circles that differ only in radius used to fingerprint alike, so
    a genuinely different element was refused as a duplicate of the first and its message said
    so wrongly.

    This changes what a capture hashes whenever it changes, so an entry stamped by an earlier
    scheme stops matching a new capture of the same geometry — which costs one re-capture,
    never a wrong placement.
    """
    flat = [_round_tenth(v) for v in (prim.get("points") or [])]
    points = tuple(sorted((flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)))
    return (
        str(prim.get("kind", "")),
        str(prim.get("layer", "")),
        str(prim.get("linetype", "")),
        points,
        str(prim.get("text", "")),
        _round_tenth(prim.get("height", 0.0)),
        _round_tenth(prim.get("width", 0.0)),
        str(prim.get("align", "")),
        _round_tenth(prim.get("radius", 0.0)),
        _round_tenth(prim.get("start_angle", 0.0)),
        _round_tenth(prim.get("end_angle", 0.0)),
        _round_tenth(prim.get("rotation", 0.0)),
        _round_tenth(prim.get("scale_x", 0.0)),
        _round_tenth(prim.get("scale_y", 0.0)),
        _round_tenth(prim.get("offset", 0.0)),
        _round_tenth(prim.get("text_height", 0.0)),
        str(prim.get("pattern", "")),
        _round_tenth(prim.get("scale", 0.0)),
        _round_tenth(prim.get("angle", 0.0)),
        bool(prim.get("solid", False)),
        bool(prim.get("closed", False)),
        str(prim.get("block", "")),
    )


def fingerprint_primitives(prims: list[dict[str, Any]]) -> str:
    """A sha256 over a canonicalisation of the captured geometry.

    Order-free on purpose: how a caller split a capture across steps is its own choice, so the
    same geometry gathered as one step or two must fingerprint the same. That is what lets a
    second capture of an element the library already holds be *recognised* and refused, rather
    than quietly registered as a duplicate under a new key.
    """
    rows = sorted({_canonical_primitive(p) for p in (prims or []) if isinstance(p, dict)})
    blob = json.dumps(rows, ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    """The capture time, UTC and second-precision — a stamp a person can read, not a clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


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
    holds and the base point as placed. Refuses, loudly and by name: an empty gathering, a
    step index repeated (it would draw that geometry into the block twice), a step that is
    missing or undone, sheet furniture (paper millimetres are not model geometry), geometry
    the library already holds under another key, a key the catalogue already has, and a
    backend that cannot write a definition at all.
    """
    wanted = [int(step) for step in (steps or [])]
    if not wanted:
        raise ValueError(
            "a capture needs at least one step to gather geometry from; none was given"
        )
    seen: set[int] = set()
    for step in wanted:
        if step in seen:
            # Gathered twice, the step's primitives are concatenated twice and the block
            # holds each entity twice — invisible in the model, and the fingerprint then
            # refuses the honest single-copy capture as a duplicate of it.
            raise ValueError(
                f"step {step} is listed twice — each step contributes its geometry once"
            )
        seen.add(step)
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

    fingerprint = fingerprint_primitives(primitives)
    for other in loaded.elements.values():
        if other.origin.get("fingerprint") == fingerprint:
            # The same geometry under another name is the same element, not a new one. Both the
            # existing key and the fingerprint are named, so the caller can reuse the entry —
            # or see which geometry it collided with, since the reply of a successful capture
            # carries the same string.
            raise ValueError(
                f"the library already holds this geometry as '{other.key}' ({fingerprint}) — "
                "capture refused; rename only if it is genuinely a different element"
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

    info = be.info() if hasattr(be, "info") else {}
    origin = {
        # A dxf drawing that has never been saved has no path, and "" is the honest answer.
        "source_drawing": str((info or {}).get("path") or ""),
        "steps": list(wanted),
        "captured_at": _utc_now(),
        "fingerprint": fingerprint,
    }
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
            # Provenance: which drawing, which steps, when, and a fingerprint of the geometry.
            # It rides `describe()` into the drawing, so the element can be traced on another
            # machine; it is excluded from `digest()`, which is placement semantics only.
            "origin": origin,
        }
    )
    return {
        "block": name,
        "key": element.key,
        "entities": int(made.get("entities", len(primitives))),
        "base_point": [float(origin_x), float(origin_y)],
        "fingerprint": fingerprint,
    }


# ---------------------------------------------------------------------------
# publishing the geometry as a fragment a fresh drawing can import
# ---------------------------------------------------------------------------


def export_fragment(
    path: str,
    name: str,
    primitives: list[dict[str, Any]],
    base_point: list[float] | tuple[float, float],
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the geometry of a captured element to a headless DXF as a block definition.

    This is the second half of a publish: the catalogue entry is a name, and this file is what
    the name resolves to on a machine that never saw the drawing it was captured in. The
    document holds a single block definition (``create_block``), and the element's own
    geometry — the journal's primitives, its base point and whatever `provenance` carries (the
    element key and its fingerprint) — is recorded in the journal meta under
    ``exported_element``. The DXF backend persists that meta, so the fragment is self-describing.

    The primitives are the journal's own vocabulary: there is deliberately **no**
    entity→primitive reverse mapping here, because the recorded primitives are the one
    canonical form of the geometry and anything re-derived from the block table would be a
    second source of truth for the same shape.

    Written atomically: a temp file in the same directory, then `os.replace`.
    """
    from .backends.dxf import DxfBackend

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    be = DxfBackend(create=True)
    be.create_block(str(name), primitives, base_point)

    payload: dict[str, Any] = {
        "block": str(name),
        "primitives": [dict(p) for p in primitives],
        "base_point": [float(base_point[0]), float(base_point[1])],
    }
    payload.update(dict(provenance or {}))
    be.journal.note_meta(exported_element=payload)

    tmp = target.with_name(target.name + ".part.dxf")
    be.save(str(tmp))
    os.replace(tmp, target)
    return {
        "path": str(target),
        "key": str(payload.get("key", "")),
        "block": str(name),
        "primitives": len(primitives),
        "fingerprint": str(payload.get("fingerprint", "")),
    }


def import_fragment(path: str) -> tuple[str, list[dict[str, Any]], list[float]]:
    """Read a published fragment back: its block name, primitives and base point.

    A file that carries no ``exported_element`` was not written by :func:`export_fragment`, so
    there is no geometry to import and it is refused by name rather than guessed at.
    """
    from .backends.dxf import DxfBackend

    be = DxfBackend(path=str(path))
    payload = be.journal.meta.get("exported_element")
    if not isinstance(payload, dict) or not str(payload.get("block", "")).strip():
        raise ValueError(
            f"{path!r} carries no exported element: it was not written by element_publish, so "
            "there is no captured geometry to import"
        )
    base = payload.get("base_point") or [0.0, 0.0]
    return (
        str(payload["block"]),
        [dict(p) for p in payload.get("primitives", [])],
        [float(base[0]), float(base[1])],
    )
