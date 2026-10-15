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

* **The checker reports, it never raises.** :func:`check` reads a *snapshot* of the drawing —
  the unit, the layers in use, every text with its height, the sheet — as plain data, and
  returns findings with a severity, the shape :mod:`acadmcp.critique` established. Five rules
  this wave:

  * ``units_not_mm`` (error) — the drawing's unit is not the millimetre this package draws in.
  * ``layer_not_in_standard`` (warning) — an entity sits on a layer the standard's table does
    not name. A warning, because a project's own layers are legitimate and the finding is that
    the standard does not know the name, which is what a reviewer acts on. This wave checks
    **which layer names are in use**, not the layer *table's* own colour, linetype or
    lineweight: reading a layer definition needs a ``layer_table()`` read the plugin's RPC does
    not yet have, and reconstructing it from entity rows would be a check of something else
    wearing this rule's name. Deferred and named, never faked.
  * ``text_height_off_series`` (error) — a text whose *paper* height (its model height times
    the plot scale) is not in the standard's nominal series. This is the rule AutoCAD's own
    CHECKSTANDARDS runs, and the reason the series is data. The nearest nominal height is named
    in the finding, because a caller wants the number to move to. Text on the sheet's own
    layers is already at 1:1 in paper millimetres, so it is measured as paper.
  * ``no_sheet`` (warning) — the drawing carries no sheet at all.
  * ``sheet_without_scale`` (error) — a sheet whose title block states no 比例. A plotted sheet
    without its scale fails the sheet's own point (GB/T 10609.1).

  A snapshot with no sheet makes the text-height rule assume :data:`DEFAULT_SCALE` and *say so*
  in the finding, because a paper height cannot be judged without a denominator and a silent
  assumption would be a verdict about a scale nobody stated. A standard that transcribes no
  text-height series silences that rule rather than flagging every text, the same "refuse by
  name rather than interpolate" discipline applied to a check that cannot judge.

Pure module: it imports :mod:`acadmcp.layers` and :mod:`acadmcp.sheet` and no CAD library,
and it reaches :mod:`acadmcp.settings` only lazily inside :func:`current` — the same shape
:func:`acadmcp.catalog.current` uses, and for the same reason.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
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


# ---------------------------------------------------------------------------
# the conformance checker
# ---------------------------------------------------------------------------
#
# `check` is pure over a snapshot the calling tool gathers, so it needs no CAD import and no
# backend: the same discipline `critique.py` keeps, and the reason a drawing can be checked on
# every driver and after a reopen. It reports, it never raises — a verdict a caller acts on is
# worth more than a crash, so every unreadable row is skipped rather than guessed at.

#: The rule codes `check` reports. Stable strings: a caller focuses on one and the tool's reply
#: quotes them, so they are named here rather than typed at each use.
RULE_UNITS = "units_not_mm"
RULE_LAYER = "layer_not_in_standard"
RULE_TEXT_HEIGHT = "text_height_off_series"
RULE_NO_SHEET = "no_sheet"
RULE_SHEET_SCALE = "sheet_without_scale"

#: Every rule code, in the order :func:`check` reports them (units, layers, text heights, sheet).
RULES = (RULE_UNITS, RULE_LAYER, RULE_TEXT_HEIGHT, RULE_NO_SHEET, RULE_SHEET_SCALE)

#: The scale assumed to read a model text height when a drawing carries no sheet. GB/T 50001's
#: commonest architectural plan scale and this package's own ``drawing_sheet`` default. Assumed
#: *and reported*, never assumed silently: a paper height cannot be judged without a denominator,
#: and a finding must not attribute a scale the drawing never stated.
DEFAULT_SCALE = "1:100"

#: How close a text's paper height must be to a nominal one to count as it, in millimetres. Tiny
#: on purpose: a model height divided by an integer scale reproduces the nominal value's own IEEE
#: double, so anything looser than arithmetic noise would forgive a real difference — a 2.6 mm
#: text is an error, and 2.6 is only 0.1 mm from 2.5.
HEIGHT_TOLERANCE = 1e-6

#: Layers whose text is drawn on the sheet at 1:1 in paper millimetres. The frame and the title
#: block are paper content — a sheet's frame is drawn at 1:1 in paper mm — so a title-block
#: field's height is *already* a paper height, and dividing it by the plot scale would read the
#: standard's own 5.0 mm field as 0.05 mm and flag every sheeted drawing with an error that is
#: not one. A snapshot may also mark a text ``"paper": true`` for text this list does not know.
SHEET_LAYERS = frozenset({"A-SHEET", "A-SHEET-TEXT"})

