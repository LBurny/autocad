"""The parametric twins: what a catalogue entry draws where a library block cannot go.

An element's *identity* is its catalogue entry — a model number, a layer, a tag prefix, a
set of sizes. How that identity reaches the paper is the backend's business: the plugin
inserts the library block the standard owns, and a backend with no library to read draws
the same door out of line and arc. Both have to be reachable through one
``place_element("door", ...)``, which is what these adapters are for.

An adapter is a thin normaliser, and its signature is the catalogue's rather than the op's::

    adapter(be, at, *, width=None, rotation=0.0, layer="", **params) -> list[Any]

``at`` is the element's *own* reference point — a column's centre, a door's hinge, a lift's
first corner — because that is what a caller placing "a door here" means. The signature is
deliberately uniform (``tests/test_catalog_ops.py`` pins it by introspection) so a catalogue
entry can name any of them and be called one way.

Nothing here computes geometry. Each adapter calls the op in :mod:`acadmcp.ops` that already
draws this thing, and that op is where the coordinates live, so a catalogue element drawn
by an op lands in the same place as one drawn by the matching tool.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import geom, layers, ops
from .geom import Point


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


def column(
    be,
    at: Point,
    *,
    size: float = layers.COLUMN_SIZE,
    depth: float | None = None,
    rotation: float = 0.0,
    hatch: bool = True,
    width: float | None = None,
    layer: str = "A-COLUMN",
    **params: Any,
) -> list[Any]:
    """A column by its centre. `width` is the column's plan size, so a 承力柱 placed into a
    wall opening comes out as thick as the opening rather than at some nominal size."""
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
    layer: str = "A-DOOR",
    **params: Any,
) -> list[Any]:
    """A single-leaf door by its hinge — the same hinge `draw_door` takes, which is what an
    opening in a wall knows: the jamb it starts at."""
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
    layer: str = "A-WINDOW",
    **params: Any,
) -> list[Any]:
    """A window by its first jamb — the same point `draw_window` starts from."""
    return ops.draw_window(be, at, width, wall_thickness, rotation=rotation, layer=layer)


def lift(
    be,
    at: Point,
    *,
    width: float = 2400.0,
    depth: float = 2200.0,
    tag: str | None = None,
    rotation: float = 0.0,
    layer: str = "A-LIFT",
    **params: Any,
) -> list[Any]:
    """A lift shaft by its lower-left corner: `width` along +X, `depth` along +Y."""
    _no_rotation("a lift shaft", rotation)
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
    layer: str = "A-STAIR",
    **params: Any,
) -> list[Any]:
    """A flight by its lower-left corner; `axis` is the direction it travels, as
    `draw_stair` takes it."""
    _no_rotation("a stair flight", rotation)
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
    layer: str = "A-SHAFT",
    **params: Any,
) -> list[Any]:
    """A service riser by its lower-left corner."""
    _no_rotation("a service shaft", rotation)
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
    layer: str = "A-EQUIP",
    **params: Any,
) -> list[Any]:
    """A piece of fixed equipment by its lower-left corner."""
    _no_rotation("a piece of equipment", rotation)
    x, y = geom.xy(at)
    return ops.draw_equipment(be, (x, y), (x + float(width), y + float(depth)), name, layer=layer)


def insert_captured(
    be,
    at: Point,
    *,
    rotation: float = 0.0,
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
    looks right.
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
    return [be.insert(block, at, rotation=rotation, layer=layer or "0")]


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
