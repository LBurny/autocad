"""The element catalogue: which library block stands for which element, here.

A factory plan is mostly the same door, the same window and the same steel section
repeated dozens of times. The catalogue is what lets an AI write ``place_element("door",
... eight times)`` instead of drawing eight doors out of lines.

It is deliberately a plain JSON file, for the same reason the plugin's journal is a
plain Xrecord: it has to be readable and editable by the person whose standard it is.
An institute's layer names and model numbers are their own, and a database would put
them somewhere they cannot open.

Two rules from the plugin's journal apply here as well:

* **Loading never raises.** A missing or broken entry is skipped and *counted*, and the
  count is reported. A catalogue that fails to load entirely would take the drawing with
  it, and one that silently drops entries would place the wrong window.
* **A catalogue that has drifted from the library is the failure to expect.** Entries
  name a block; the library defines blocks; nothing keeps those two in step. So
  :meth:`Catalog.check_against` compares them and reports every entry whose block the
  library has not got.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The catalogue that ships with the package.
PACKAGE_CATALOG = Path(__file__).with_name("data") / "elements.json"


@dataclass(frozen=True)
class Variant:
    """One size or type of an element: the parameters that select it."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "params": dict(self.params), "notes": self.notes}


@dataclass(frozen=True)
class Element:
    """A catalogue entry: one kind of thing that gets placed over and over."""

    key: str
    label: str
    category: str
    block: str
    source: str
    layer: str
    variants: dict[str, Variant] = field(default_factory=dict)
    tag_prefix: str = ""
    default_variant: str = ""
    #: Parameters every variant of this element carries, e.g. a wall thickness.
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def variant(self, name: str | None = None) -> Variant | None:
        """The named variant, or the default one, or None if there is no such variant.

        With no name and no declared default, the first variant listed is the default. An
        entry that offers sizes should place one of them: an unconfigured dynamic block can
        come out at whatever size the block's own author left it, which is not a decision
        the catalogue should be making by omission.
        """
        if name:
            return self.variants.get(name.lower())
        if self.default_variant:
            return self.variants.get(self.default_variant.lower())
        return next(iter(self.variants.values()), None)

    def settings(self, name: str | None = None, overrides: dict[str, Any] | None = None) -> dict:
        """The dynamic block parameters to set when placing this element.

        The element's own parameters first, then the variant's, then the caller's: the more
        specific wins, so a variant can correct a default and a caller can correct both.

        A variant name that does not exist raises rather than falling back. Placing an
        element at a size nobody asked for — because the name was a typo — is the same kind
        of failure as setting a dynamic property the block has not got, and the plugin
        refuses that one too.
        """
        params = dict(self.params)
        if name:
            chosen = self.variants.get(name.strip().lower())
            if chosen is None:
                raise ValueError(
                    f"'{self.key}' has no variant '{name}'; it has: "
                    + (", ".join(v.name for v in self.variants.values()) or "none")
                )
        else:
            chosen = self.variant()
        if chosen is not None:
            params.update(chosen.params)
        if overrides:
            params.update(overrides)
        return params

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "block": self.block,
            "source": self.source,
            "layer": self.layer,
            "tag_prefix": self.tag_prefix,
            "default_variant": self.default_variant,
            "params": dict(self.params),
            "variants": {name: v.describe() for name, v in self.variants.items()},
            "notes": self.notes,
        }