#: Where a title block's fields sit in a snapshot, in the order tried. A caller that threads the
#: fields through names the group one of these; absent, the sheet's own ``scale`` is taken as the
#: evidence that 比例 was stated, which is how ``be.info()["sheet"]`` reports a sheet.
_TITLE_FIELDS_KEYS = ("title_fields", "title_block", "fields")

#: The field names that state the scale. 比例 first, because that is what the sheet writes.
_SCALE_FIELD_KEYS = ("比例", "scale", "SCALE")


def _number(value: Any) -> float | None:
    """A finite number, or None. A missing value and a malformed or non-finite one read alike."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finding(
    rule: str, severity: str, detail: dict[str, Any], entities: list[Any] | None = None
) -> dict[str, Any]:
    """One issue, in the shape the tool reports: ``rule``, ``severity``, ``detail``, ``entities``.

    ``entities`` names what the finding is about in the snapshot's own vocabulary — a layer name,
    a text's content — because a snapshot is a plain-data projection with no handles in it; a
    finding about the whole drawing (the unit, the sheet) carries none.
    """
    return {
        "rule": rule,
        "severity": severity,
        "detail": detail,
        "entities": list(entities or []),
    }


def _scale(snapshot: dict[str, Any]) -> tuple[int, int, bool]:
    """``(numerator, denominator, assumed)`` for the snapshot's sheet.

    Read through :func:`acadmcp.sheet.resolve_scale`, the one parser that also wrote the ratio, so
    a ratio this accepts is a ratio ``drawing_sheet`` accepts. A sheet with no readable scale falls
    back to :data:`DEFAULT_SCALE` and says so, rather than refusing to check heights at all.
    """
    block = snapshot.get("sheet")
    label = str(block.get("scale") or "").strip() if isinstance(block, dict) else ""
    if label:
        with contextlib.suppress(ValueError):
            numerator, denominator = sheet.resolve_scale(label)
            return int(numerator), int(denominator), False
    numerator, denominator = sheet.resolve_scale(DEFAULT_SCALE)
    return int(numerator), int(denominator), True


def _check_units(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule 1. Case- and padding-insensitive, because AutoCAD reports INSUNITS as ``"mm"`` and a
    caller relaying it should not be failed for a stray space."""
    units = snapshot.get("units")
    if str(units or "").strip().lower() == "mm":
        return []
    return [_finding(RULE_UNITS, "error", {"units": units, "expected": "mm"})]


def _check_layers(standard: Standard, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule 2. A layer an entity is on that the standard does not name is a warning.

    Only the *names* are compared — ``Standard.layers`` carries each row's colour, linetype and
    lineweight too, but those describe the layer *table's* definition, which this snapshot does
    not hold (see the module docstring: the ``layer_table()`` read is deferred). Names are
    upper-cased the way AutoCAD stores them, so ``a-wall`` lands on the ``A-WALL`` row.
    """
    known = standard.layers
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in snapshot.get("layers_in_use") or []:
        name = str(raw or "").strip()
        if not name:
            continue
        key = name.upper()
        # One finding per layer, however many entities are on it, and a name the standard knows
        # regardless of case is not a finding at all.
        if key in known or key in seen:
            continue
        seen.add(key)
        issues.append(_finding(RULE_LAYER, "warning", {"layer": name}, [name]))
    return issues


def _check_text_heights(
    standard: Standard,
    snapshot: dict[str, Any],
    numerator: int,
    denominator: int,
    assumed: bool,
) -> list[dict[str, Any]]:
    """Rule 3, the one AutoCAD's CHECKSTANDARDS runs: a text's paper height is off the series.

    A paper height is the model height times the plot scale — ``height * numerator / denominator``,
    which for the ordinary ``1:N`` plan is the model height over N. Text on the sheet's layers (or
    a text the snapshot marks ``paper``) is already at 1:1 in paper millimetres and is not scaled.
    A blank text is skipped, and so is one whose height cannot be read: a check that cannot be sure
    says nothing, the rule ``critique`` keeps, rather than making a verdict on a guess.
    """
    series = standard.text_heights
    if not series:
        # A standard that transcribes no series has no opinion on heights; flagging every text
        # against an empty table would be a false verdict, which is worse than a missing one.
        return []
    raw_texts = snapshot.get("texts")
    texts = raw_texts if isinstance(raw_texts, (list, tuple)) else []
    issues: list[dict[str, Any]] = []
    for text in texts:
        if not isinstance(text, dict):
            continue
        content = text.get("content")
        if not isinstance(content, str) or not content.strip():
            continue  # nothing to size, so nothing to find
        height = _number(text.get("height"))
        if height is None or height <= 0:
            continue
        layer = str(text.get("layer") or "")
        on_paper = bool(text.get("paper")) or layer.strip().upper() in SHEET_LAYERS
        paper = height if on_paper else height * numerator / denominator
        if any(abs(nominal - paper) <= HEIGHT_TOLERANCE for nominal in series):
            continue
        # The nearest nominal, ties to the smaller — a caller wants the number to move to, and a
        # tie is arbitrary, so it is broken the same way every time.
        nearest = min(series, key=lambda nominal: (abs(nominal - paper), nominal))
        detail: dict[str, Any] = {
            "height_mm": height,
            "paper_height_mm": round(paper, 4),
            "layer": layer,
            "content": content,
            "nearest_nominal_mm": nearest,
        }
        if assumed:
            detail["assumed_scale"] = DEFAULT_SCALE
        issues.append(_finding(RULE_TEXT_HEIGHT, "error", detail, [content]))
    return issues


def _title_fields(block: dict[str, Any]) -> dict[str, Any] | None:
    """The title block's own fields, under whichever name the snapshot gave them, or None."""
    for key in _TITLE_FIELDS_KEYS:
        value = block.get(key)
        if isinstance(value, dict):
            return value
    return None


