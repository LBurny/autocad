"""The parametric twins: what a catalogue entry draws where a library block cannot go.

An element's *identity* is its catalogue entry — a model number, a layer, a tag prefix, a
set of sizes. How that identity reaches the paper is the backend's business: the plugin
inserts the library block the standard owns, and a backend with no library to read draws
the same door out of line and arc. Both have to be reachable through one
``place_element("door", ...)``, which is what these adapters are for.

An adapter is a thin normaliser, and its signature is the catalogue's rather than the op's::

    adapter(be, at, *, width=None, rotation=0.0, scale=1.0, layer="", **params) -> list[Any]

``at`` is the element's *own* reference point — a column's centre, a door's hinge, a lift's
first corner — because that is what a caller placing "a door here" means. The signature is
deliberately uniform (``tests/test_catalog_ops.py`` pins it by introspection) so a catalogue
entry can name any of them and be called one way. ``scale`` is honoured only where it means
something — a captured block reference — and refused by name by every twin, which is built
from its own real-size dimensions.

Nothing here computes geometry. Each adapter calls the op in :mod:`acadmcp.ops` that already
draws this thing, and that op is where the coordinates live, so a catalogue element drawn
by an op lands in the same place as one drawn by the matching tool.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from . import geom, layers, ops
from .backends.base import BackendError
from .geom import Point

#: The names `place_element` passes the adapter itself rather than through a catalogue's
#: `params`: they are tool arguments (a caller's rotation/scale, the resolved layer), not
#: properties of the element. Excluded from the vocabulary a caller's `params` is checked
#: against, so passing one of them as a param is a refusal rather than a duplicate keyword.
_TOOL_LEVEL = frozenset({"rotation", "layer", "scale"})


def param_vocabulary(adapter: Callable[..., list[Any]]) -> list[str]:
    """The parameter names a catalogue entry or a caller may set on this twin.

    An adapter tolerates ``**params`` so a catalogue's own declared keys can be splatted
    straight in, which also makes a misspelled key silently ignorable — a caller could set a
    size that reached nothing and be told it worked. This is the vocabulary that turns such a
    key into a refusal: the adapter's own named parameters, minus the ones the tool passes
    directly (`rotation`, `layer`, `scale`).

    The adapter's names *are* the twin's vocabulary, which is exactly what a catalogue states
    under ``op_params`` — the door block calls its width 门尺寸 while ``door_symbol`` calls it
    ``width`` — so a legitimate declared override is always in this list.
    """
    names = [
        name
        for name, parameter in inspect.signature(adapter).parameters.items()
        if name not in ("be", "at")
        and name not in _TOOL_LEVEL
        and parameter.kind
        in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return sorted(names)


def _no_scale(what: str, scale: float) -> None:
    """Refuse to scale something whose adapter draws it at real size.

    A parametric twin is built from its own dimensions — a door from its `width`, a column
    from its `size` — so a uniform scale would produce a door of no standard size at all,
    which is the right-looking wrong element the catalogue exists to prevent. The size asked
    for is the way to change it (a variant, or a measured width); a uniform scale only means
    something for a captured block reference, whose geometry is fixed.
    """
    if abs(float(scale) - 1.0) > 1e-9:
        raise ValueError(
            f"{what} is drawn at its own real size from its dimensions, so it cannot be "
            f"scaled by {scale:g}; place it at the size wanted (a variant, or a measured "
            "width), or scale a captured block instead"
        )


def _no_rotation(what: str, rotation: float) -> None:
    """Refuse to rotate something whose op cannot rotate.

    `draw_lift`, `draw_shaft`, `draw_equipment` and `draw_stair` all build an axis-aligned
    rectangle from two corners, and their innards — the X cross, the caption, a stair's
    treads and 上/下 labels — would not follow a rotated outline. Rotating the two corners
    before handing them over does not help: `geom.rect_corners` rebuilds the rectangle
    axis-aligned from whatever it is given. So this refuses rather than drawing an upright
    shaft where a rotated one was asked for.
    """
    if float(rotation) % 360.0:
        raise ValueError(
            f"{what} is drawn as an axis-aligned rectangle, so it cannot be rotated by "
            f"{rotation:g} degrees; rotate the coordinates you place it at, or use an "
            "element whose op takes a rotation (column, door_symbol, window_symbol)"
        )


def _no_rotation_symbol(what: str, rotation: float) -> None:
    """Refuse to rotate a drafting symbol whose op draws it in one fixed orientation.

    Unlike a rectangle (see :func:`_no_rotation`), moving the coordinates would not help: the
    symbol's whole shape is defined in its own frame and its op takes no rotation. A level
    mark is read apex-down and a north arrow points up the sheet by convention, so a rotated
    one is a different mark, not the same mark turned.
    """
    if float(rotation) % 360.0:
        raise ValueError(
            f"{what} is drawn in one fixed orientation and cannot be rotated by "
            f"{rotation:g} degrees; place it where its orientation is wanted, or rotate the "
            "drawing"
        )


def _no_scale_symbol(what: str, scale: float) -> None:
    """Refuse to scale a symbol whose size is a paper measure, not the caller's.

    Every dimension of these symbols is a *paper* millimetre times the sheet's plot scale, so
    the drawing's own scale already fixes the size — a further uniform scale would draw
    off-series geometry on the paper, the very thing the standard check exists to catch. Change
    the sheet's scale instead, or scale a captured block reference, which really does carry one.
    """
    if abs(float(scale) - 1.0) > 1e-9:
        raise ValueError(
            f"{what} is sized in paper millimetres by the sheet's own scale, so it cannot be "
            f"scaled by {scale:g}; set the sheet's scale instead"
        )


def column(
    be,
    at: Point,
    *,
    size: float = layers.COLUMN_SIZE,
    depth: float | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    hatch: bool = True,
    width: float | None = None,
    layer: str = "A-COLUMN",
    **params: Any,
) -> list[Any]:
    """A column by its centre. `width` is the column's plan size, so a 承力柱 placed into a
    wall opening comes out as thick as the opening rather than at some nominal size."""
    _no_scale("a column", scale)
    if width is not None:
        size = float(width)
    return ops.draw_column(be, at, size, rotation=rotation, depth=depth, layer=layer, hatch=hatch)


def door_symbol(
    be,
    at: Point,
    *,
    width: float = layers.DOOR_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    swing: str = "left",
    open_angle: float = 90.0,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str = "A-DOOR",
    **params: Any,
) -> list[Any]:
    """A single-leaf door by its hinge — the same hinge `draw_door` takes, which is what an
    opening in a wall knows: the jamb it starts at."""
    _no_scale("a door symbol", scale)
    return ops.draw_door(
        be,
        at,
        width,
        wall_thickness,
        rotation=rotation,
        open_angle=open_angle,
        swing=swing,
        layer=layer,
    )


def window_symbol(
    be,
    at: Point,
    *,
    width: float = layers.WINDOW_WIDTH,
    wall_thickness: float = layers.WALL_THICKNESS,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str = "A-WINDOW",
    **params: Any,
) -> list[Any]:
    """A window by its first jamb — the same point `draw_window` starts from."""
    _no_scale("a window symbol", scale)
    return ops.draw_window(be, at, width, wall_thickness, rotation=rotation, layer=layer)


def lift(
    be,
    at: Point,
    *,
    width: float = 2400.0,
    depth: float = 2200.0,
    tag: str | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str = "A-LIFT",
    **params: Any,
) -> list[Any]:
    """A lift shaft by its lower-left corner: `width` along +X, `depth` along +Y."""
    _no_rotation("a lift shaft", rotation)
    _no_scale("a lift shaft", scale)
    x, y = geom.xy(at)
    return ops.draw_lift(be, (x, y), (x + float(width), y + float(depth)), tag=tag, layer=layer)


def stair(
    be,
    at: Point,
    *,
    width: float = 1200.0,
    depth: float = 3000.0,
    steps: int = 9,
    axis: str = "y",
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str = "A-STAIR",
    **params: Any,
) -> list[Any]:
    """A flight by its lower-left corner; `axis` is the direction it travels, as
    `draw_stair` takes it."""
    _no_rotation("a stair flight", rotation)
    _no_scale("a stair flight", scale)
    x, y = geom.xy(at)
    return ops.draw_stair(
        be,
        (x, y),
        (x + float(width), y + float(depth)),
        steps=int(steps),
        axis=axis,
        layer=layer,
    )


def shaft(
    be,
    at: Point,
    *,
    width: float = 1500.0,
    depth: float = 1200.0,
    name: str | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str = "A-SHAFT",
    **params: Any,
) -> list[Any]:
    """A service riser by its lower-left corner."""
    _no_rotation("a service shaft", rotation)
    _no_scale("a service shaft", scale)
    x, y = geom.xy(at)
    return ops.draw_shaft(be, (x, y), (x + float(width), y + float(depth)), name, layer=layer)


def equipment(
    be,
    at: Point,
    *,
    width: float = 800.0,
    depth: float = 600.0,
    name: str | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    layer: str = "A-EQUIP",
    **params: Any,
) -> list[Any]:
    """A piece of fixed equipment by its lower-left corner."""
    _no_rotation("a piece of equipment", rotation)
    _no_scale("a piece of equipment", scale)
    x, y = geom.xy(at)
    return ops.draw_equipment(be, (x, y), (x + float(width), y + float(depth)), name, layer=layer)


#: The section mark's cut-line length in mm when a caller gives none. The uniform adapter
#: signature carries one point and the tool's own `rotation`, so the mark's second end is
#: derived as `at` plus this far along the cut direction. A section cut normally spans the whole
#: building, so a caller with a real plan to section states its own `length`.
SECTION_CUT_LENGTH = 6000.0


def level_mark(
    be,
    at: Point,
    *,
    text: str = "\u00b10.000",
    mirror: bool = False,
    rotation: float = 0.0,
    scale: float = 1.0,
    width: float | None = None,
    layer: str = "A-DIM",
    **params: Any,
) -> list[Any]:
    """A level mark by the point whose level it notes — the triangle's apex, the same `at`
    `draw_level_mark` takes. The level itself is `text`, and `mirror` flips the mark to the
    other side of that point; a mark is read in one orientation, so a rotation is refused."""
    _no_scale_symbol("a level mark", scale)
    _no_rotation_symbol("a level mark", rotation)
    return ops.draw_level_mark(be, at, text, mirror=mirror, layer=layer)


def north_arrow(
    be,
    at: Point,
    *,
    label: str = "\u5317",
    rotation: float = 0.0,
    scale: float = 1.0,
    width: float | None = None,
    layer: str = "A-ANNO",
    **params: Any,
) -> list[Any]:
    """A north arrow by its circle's centre — the point `draw_north_arrow` takes. The arrow
    points up the sheet, so a rotation is refused rather than drawing a compass whose needle
    no longer means north; `label` is the text above the circle."""
    _no_scale_symbol("a north arrow", scale)
    _no_rotation_symbol("a north arrow", rotation)
    return ops.draw_north_arrow(be, at, label=label, layer=layer)


def section_mark(
    be,
    at: Point,
    *,
    label: str = "1-1",
    length: float = SECTION_CUT_LENGTH,
    side: str = "left",
    rotation: float = 0.0,
    scale: float = 1.0,
    width: float | None = None,
    layer: str = "A-ANNO",
    **params: Any,
) -> list[Any]:
    """A section mark by its first end. `at` is p1; `rotation` is the cut direction in degrees
    CCW from +X and `length` how far the cut line runs to p2 — the two the uniform signature
    carries, so the mark's second end is derived rather than passed. `label` names the section
    and `side` picks which side of the travel the viewing ticks sit on."""
    _no_scale_symbol("a section mark", scale)
    x, y = geom.xy(at)
    p2 = geom.polar((x, y), float(rotation), float(length))
    return ops.draw_section_mark(be, (x, y), p2, label, side=side, layer=layer)


def insert_captured(
    be,
    at: Point,
    *,
    rotation: float = 0.0,
    scale: float = 1.0,
    width: float | None = None,
    layer: str = "",
    **params: Any,
) -> list[Any]:
    """A captured element: a block definition that lives in the drawing, placed at `at`.

    The definition is not a library block — a capture wrote it into the drawing from
    geometry that was drawn and checked — so this works on any backend the capture worked
    on, and the name comes from the entry's `params` rather than from a library file. `at`
    is the base point the capture chose, which its own docstring records: a door's hinge, a
    component's corner.

    A measured `width` is refused rather than ignored. The block is fixed geometry, and a
    capture does not carry what the geometry is measured *from*, so there is no honest way to
    resize it — drawing it at its captured size into a measured hole is the wrong size that
    looks right. A uniform `scale`, on the other hand, *is* honoured: unlike a twin, which is
    built from its dimensions, a block reference has a real scale factor, so this is where the
    block path and the op path finally mean the same thing.

    Where the drawing has never seen the definition — a fresh drawing placing an element that
    was *published* from another — the published geometry fragment is imported and the block
    written into this drawing first. Only the dxf driver can write a definition, so the live
    drivers keep refusing by name, exactly as before.
    """
    block = str(params.get("block", "")).strip()
    if not block:
        raise ValueError(
            "a captured element needs the definition it places, under 'block' in its catalogue "
            "entry's params; re-register it, or place it with place_element from a capture"
        )
    if width is not None:
        raise ValueError(
            "a captured element is a block of the geometry that was captured, so it cannot be "
            "resized to a measured width; capture it at the size wanted, or use a parametric "
            "entry whose twin takes a width"
        )
    _import_fragment_if_needed(be, block)
    return [be.insert(block, at, rotation=rotation, scale=scale, layer=layer or "0")]


def _import_fragment_if_needed(be, block: str) -> None:
    """Bring a published element's definition into a dxf drawing that lacks it.

    A no-op on every other backend, whose `insert` refuses by name as it always has. On dxf,
    when the definition is absent but the entry carries a ``geometry_file``, the fragment is
    read back and its definition written — document state, so a rollback never erases it, the
    same rule the capture that made it follows.
    """
    doc = getattr(be, "doc", None)
    if doc is None or not getattr(be, "supports_block_create", False):
        return
    if block in doc.blocks:
        return

    from pathlib import Path

    from . import capture_ops
    from . import catalog as _catalog

    loaded = _catalog.current()
    element = next(
        (e for e in loaded.elements.values() if str(e.params.get("block", "")).strip() == block),
        None,
    )
    if element is None:
        # Not an entry this catalogue knows: let `insert` refuse by name, as before.
        return
    geometry_file = str(element.origin.get("geometry_file", "")).strip()
    if not geometry_file:
        # The catalogue knows the element, so it was captured — but a capture's definition
        # lives only in the drawing that captured it, and this drawing is not that one.
        # Naming element_capture here would send the caller in a circle; publishing is the
        # step that lets a fresh drawing place it.
        raise BackendError(
            f"'{element.key}' was captured but never published, so its block definition "
            "lives only in the drawing that captured it. Open that drawing and publish it "
            "(element_publish), or capture the element again here (element_capture)"
        )

    base = loaded.source_file(element.key)
    if base is None:
        raise BackendError(
            f"'{element.key}' carries a geometry fragment, but the entry came from the drawing "
            "itself (or was captured at runtime), so its fragment path has no catalogue file to "
            "resolve against; publish it into a project catalogue file and reopen, or capture "
            "the element in this drawing"
        )
    fragment_path = Path(base).parent / geometry_file
    if not fragment_path.is_file():
        raise BackendError(
            f"'{element.key}' names the geometry fragment {geometry_file!r}, but "
            f"{str(fragment_path)!r} is not there; republish the element from the drawing that "
            "captured it"
        )
    name, primitives, base_point = capture_ops.import_fragment(str(fragment_path))
    be.create_block(name, primitives, base_point)


#: What a catalogue entry may name in its ``op``: the single list a catalogue is written
#: against. Kept as data so the registry, the refusal and the documentation cannot drift.
CATALOG_OPS: dict[str, Callable[..., list[Any]]] = {
    "column": column,
    "door_symbol": door_symbol,
    "window_symbol": window_symbol,
    "lift": lift,
    "stair": stair,
    "shaft": shaft,
    "equipment": equipment,
    "level_mark": level_mark,
    "north_arrow": north_arrow,
    "section_mark": section_mark,
    "insert_captured": insert_captured,
}


def get(name: str) -> Callable[..., list[Any]]:
    """The parametric builder an entry names, or a refusal listing the ones there are."""
    adapter = CATALOG_OPS.get((name or "").strip().lower())
    if adapter is None:
        raise ValueError(
            f"no parametric builder {name!r}; a catalogue entry may name: "
            + ", ".join(sorted(CATALOG_OPS))
        )
    return adapter
