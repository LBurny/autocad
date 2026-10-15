"""The drafting standard, as data: GB/T tables with sources, and lookups that refuse.

A layer list is half a standard. The other half is the numbers behind it — the line-width
groups a heavy wall and a dimension line are drawn with, the series of text heights a sheet
is written in, the scales it is plotted at, the sheet's frame and title block. Those live in
:mod:`acadmcp.layers` and :mod:`acadmcp.sheet`, which is where the drawing code reads them;
this module is what lets them be *checked*, replaced per project and reported on, because
none of them is this package's to decide. A factory's own standard is a file the draughtsman
can open, the same rule the catalogue follows.

Three rules, all of them the catalogue loader's and the plugin journal's before it:

* **Loading never raises.** A missing or broken file, a standard with no name, a row that is
  not an object — each is skipped and *counted* in :attr:`Standards.skipped`, never raised. A
  standard that failed to load would take the drawing with it.
* **Later files win, row by row.** The package's table is read first and a project's file
  merges over it, so a project restates only the rows it disagrees with and inherits the
  rest. A project and the package cannot disagree about the rows neither restated.
* **A lookup outside a transcribed table refuses by name.** ``b / 0.7b / 0.5b / 0.35b`` is
  not recalled here: a table nobody has transcribed ships empty, and asking it for a row
  says so and names what it does hold. Interpolating a neighbouring value would be a
  wrong-looking line that nothing catches — the discipline the element catalogue keeps for a
  named variant that does not exist, applied to a number.

Pure module: it imports :mod:`acadmcp.layers` and :mod:`acadmcp.sheet` and no CAD library,
and it reaches :mod:`acadmcp.settings` only lazily inside :func:`current` — the same shape
:func:`acadmcp.catalog.current` uses, and for the same reason.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import sheet

#: The standard that ships with the package.
PACKAGE_STANDARDS = Path(__file__).with_name("data") / "standards.json"

#: The standard in force when a project names none of its own. A project that wants its own
#: scheme overrides this standard's rows — naming the file it comes from is the whole
#: mechanism, so there is no second place to say which standard applies.
DEFAULT_STANDARD = "gbt50001"

#: AutoCAD's own line-weight list, read out of ``acdbmgd`` with the plugin's ApiProbe rather
#: than recalled: its ``LineWeight`` enum stops at 2.11 mm. A value is carried in 1/100 mm —
#: the unit a layer record and DXF group 370 both use — so this is the range of that unit,
#: not a GB/T table.
MIN_LINEWEIGHT_MM = 0.0
MAX_LINEWEIGHT_MM = 2.11


# ---------------------------------------------------------------------------
# one table set
# ---------------------------------------------------------------------------


class Standard:
    """One named standard: its tables, and what could not be read out of them."""

    def __init__(
        self,
        name: str,
        label: str = "",
        tables: dict[str, dict[str, Any]] | None = None,
        skipped: list[str] | None = None,
    ) -> None:
        self.name = name
        self.label = label or name
        self.tables: dict[str, dict[str, Any]] = {
            str(table): dict(rows) for table, rows in (tables or {}).items()
        }
        self.skipped: list[str] = list(skipped or [])

    # --- reading -----------------------------------------------------------

    def rows(self, table: str) -> dict[str, dict[str, Any]]:
        """The rows of a table, without its metadata. Empty for an absent or untranscribed one.

        Tolerant where :meth:`lookup` is strict, because reading a series — every text height,
        every layer — is how a checker walks a table, while asking for one row is a claim about
        what the standard says and has to refuse when it says nothing.
        """
        return {
            str(key): dict(value)
            for key, value in self.tables.get(table, {}).items()
            if not str(key).startswith("_") and isinstance(value, dict)
        }

    def lookup(self, table: str, key: str) -> dict[str, Any]:
        """One row, or ``ValueError`` naming what the table does hold.

        The refusal is the feature: a caller asking a standard what a 0.33 mm line is must be
        told the standard has no such row, not handed 0.35 because it is close. `coverage` is
        computed from the rows present rather than stored beside them, so it cannot go stale.
        """
        if table not in self.tables:
            raise ValueError(
                f"standard '{self.name}' has no table '{table}'; it has: "
                + (", ".join(sorted(self.tables)) or "none")
            )
        wanted = str(key).strip()
        rows = self.tables[table]
        for candidate in (wanted, wanted.lower(), wanted.upper()):
            row = rows.get(candidate)
            if isinstance(row, dict):
                return dict(row)
        if not self.rows(table):
            raise ValueError(
                f"{self.name}/{table}: not transcribed yet — refused by name rather than "
                "interpolated. Transcribe the rows into a project standards file (see the "
                "`standards` list in acadmcp.json) and they will be read"
            )
        raise ValueError(
            f"{self.name}/{table} has no row for '{key}'; coverage is {self.coverage(table)}"
        )

    def coverage(self, table: str) -> list[str]:
        """The keys a table can answer for, in a readable order.

        Numbers sort as numbers, so a refusal about a text height reads as the series it is
        rather than as a sort of its digits.
        """
        keys = list(self.rows(table))

        def order(value: str) -> tuple[int, float, str]:
            with contextlib.suppress(ValueError):
                return (0, float(value), "")
            return (1, 0.0, value)

        return sorted(keys, key=order)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "digest": self.digest(),
            "tables": {table: self.coverage(table) for table in sorted(self.tables)},
            "skipped": list(self.skipped),
        }

    def digest(self) -> str:
        """A hash of the tables, so a difference is detectable without a diff.

        Over the tables only — not the label, not the skip lines — so a project that restates
        one row changes the digest and a project that renames its file does not.
        """
        blob = json.dumps(self.tables, sort_keys=True, ensure_ascii=True, default=str)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # --- the tables the drawing code and the checker read ------------------

    @property
    def layers(self) -> dict[str, tuple[int, str, float | None]]:
        """The layer table as ``(colour, linetype, lineweight-mm or None)``.

        A layer name is upper-cased here: AutoCAD stores it that way, and a project file that
        spells it ``a-wall`` must override *that* layer rather than stand a second one beside
        it. A missing colour or linetype falls back to the same defaults
        ``acadmcp.settings`` uses, because a layer row is still drawable without them; a
        missing lineweight is None, which every driver reads as "leave it as it is".

        A row that cannot be read *raises*, naming itself, rather than being dropped: the
        person who typed ``"color": "red"`` into their standard needs the line, not a drawing
        one layer short. Rows that are not objects at all are counted in `skipped` while the
        file loads, which is the other half of the same rule.
        """
        table: dict[str, tuple[int, str, float | None]] = {}
        for name, row in self.rows("layers").items():
            try:
                weight = row.get("lineweight")
                table[name.strip().upper()] = (
                    int(row.get("color", 7)),
                    str(row.get("linetype", "CONTINUOUS")),
                    None if weight is None else float(weight),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.name}/layers/{name}: {exc}") from exc
        return table

    @property
    def text_heights(self) -> frozenset[float]:
        """The nominal series of paper text heights, in mm."""
        heights: set[float] = set()
        for key, row in self.rows("text_heights").items():
            try:
                heights.add(float(row.get("height", key)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.name}/text_heights/{key}: {exc}") from exc
        return frozenset(heights)

    @property
    def scales(self) -> dict[str, tuple[int, int]]:
        """The plot scales — :data:`acadmcp.sheet.SCALES` itself, not a copy of it.

        One list, one source. The standard's own ``scales`` table is metadata only, stating
        where the ratios live; returning this dict rather than restating the ratios is what
        keeps the two from drifting.
        """
        return sheet.SCALES

    @property
    def title_block(self) -> dict[str, Any] | None:
        """The title-block row, or None when this standard states none."""
        return self.rows("title_block").get("title_block")

    # --- merging -----------------------------------------------------------

    def merge_tables(self, tables: dict[str, Any], *, where: str, skipped: list[str]) -> None:
        """Merge a later file's tables in, row by row, later rows winning.

        Row-level rather than table-level, because that is what makes a project file short: a
        project that changes one layer writes one row and inherits the other twenty. An empty
        table in a later file leaves the earlier rows standing — a project says what its rows
        are, it does not get to erase the package's by omission. A table's metadata (``_source``,
        ``_note``, ``_see``) is carried through as ordinary entries in the same dict, so the
        file's own explanation travels with the rows it explains.
        """
        for table, rows in tables.items():
            if not isinstance(rows, dict):
                skipped.append(f"{where}: table '{table}' must be an object")
                continue
            target = self.tables.setdefault(str(table), {})
            for key, row in rows.items():
                if str(key).startswith("_"):
                    target[str(key)] = row
                    continue
                if not isinstance(row, dict):
                    skipped.append(f"{where}: {table}/{key} must be an object")
                    continue
                target[str(key)] = dict(row)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


class Standards:
    """The standards loaded for this process, plus what was skipped getting there."""

    def __init__(
        self,
        standards: dict[str, Standard] | list[Standard] | None = None,
        skipped: list[str] | None = None,
    ) -> None:
        if isinstance(standards, dict):
            self.standards: dict[str, Standard] = dict(standards)
        else:
            self.standards = {std.name: std for std in (standards or [])}
        #: One line per standard or row that could not be used, in the order they were read.
        self.skipped: list[str] = list(skipped or [])

    # --- loading -----------------------------------------------------------

    @classmethod
    def load(cls, paths: list[str | Path] | None = None) -> Standards:
        """Read the package standard and then any project ones, later files winning."""
        registry = cls()
        for path in [PACKAGE_STANDARDS, *(paths or [])]:
            registry.add_file(Path(path))
        return registry

    @classmethod
    def current(cls) -> Standards:
        """The registry in force for this process — the project's files over the package's."""
        return current()

    def add_file(self, path: Path) -> None:
        """Merge one standards file in. A file that cannot be read is reported, not fatal."""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipped.append(f"{path}: no such standards file")
            return
        except (OSError, ValueError) as exc:
            self.skipped.append(f"{path}: {exc}")
            return

        entries = payload.get("standards") if isinstance(payload, dict) else payload
        if isinstance(entries, dict):
            # Keyed by name rather than a list: the same fact, and a hand-written file is
            # likelier to be written this way.
            entries = [
                dict(spec, name=name) if isinstance(spec, dict) else spec
                for name, spec in entries.items()
            ]
        if not isinstance(entries, list):
            self.skipped.append(
                f"{path}: expected a list of standards, under 'standards' or at the top"
            )
            return

        for index, entry in enumerate(entries):
            self._read_standard(entry, where=f"{path}[{index}]")

    def _read_standard(self, entry: Any, *, where: str) -> None:
        """One standard out of a file, or a line saying why not."""
        if not isinstance(entry, dict):
            self.skipped.append(f"{where}: not an object")
            return
        name = str(entry.get("name", "")).strip().lower()
        if not name:
            self.skipped.append(f"{where}: no name")
            return

        tables = entry.get("tables")
        if not isinstance(tables, dict):
            # An author who wrote the tables at the top level, beside `name`, is saying the
            # same thing; every other key of theirs is metadata.
            tables = {
                str(key): value
                for key, value in entry.items()
                if not str(key).startswith("_")
                and key not in ("name", "label", "tables")
                and isinstance(value, dict)
            }
        if not tables:
            self.skipped.append(f"{where}: standard '{name}' has no tables")
            return

        existing = self.standards.get(name)
        if existing is None:
            existing = Standard(name=name, label=str(entry.get("label", name)))
            self.standards[name] = existing
        elif entry.get("label"):
            existing.label = str(entry["label"])
        existing.merge_tables(tables, where=where, skipped=self.skipped)

    # --- lookup ------------------------------------------------------------

    def get(self, name: str) -> Standard | None:
        return self.standards.get(str(name).strip().lower())

    def keys(self) -> list[str]:
        return sorted(self.standards)

    def __contains__(self, name: object) -> bool:
        return self.get(str(name)) is not None

    def __iter__(self) -> Iterator[str]:
        """The names, in the order `keys()` reports them."""
        return iter(self.keys())

    def in_force(self, name: str = "") -> Standard | None:
        """The standard a drawing is drawn under, or None when there is none.

        The named one when a caller names one — the template builder draws under the standard
        a template declares. Otherwise :data:`DEFAULT_STANDARD` if it is loaded, and the first
        standard read if it is not. None is a real answer and not an error: a process with no
        standard at all falls back to the built-in layer scheme, which is what this package
        drew before any of this was data.
        """
        wanted = str(name).strip().lower()
        if wanted:
            return self.standards.get(wanted)
        if DEFAULT_STANDARD in self.standards:
            return self.standards[DEFAULT_STANDARD]
        return next(iter(self.standards.values()), None)

    def describe(self) -> dict[str, Any]:
        return {
            "count": len(self.standards),
            "in_force": getattr(self.in_force(), "name", ""),
            "skipped": list(self.skipped),
            "standards": {name: self.standards[name].describe() for name in self.keys()},
        }


# ---------------------------------------------------------------------------
# the process-wide registry
# ---------------------------------------------------------------------------


def load(paths: list[str | Path] | None = None) -> Standards:
    """Read the standards named by a list of files, over the package's own."""
    return Standards.load(paths)