def _check_sheet(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule 4. A drawing with no sheet warns; a sheet whose title block states no 比例 errors.

    When a snapshot gives the title block's fields, they are authoritative: a sheet carrying a
    ``scale`` but a block without 比例 still fails, because the block is what a person reads off
    the paper. When it gives no fields, the sheet's own ``scale`` is the evidence that 比例 was
    written — which is how ``drawing_sheet`` records it and ``be.info()["sheet"]`` reports it.
    """
    block = snapshot.get("sheet")
    if not isinstance(block, dict) or not block:
        return [_finding(RULE_NO_SHEET, "warning", {"sheet": {}})]
    fields = _title_fields(block)
    if fields is not None:
        stated = any(str(key).strip() in _SCALE_FIELD_KEYS for key in fields)
    else:
        stated = bool(str(block.get("scale") or "").strip())
    if stated:
        return []
    return [
        _finding(
            RULE_SHEET_SCALE,
            "error",
            {
                "paper": block.get("paper"),
                "title_fields": dict(fields) if fields is not None else {},
            },
        )
    ]


def check(standard: Standard, snapshot: dict) -> list[dict[str, Any]]:
    """Every conformance rule, over a snapshot of the drawing.

    ``snapshot`` is what the calling tool gathers, as plain data in the shape the journal and the
    backends already report, so this stays a pure function and needs no CAD import::

        {
          "units": "mm",                          # from be.info()
          "layers_in_use": ["A-WALL", ...],       # the layers entities are actually on
          "texts": [{"height": 350.0, "layer": "A-ROOM-NAME", "content": "生产车间"}, ...],
          "sheet": {"scale": "1:100", ...},       # be.info()["sheet"], {} when there is none
        }

    Returns a list of ``{"rule", "severity", "detail", "entities"}`` — the shape
    :mod:`acadmcp.critique` established — with ``severity`` either ``"error"`` (the drawing
    breaks the standard) or ``"warning"`` (a reviewer should look). It never raises on a bad
    snapshot and never draws: an unreadable row is skipped, a value it cannot judge is left
    unchecked, and the caller counts the findings.

    ``standard`` is the standard to check against; ``None`` is a caller mistake and is refused by
    name — there is nothing to check against — so gather it with :func:`in_force` and handle its
    ``None`` before calling.
    """
    if not isinstance(standard, Standard):
        raise ValueError(
            "check needs a Standard to check against; none was loaded — read "
            "standards.in_force() and handle its None before calling"
        )
    data = snapshot if isinstance(snapshot, dict) else {}
    numerator, denominator, assumed = _scale(data)
    issues: list[dict[str, Any]] = []
    issues.extend(_check_units(data))
    issues.extend(_check_layers(standard, data))
    issues.extend(_check_text_heights(standard, data, numerator, denominator, assumed))
    issues.extend(_check_sheet(data))
    return issues


_current: Standards | None = None

__all__ = [
    "DEFAULT_SCALE",
    "DEFAULT_STANDARD",
    "HEIGHT_TOLERANCE",
    "MAX_LINEWEIGHT_MM",
    "MIN_LINEWEIGHT_MM",
    "PACKAGE_STANDARDS",
    "RULES",
    "RULE_LAYER",
    "RULE_NO_SHEET",
    "RULE_SHEET_SCALE",
    "RULE_TEXT_HEIGHT",
    "RULE_UNITS",
    "SHEET_LAYERS",
    "Standard",
    "Standards",
    "check",
    "current",
    "describe",
    "get",
    "in_force",
    "lineweight_centimm",
    "load",
    "reset",
    "use",
]