class Catalog:
    """The loaded catalogue, plus what was skipped getting there."""

    def __init__(
        self, elements: dict[str, Element] | None = None, skipped: list[str] | None = None
    ) -> None:
        self.elements: dict[str, Element] = dict(elements or {})
        #: One line per entry that could not be used, in the order they were read.
        self.skipped: list[str] = list(skipped or [])
        #: The library directory each file declared, for the report.
        self.libraries: dict[Path, str] = {}

    # --- loading -----------------------------------------------------------

    @classmethod
    def load(cls, paths: list[str | Path] | None = None) -> Catalog:
        """Read the package catalogue and then any project ones, later files winning."""
        catalog = cls()
        for path in [PACKAGE_CATALOG, *(paths or [])]:
            catalog.add_file(Path(path))
        return catalog

    def add_file(self, path: Path) -> None:
        """Merge one catalogue file in. A file that cannot be read is reported, not fatal."""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipped.append(f"{path}: no such catalogue file")
            return
        except (OSError, ValueError) as exc:
            self.skipped.append(f"{path}: {exc}")
            return

        entries = payload.get("elements") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            self.skipped.append(
                f"{path}: expected a list of elements, under 'elements' or at the top"
            )
            return

        library = ""
        if isinstance(payload, dict):
            library = os.environ.get("ACADMCP_LIBRARY") or str(payload.get("library", ""))
        if library:
            self.libraries[path] = library

        for index, entry in enumerate(entries):
            element = _read_element(
                entry, where=f"{path}[{index}]", skipped=self.skipped, library=library
            )
            if element is not None:
                self.elements[element.key] = element

    # --- lookup ------------------------------------------------------------

    def get(self, key: str) -> Element | None:
        return self.elements.get(key.strip().lower())

    def keys(self) -> list[str]:
        return sorted(self.elements)

    def by_category(self, category: str | None = None) -> dict[str, list[Element]]:
        """Elements grouped by category, so a caller can see what is on offer."""
        grouped: dict[str, list[Element]] = {}
        for element in self.elements.values():
            if category and element.category != category:
                continue
            grouped.setdefault(element.category, []).append(element)
        return {name: sorted(items, key=lambda e: e.key) for name, items in sorted(grouped.items())}

    def sources(self) -> list[str]:
        return sorted({e.source for e in self.elements.values() if e.source})

    # --- the check that matters -------------------------------------------

    def check_against(self, blocks: dict[str, list[str]]) -> list[str]:
        """Entries whose block is not in the library they name.

        `blocks` maps a library file to the names it defines, as ``library_list``
        reports it. An entry naming a source that was never read is reported too,
        because "we could not check this" is not the same as "this is fine".
        """
        problems: list[str] = []
        for key in self.keys():
            element = self.elements[key]
            if not element.source:
                continue
            if element.source not in blocks:
                problems.append(
                    f"{key}: '{element.source}' was not read, so its block is unchecked"
                )
            elif element.block not in blocks[element.source]:
                problems.append(f"{key}: '{element.source}' has no block '{element.block}'")
        return problems

    def describe(self) -> dict[str, Any]:
        return {
            "count": len(self.elements),
            "skipped": list(self.skipped),
            "categories": {
                name: [e.describe() for e in items] for name, items in self.by_category().items()
            },
        }


def _read_element(
    entry: Any, *, where: str, skipped: list[str], library: str = ""
) -> Element | None:
    """One entry, or None with the reason appended to `skipped`.

    Tolerant about what is absent and strict about what is wrong: a missing category is a
    default, a missing block is not, because an element with no block cannot be placed and
    the person reading the report needs to know which line to fix.
    """
    if not isinstance(entry, dict):
        skipped.append(f"{where}: not an object")
        return None

    key = str(entry.get("key", "")).strip().lower()
    block = str(entry.get("block", "")).strip()
    if not key:
        skipped.append(f"{where}: no key")
        return None
    if not block:
        skipped.append(f"{key}: no block to place")
        return None

    variants: dict[str, Variant] = {}
    raw_variants = entry.get("variants") or {}
    if isinstance(raw_variants, dict):
        for name, spec in raw_variants.items():
            if isinstance(spec, dict):
                variants[str(name).strip().lower()] = Variant(
                    name=str(name),
                    params=dict(spec.get("params") or {}),
                    notes=str(spec.get("notes", "")),
                )
            else:
                # A bare value is the one parameter the variant sets, which is the common
                # case: "900": 900 for a door width.
                variants[str(name).strip().lower()] = Variant(name=str(name), params={"size": spec})
    elif raw_variants:
        skipped.append(f"{key}: variants must be an object")

    return Element(
        key=key,
        label=str(entry.get("label", key)),
        category=str(entry.get("category", "other")),
        block=block,
        source=_resolve_source(str(entry.get("source", "")), library),
        layer=str(entry.get("layer", "0")),
        variants=variants,
        tag_prefix=str(entry.get("tag_prefix", "")),
        default_variant=str(entry.get("default_variant", "")).strip().lower(),
        params=dict(entry.get("params") or {}),
        notes=str(entry.get("notes", "")),
    )