def current() -> Standards:
    """The standards for this process, loaded once.

    The files come from the project's ``acadmcp.json`` ``standards`` list, and their rows merge
    over the package's, so a project restates only what it disagrees with. `settings` is
    imported here rather than at module scope so this stays a pure module with no import cycle.
    """
    global _current
    if _current is None:
        from . import settings

        _current = Standards.load(settings.current().standards)
    return _current


def reset() -> None:
    """Forget the loaded standards. For tests, and after a project file changes."""
    global _current
    _current = None


def use(loaded: Standards | None) -> Standards | None:
    """Install `loaded` as the standards in force, and return it.

    None is the answer for "no standard" — a drawing whose caller wants the built-in layer
    scheme, which is what :func:`acadmcp.ops.setup_layers` falls back to. `reset()` restores
    the loaded chain.
    """
    global _current
    _current = loaded
    return loaded


def get(name: str) -> Standard | None:
    """One standard by name from the registry in force, or None."""
    return current().get(name)


def in_force(name: str = "") -> Standard | None:
    """The standard a drawing is drawn under, or None when there is none."""
    return current().in_force(name)


def lineweight_centimm(mm: float) -> int:
    """A line weight in millimetres, as the 1/100 mm AutoCAD and DXF count in.

    One conversion for all three drivers, so a 0.7 mm wall face on the `dxf` backend and the
    same wall in AutoCAD cannot come out different. Rounded rather than truncated: 0.35 mm is
    34.999999999999996 as an IEEE double, and ``int()`` would call that 34.
    """
    value = float(mm)
    if not MIN_LINEWEIGHT_MM <= value <= MAX_LINEWEIGHT_MM:
        raise ValueError(
            f"a lineweight of {mm} mm is outside AutoCAD's own line-weight list "
            f"({MIN_LINEWEIGHT_MM} to {MAX_LINEWEIGHT_MM} mm); give millimetres, not "
            "hundredths of a millimetre"
        )
    return int(round(value * 100.0))


def describe() -> dict[str, Any]:
    """The registry in force, for a report."""
    return current().describe()


_current: Standards | None = None

__all__ = [
    "DEFAULT_STANDARD",
    "MAX_LINEWEIGHT_MM",
    "MIN_LINEWEIGHT_MM",
    "PACKAGE_STANDARDS",
    "Standard",
    "Standards",
    "current",
    "describe",
    "get",
    "in_force",
    "lineweight_centimm",
    "load",
    "reset",
    "use",
]
