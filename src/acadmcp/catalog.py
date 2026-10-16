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
    #: Where this entry came from, for a captured one: source drawing, journal steps the
    #: geometry was gathered from, when it was captured, a geometry fingerprint and — once
    #: published — the fragment file. `{}` for an entry written by hand, so "no provenance"
    #: and "provenance that could not be read" are never confused. It rides `describe()` so a
    #: drawing carries it, but it is excluded from `digest()`: provenance is not placement
    #: semantics, and a stamped entry must hash like its file twin.
    origin: dict[str, Any] = field(default_factory=dict)

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
            "origin": dict(self.origin),
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
        #: Which file each entry was read from, so a published entry's `geometry_file` — a path
        #: relative to the catalogue file it lives in — can be resolved. An entry adopted from a
        #: drawing, or registered at capture time, has no file context and no row here.
        self._files: dict[str, Path] = {}

    # --- loading -----------------------------------------------------------

    @classmethod
    def load(cls, paths: list[str | Path] | None = None) -> Catalog:
        """Read the package catalogue and then any project ones, later files winning."""
        catalog = cls()
        for path in [PACKAGE_CATALOG, *(paths or [])]:
            catalog.add_file(Path(path))
        return catalog

    @classmethod
    def adopt(cls, payload: dict[str, Any] | None, origin_file: str | Path | None = None) -> Catalog:
        """Rebuild a catalogue from an identity embedded in a drawing.

        Tolerant, for the same reason loading is: a drawing whose embedded catalogue is
        damaged must still open, and what could not be read is counted in `skipped` and
        reported rather than raised. Entries come flat, under `elements`, and a payload
        grouped under `categories` (the report's own shape) is accepted too, so a reader
        never has to know which of the two it was handed.

        The rebuilt entries are also merged into the catalogue **in force**: a drawing carries
        the elements it was drawn with, and one captured in it but not yet published is
        otherwise unreachable the moment a project has a catalogue of its own — the project's
        files win the precedence, the drawing's own entry is dropped, and its block definition
        stays in the file, so the element can be neither placed nor captured again under that
        name. A key the catalogue in force already has is left as its own, so the local
        standard still wins and there is one entry per key. This runs before the caller reads
        the catalogue in force (`drawing_open` reaches it through the backend's
        `embedded_catalog`), which is what makes the merge visible to that read.
        """
        catalog = cls()
        if isinstance(payload, dict):
            entries = payload.get("elements")
            if not isinstance(entries, list):
                entries = [
                    item
                    for items in (payload.get("categories") or {}).values()
                    if isinstance(items, list)
                    for item in items
                ]
            catalog.add_entries(entries, library=str(payload.get("library", "")), where="<drawing>")
            if origin_file is not None:
                # The drawing stands in as the catalogue file for what it carries: publish
                # writes the fragment under `library/` beside the catalogue file, so an
                # adopted entry's `geometry_file` resolves against the drawing's own
                # directory. Where the drawing does not sit beside the catalogue, the
                # resolution fails honestly ("is not there; republish"), as before.
                for adopted_key in catalog.elements:
                    catalog._files[adopted_key] = Path(origin_file)
            current().adopt_missing(catalog)
        return catalog

    def add_file(self, path: Path) -> None:
        """Merge one catalogue file in. A file that cannot be read is reported, not fatal."""
        source_file = Path(path)
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

        self.add_entries(entries, library=library, where=str(path), source_file=source_file)

    def add_entries(
        self,
        entries: list[Any],
        library: str = "",
        where: str = "",
        source_file: Path | None = None,
    ) -> None:
        """Read a list of raw entries through the one reader, later entries winning.

        Shared by :meth:`add_file` and :meth:`adopt` deliberately: an entry adopted from a
        drawing and an entry read from a project file must not be able to mean different
        things, and a second reader would eventually drift from the first. `source_file` is
        the catalogue file an entry was read from, recorded so a published entry's fragment
        path — relative to that file — can be resolved; it is None for an adopted or
        registered entry, which has no such context.
        """
        for index, entry in enumerate(entries):
            element = _read_element(
                entry, where=f"{where}[{index}]", skipped=self.skipped, library=library
            )
            if element is not None:
                self.elements[element.key] = element
                if source_file is not None:
                    self._files[element.key] = source_file

    def adopt_missing(self, other: Catalog) -> list[str]:
        """Take on the entries `other` carries that this catalogue has not got.

        One entry per key: a key both catalogues have is left as this one's, because the
        catalogue in force is the local standard and quietly redrawing with a drawing's copy
        of it is the failure the precedence rule exists to prevent. A collided key is returned
        so the caller can report it — the entries in force also differ, so the drawing's
        recorded digest disagrees with this catalogue's and the mismatch is reported there
        too, which is the same warning a differing catalogue has always produced.

        An adopted entry's resolution base travels with it: `adopt(origin_file=...)` records
        the drawing's own path as the file behind each entry, so a `geometry_file` resolves
        beside the drawing. Entries adopted without an origin file keep the old answer —
        `source_file` says None rather than guessing a path.
        """
        collisions = [key for key in other.elements if key in self.elements]
        for key, element in other.elements.items():
            if key not in self.elements:
                self.elements[key] = element
                if key in other._files:
                    self._files[key] = other._files[key]
        return collisions

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

    def source_file(self, key: str) -> Path | None:
        """The file an entry resolves its `geometry_file` against, or None.

        The catalogue file an entry was read from, or — for one adopted from a drawing's
        embedded copy — the drawing itself, which stands in as the catalogue file (publish
        writes `library/` beside the catalogue, so a drawing saved beside it resolves).
        None only for a runtime-captured entry: no file of any kind stands behind it, and
        the caller is told by name rather than the path being guessed.
        """
        return self._files.get(str(key or "").strip().lower())

    def fragment_ready(self, element: Element) -> bool | None:
        """Whether a captured element's geometry fragment resolves to a file on disk.

        None for an entry with no fragment to resolve — anything whose `op` is not
        `insert_captured` places from a library block or a parametric twin, so "ready" is
        not a question it asks. For a captured entry the fragment is what a *fresh* drawing
        imports, so False means: the drawing that captured it can still place it, and any
        other needs element_publish first. False also when the fragment path cannot be
        resolved at all — a runtime capture has no catalogue file behind it, and an adopted
        entry whose drawing sits away from its catalogue resolves to nothing that exists.
        """
        if element.op != "insert_captured":
            return None
        geometry_file = str(element.origin.get("geometry_file", "")).strip()
        if not geometry_file:
            return False
        base = self.source_file(element.key)
        if base is None:
            return False
        return (Path(base).parent / geometry_file).is_file()

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

        `origin` is deliberately excluded: **provenance is not placement semantics**. A
        capture stamps where its geometry came from, so a stamped entry and its file twin
        would otherwise hash differently — and every drawing drawn before the field existed
        would report a `catalog_warning` the moment it was reopened.
        """
        rows = []
        for key in self.keys():
            row = self.elements[key].describe()
            row.pop("origin", None)
            rows.append(row)
        blob = json.dumps(rows, sort_keys=True, ensure_ascii=True, default=str)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # --- publishing into the project library -------------------------------

    def publish_to(
        self,
        path: str | Path | None,
        element: Element,
        fragment: dict[str, Any] | None,
        *,
        capture_undone: bool = False,
    ) -> dict[str, Any]:
        """Write `element` and its geometry fragment into the project catalogue at `path`.

        Publishing is the one step that makes an element *accumulate*: a capture is
        drawing-local, but a capture **plus its geometry** written into a project file is
        something a fresh drawing can place. So two things are written — the catalogue entry,
        and a headless DXF under ``<catalogue dir>/library/<key>.dxf`` holding the block
        definition and, in its journal meta, the element's own geometry.

        `fragment` is that geometry, in the journal's vocabulary — ``{"block", "primitives",
        "base_point"}`` from `Backend.find_block_primitives`, plus the capture's
        ``fingerprint``. Passing ``None`` (or a fragment with no geometry) is how a publish
        from a drawing that never captured the definition is refused: the geometry lives in
        the drawing that captured it, and nothing here re-derives primitives from entities.

        `path` resolves in order: an explicit path; else the first project catalogue file in
        settings; else ``./elements.json`` is created beside a ``./acadmcp.json`` naming it,
        and what was created is reported. The target file is **appended to, never
        overwritten or re-sorted**, its ``_comment`` and any other key preserved, and the
        write is atomic (tmp + `os.replace`).

        `capture_undone` is the caller's answer to "does this drawing hold the capture of
        `element`, but undone". The finder reads applied steps only, so an undone capture and
        a definition this drawing never had both arrive here as no fragment — and they have
        different recoveries, so the refusal has to say which. Left at its default it says
        nothing about an undo, because this module cannot see the drawing.

        Returns ``{ok, file, fragment, created, key, digest}``. Refuses, by name: a key not
        in the catalogue in force, a key the target file already has, and a missing
        definition record.
        """
        key = str(getattr(element, "key", "") or "").strip().lower()
        if not key:
            raise ValueError("publishing needs an element with a key to name the entry")
        if self.get(key) is None:
            raise ValueError(
                f"'{key}' is not in the catalogue in force, so there is nothing to publish; "
                "capture it first with element_capture"
            )

        target, created = _resolve_publish_target(path)
        payload = _read_target_payload(target)
        if _entry_index(payload, key) is not None:
            raise ValueError(
                f"the catalogue file {str(target)!r} already has an entry '{key}'; publishing "
                "adds a new element rather than replacing one — choose another key or file"
            )

        block = str((fragment or {}).get("block", "")).strip()
        primitives = list((fragment or {}).get("primitives") or [])
        base_point = list((fragment or {}).get("base_point") or [0.0, 0.0])
        if not block or not primitives:
            if capture_undone:
                raise ValueError(
                    f"the capture step that made '{key}' is undone, so this drawing no longer "
                    "holds the geometry to publish — drawing_redo restores that step, or "
                    "capture the element again"
                )
            raise ValueError(
                f"the definition for '{key}' lives in the drawing that captured it — open that "
                "drawing and publish from there"
            )

        fingerprint = str((fragment or {}).get("fingerprint", "")).strip()
        if not fingerprint:
            # No fingerprint stamped: derive one from the geometry being published, so the
            # entry that lands still carries a usable identity.
            from . import capture_ops

            fingerprint = capture_ops.fingerprint_primitives(primitives)

        fragment_path = Path(target).parent / "library" / f"{key}.dxf"
        from . import capture_ops

        capture_ops.export_fragment(
            str(fragment_path),
            block,
            primitives,
            [float(base_point[0]), float(base_point[1])],
            {"key": key, "fingerprint": fingerprint},
        )
        # Relative to the catalogue file, and forward-slashed, so the entry as committed is
        # the same wherever the project is checked out.
        relative = os.path.relpath(str(fragment_path), str(Path(target).parent))
        relative = relative.replace(os.sep, "/")

        entry = element.describe()
        origin = dict(entry.get("origin") or {})
        origin["geometry_file"] = relative
        origin.setdefault("fingerprint", fingerprint)
        entry["origin"] = origin
        payload["elements"].append(entry)
        _atomic_write_json(Path(target), payload)

        # The project's catalogue files now differ from what was loaded, so the settings cache
        # is dropped and a later load walks the tree afresh. This object, though, is the
        # session's catalogue and it holds captures no file has: resetting on its own would
        # leave `catalog.current()` reloading from files alone, losing every runtime capture —
        # and the entry just published, whenever the target is not in the search path — while
        # the tool still reported ok. So it is put back as the one in force, with the file's own
        # copy of this entry in place of its runtime twin: one key, and the copy that resolves
        # on the next machine.
        from . import settings as _settings

        _settings.reset()
        use(self)

        written = Catalog()
        written.add_file(Path(target))
        published = written.get(key)
        if published is not None:
            self.elements[key] = published
            source_file = written.source_file(key)
            if source_file is not None:
                self._files[key] = source_file
        return {
            "ok": True,
            "file": str(target),
            "fragment": str(fragment_path),
            "created": created,
            "key": key,
            "digest": written.digest(),
        }


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
        # Absent and empty both read as {}: an entry written by hand has no provenance, and a
        # reader must never have to tell "no origin" from "an origin that went missing".
        origin=dict(entry.get("origin") or {}),
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


#: What a freshly created project catalogue says about itself. It is written into the file so
#: the person who finds `elements.json` beside their drawing knows what put it there.
ELEMENTS_COMMENT = (
    "Project element catalogue. Entries written by element_publish live here; each one's "
    "geometry is under library/. Edit by hand and it still loads."
)


def _resolve_publish_target(path: str | Path | None) -> tuple[Path, list[str]]:
    """Where a publish goes, and what it had to create to get there.

    An explicit path wins; then the first project catalogue file in settings; and only when
    neither exists are `./elements.json` and `./acadmcp.json` created in the working
    directory — reported, so a file appearing in someone's project is never a surprise.
    """
    if str(path or "").strip():
        return Path(path), []

    from . import settings as _settings

    project = _settings.current()
    if project.catalogs:
        return Path(project.catalogs[0]), []

    cwd = Path.cwd()
    created: list[str] = []
    elements = cwd / "elements.json"
    if not elements.is_file():
        _atomic_write_json(elements, {"_comment": ELEMENTS_COMMENT, "elements": []})
        created.append(str(elements))
    settings_file = cwd / _settings.FILENAME
    if not settings_file.is_file():
        _atomic_write_json(settings_file, {"catalogs": ["elements.json"]})
        created.append(str(settings_file))
    return elements, created


def _read_target_payload(target: Path) -> dict[str, Any]:
    """The target file's JSON, ready to append to, or a fresh payload when it is not there.

    Strict where the loader is tolerant: a publish is a direct caller action with a caller
    waiting, so a target file that cannot be read is refused by name rather than silently
    replaced — overwriting somebody's catalogue is exactly what ADD-only exists to prevent.
    """
    if not Path(target).is_file():
        return {"_comment": ELEMENTS_COMMENT, "elements": []}
    try:
        raw = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot publish into {str(target)!r}: {exc}") from exc
    if isinstance(raw, list):
        return {"elements": raw}
    if isinstance(raw, dict):
        if not isinstance(raw.get("elements"), list):
            raise ValueError(
                f"cannot publish into {str(target)!r}: expected a list of elements under 'elements'"
            )
        return raw
    raise ValueError(f"cannot publish into {str(target)!r}: expected an object or a list")


def _entry_index(payload: dict[str, Any], key: str) -> int | None:
    """The position of `key` among the payload's entries, or None."""
    for index, entry in enumerate(payload.get("elements") or []):
        if isinstance(entry, dict) and str(entry.get("key", "")).strip().lower() == key:
            return index
    return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write `payload` in one move: a temp file in the same directory, then `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