def _resolve_source(source: str, library: str) -> str:
    """Fill in `{library}` in a source path.

    A catalogue names library files relative to one directory, because that directory is the
    one thing that differs between machines: the blocks themselves are the same file. Keeping
    the token rather than a stamped-in absolute path is what lets the same catalogue work for
    somebody whose samples live on another drive, by pointing `{library}` at it.
    """
    if "{library}" not in source:
        return source
    if not library:
        return source
    return source.replace("{library}", library.rstrip("\\/"))


# ---------------------------------------------------------------------------
# numbering and repetition
# ---------------------------------------------------------------------------


def next_tag(prefix: str, existing: list[str], separator: str = "-") -> str:
    """The next free number for a tag, derived from the ones the drawing already has.

    Derived rather than stored, and deliberately so: a counter kept beside the drawing is a
    second copy of a fact the drawing already holds, and the two drift the moment anyone
    edits by hand. A tag that cannot be parsed is ignored rather than treated as zero, so a
    hand-written "M-?" does not restart the numbering at 1.
    """
    if not prefix:
        return ""
    highest = 0
    head = f"{prefix}{separator}"
    for tag in existing:
        text = str(tag).strip().upper()
        if not text.startswith(head.upper()):
            continue
        digits = text[len(head) :]
        if digits.isdigit():
            highest = max(highest, int(digits))
    return f"{prefix}{separator}{highest + 1}"


def positions_along(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    count: int | None = None,
    spacing: float | None = None,
    inset: float = 0.0,
) -> list[tuple[float, float]]:
    """Points evenly spread along a run — a row of columns, a wall of windows.

    Either a count or a spacing, not both: `count` divides the run into that many equal
    gaps (first and last at the ends), `spacing` steps along it from the start so the last
    point is not past the end. `inset` pulls the first and last points in, which is what
    keeps a column off the corner of a wall.
    """
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 0:
        raise ValueError("the run has no length; give two different points")
    if count is not None and spacing is not None:
        raise ValueError("give either a count or a spacing, not both")
    inset = max(0.0, min(float(inset), length / 2.0))
    span = length - 2 * inset
    ux, uy = dx / length, dy / length
    origin = (x0 + ux * inset, y0 + uy * inset)

    if count is not None:
        count = int(count)
        if count < 1:
            raise ValueError("a count of at least 1 is needed")
        if count == 1:
            return [(x0 + ux * length / 2.0, y0 + uy * length / 2.0)]
        step = span / (count - 1)
        return [(origin[0] + ux * step * i, origin[1] + uy * step * i) for i in range(count)]

    if spacing is None:
        raise ValueError("give either a count or a spacing")
    if spacing <= 0:
        raise ValueError("a spacing of more than 0 is needed")
    points = []
    walked = 0.0
    # A hair of tolerance, so a run that divides exactly by the spacing still gets its
    # last point instead of dropping it to floating-point error.
    while walked <= span + 1e-9:
        points.append((origin[0] + ux * walked, origin[1] + uy * walked))
        walked += spacing
    return points


def positions_grid(xs: list[float], ys: list[float]) -> list[tuple[float, float]]:
    """Every combination of the two lists — an axis grid, which is what a frame is."""
    return [(float(x), float(y)) for y in ys for x in xs]


def current(paths: list[str | Path] | None = None) -> Catalog:
    """The catalogue for this process, loaded once.

    The project's own catalogue files are merged over the package's, so a project restates
    only what it disagrees with — its own door block, its own layers — and inherits the
    rest. Where the library lives comes from the same settings file.
    """
    global _current
    if _current is None:
        from . import settings

        project = settings.current()
        _current = Catalog.load(paths or project.catalogs)
    return _current


def reset() -> None:
    """Forget the loaded catalogue. For tests, and after a project file changes."""
    global _current
    _current = None


_current: Catalog | None = None
