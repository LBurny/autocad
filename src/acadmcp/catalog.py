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

import hashlib
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
    #: The same variant in the parametric twin's vocabulary, for an entry that also names a
    #: library block. None means "not stated" and is refused where it would matter; {} means
    #: "the twin needs nothing of its own", which is a deliberate answer.
    op_params: dict[str, Any] | None = None
    notes: str = ""

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "params": dict(self.params), "notes": self.notes}
        if self.op_params is not None:
            out["op_params"] = dict(self.op_params)
        return out


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
    #: The name of a parametric builder in :mod:`acadmcp.catalog_ops` — the twin this
    #: element is drawn by where a library block cannot be inserted. An entry may carry
    #: both, and then which one is used is the backend's capability's to decide.
    op: str = ""
    #: Which parameter carries a *measured* width — a wall opening's width, as opposed to a
    #: nominal size. Only the catalogue knows that one block calls it 门尺寸 and another
    #: calls it Wall. An `op` entry need not state it: the adapters all take `width`.
    width_param: str = ""
    #: Parameters every variant of the twin carries, e.g. the wall thickness a symbol is
    #: drawn against. Only meaningful for an entry that names a library block as well: an
    #: op-only entry's `params` are already the twin's vocabulary.
    op_params: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """How this entry reaches the paper: a library block, an op, or either."""
        if self.block and self.op:
            return "both"
        return "block" if self.block else "op"

    @property
    def placement(self) -> str:
        """The same fact as `kind`, in the words a caller reads.

        `kind` describes the entry to the code, where an op and a block are two different
        mechanisms; `placement` describes it to whoever is choosing an element, where "it
        is drawn parametrically" is the useful thing to know. One field for the tool
        surface, so a client does not have to be told that "op" means parametric.
        """
        return {"block": "block", "op": "parametric", "both": "both"}[self.kind]

    @property
    def width_key(self) -> str:
        """The parameter a measured width goes under, or "" when there is none.

        An op adapter is written to take `width`; a library block calls it whatever its
        author called it. Defaulting the op case means a catalogue only has to state
        `width_param` for an entry that names a block — and an entry that names a block and
        does not state one will be refused a width rather than putting it somewhere
        arbitrary.
        """
        return self.width_param or ("width" if self.op else "")

    def placement_for(self, can_insert: bool) -> str:
        """Which half of this entry a backend should draw: ``"block"`` or ``"op"``.

        A backend that can insert takes the library block, and that is the point of the
        catalogue: a block carries the model number a schedule counts, and a colleague's
        AutoCAD can explode and measure it. A backend that cannot — the `dxf` one has no
        library to read, the `com` one refuses — falls back to the parametric twin, so one
        element name draws on every backend and a plan can be written headless and replayed
        into AutoCAD.

        Decided here rather than probed by trying `insert`: inside a step the `dxf` and
        `com` refusals are deferred to the commit, so a caller cannot find out by trying.
        """
        if self.block and can_insert:
            return "block"
        if self.op:
            return "op"
        if self.block:
            raise ValueError(
                f"'{self.key}' names a library block and no parametric twin ('op'), so it "
                "can only be placed where library blocks can be inserted — connect with "
                "acad_connect(mode='plugin'), or add an 'op' to its catalogue entry"
            )
        raise ValueError(f"'{self.key}' has neither a block nor an op to place")

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

    def twin_settings(
        self,
        name: str | None = None,
        overrides: dict[str, Any] | None = None,
        width: float | None = None,
    ) -> dict[str, Any]:
        """The parameters the parametric twin is called with.

        Same precedence as :meth:`settings` — the element's own, then the variant's, then the
        caller's — but in the twin's vocabulary rather than the block's, because the two are
        not the same words: the door block calls its width 门尺寸 while `door_symbol` takes
        `width`. An op-only entry needs no second vocabulary, so its `params` are used
        directly.

        A variant that states nothing for the twin is **refused**, not defaulted, where it
        has stated something for the block: `place_element("door", variant="900")` reaching
        `door_symbol` as its own default of 900 would be a right-looking door of the wrong
        size for a catalogue that meant a 1100 one. `"op_params": {}` is how an author says
        the twin deliberately needs nothing.
        """
        from_block = bool(self.block)
        params = dict(self.op_params) if from_block else dict(self.params)

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
            if from_block and chosen.op_params is None and chosen.params:
                raise ValueError(
                    f"'{self.key}' variant '{chosen.name}' cannot be placed parametrically: "
                    f"its parameters ({', '.join(sorted(chosen.params))}) are the block's and "
                    "the twin does not understand them; state the twin's own under "
                    '"op_params" (use {} if it needs none)'
                )
            params.update((chosen.op_params or {}) if from_block else chosen.params)

        if overrides:
            params.update(overrides)
        if width is not None:
            params["width"] = float(width)
        return params

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "block": self.block,
            "op": self.op,
            "kind": self.kind,
            "placement": self.placement,
            "width_param": self.width_param,
            "op_params": dict(self.op_params),
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

    @classmethod
    def adopt(cls, payload: dict[str, Any] | None) -> Catalog:
        """Rebuild a catalogue from an identity embedded in a drawing.

        Tolerant, for the same reason loading is: a drawing whose embedded catalogue is
        damaged must still open, and what could not be read is counted in `skipped` and
        reported rather than raised. Entries come flat, under `elements`, and a payload
        grouped under `categories` (the report's own shape) is accepted too, so a reader
        never has to know which of the two it was handed.
        """
        catalog = cls()
        if not isinstance(payload, dict):
            return catalog
        entries = payload.get("elements")
        if not isinstance(entries, list):
            entries = [
                item
                for items in (payload.get("categories") or {}).values()
                if isinstance(items, list)
                for item in items
            ]
        catalog.add_entries(entries, library=str(payload.get("library", "")), where="<drawing>")
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

        self.add_entries(entries, library=library, where=str(path))

    def add_entries(self, entries: list[Any], library: str = "", where: str = "") -> None:
        """Read a list of raw entries through the one reader, later entries winning.

        Shared by :meth:`add_file` and :meth:`adopt` deliberately: an entry adopted from a
        drawing and an entry read from a project file must not be able to mean different
        things, and a second reader would eventually drift from the first.
        """
        for index, entry in enumerate(entries):
            element = _read_element(
                entry, where=f"{where}[{index}]", skipped=self.skipped, library=library
            )
            if element is not None:
                self.elements[element.key] = element

    def register(self, payload: dict[str, Any]) -> Element:
        """Add one entry at runtime — a captured element — strictly, or not at all.

        Unlike loading, this **raises**. A capture is a direct caller action with a caller
        waiting for the answer, so a broken payload has to name its own fault instead of being
        counted in `skipped` and forgotten; a capture that appeared to work and added nothing
        is the failure this is meant to prevent. The per-entry reader is the loader's own, so a
        registered entry cannot come to mean something a file entry does not.

        A duplicate key is refused rather than replaced: the entry may be what a drawing was
        drawn with, and overwriting it silently would redraw somebody's elements with whatever
        was captured last.
        """
        if not isinstance(payload, dict):
            raise ValueError("a catalogue entry must be an object")
        reason: list[str] = []
        element = _read_element(payload, where="<capture>", skipped=reason, library="")
        if element is None:
            raise ValueError(
                "cannot register the captured element: " + "; ".join(reason or ["unreadable"])
            )
        if element.key in self.elements:
            raise ValueError(
                f"the catalogue already has an entry '{element.key}'; a capture adds a new "
                "element rather than replacing one — capture under another key"
            )
        self.elements[element.key] = element
        return element

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
            # Nothing in a library stands for an entry that is drawn parametrically, so
            # there is nothing to compare and it is not "unchecked" — it is not a library
            # element at all. An entry with both halves still gets its block checked.
            if not element.source or not element.block:
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

    # --- what a drawing remembers ------------------------------------------

    def identity(self, library: str = "") -> dict[str, Any]:
        """The catalogue as the drawing will remember it.

        The whole thing, not a reference to it: a catalogue is a few kilobytes, and a drawing
        that records only a file path resolves to something else the moment it is opened on
        another machine — which is the failure this exists to prevent. `elements` is each
        entry's own ``describe()``, so :meth:`adopt` rebuilds through the same reader that
        built it and an adopted entry cannot mean something a file entry does not.
        """
        return {
            "library": library
            or (next(iter(self.libraries.values()), "") if self.libraries else ""),
            "count": len(self.elements),
            "skipped": list(self.skipped),
            "elements": [self.elements[key].describe() for key in self.keys()],
            "digest": self.digest(),
        }

    def digest(self) -> str:
        """A hash of the entries, so a difference is detectable without a diff.

        Over `describe()`, sorted and ASCII-escaped, so it is stable across machines and
        across a JSON round trip — the same catalogue embedded in a drawing and loaded from
        a file must hash the same, or every reopen reports a spurious mismatch.
        """
        rows = [self.elements[key].describe() for key in self.keys()]
        blob = json.dumps(rows, sort_keys=True, ensure_ascii=True, default=str)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_element(
    entry: Any, *, where: str, skipped: list[str], library: str = ""
) -> Element | None:
    """One entry, or None with the reason appended to `skipped`.

    Tolerant about what is absent and strict about what is wrong: a missing category is a
    default, while an entry with neither a block nor an `op` cannot be placed by anything,
    and the person reading the report needs to know which line to fix.
    """
    if not isinstance(entry, dict):
        skipped.append(f"{where}: not an object")
        return None

    key = str(entry.get("key", "")).strip().lower()
    block = str(entry.get("block", "")).strip()
    op = str(entry.get("op", "")).strip().lower()
    if not key:
        skipped.append(f"{where}: no key")
        return None
    if not block and not op:
        # An entry with neither half cannot be drawn at all, and the person reading the
        # report needs to know which line to fix.
        skipped.append(f"{key}: neither a block nor an op to place")
        return None

    variants: dict[str, Variant] = {}
    raw_variants = entry.get("variants") or {}
    if isinstance(raw_variants, dict):
        for name, spec in raw_variants.items():
            if isinstance(spec, dict):
                # `op_params` absent and `op_params: {}` are different answers: the first
                # says nothing about the twin, the second says it needs nothing. `get`
                # with an explicit default is what tells them apart.
                op_params = spec.get("op_params", None)
                variants[str(name).strip().lower()] = Variant(
                    name=str(name),
                    params=dict(spec.get("params") or {}),
                    op_params=None if op_params is None else dict(op_params or {}),
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
        op=op,
        width_param=str(entry.get("width_param", "")).strip(),
        op_params=dict(entry.get("op_params") or {}),
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


def use(loaded: Catalog) -> Catalog:
    """Install `loaded` as the current catalogue for this process, and return it.

    Needed when a drawing's *embedded* catalogue is the one in force: the project has no
    catalogue files of its own, so the copy the drawing carries is better than the
    package's starter and must become what `place_element` resolves names against. The
    local standard still wins wherever a project supplies one — this is only reached when
    it does not. `reset()` puts the loaded-chain back.
    """
    global _current
    _current = loaded
    return loaded


_current: Catalog | None = None
