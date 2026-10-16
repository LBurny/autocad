"""The shipped templates: which sheet a drawing starts from, and whether it fits.

A template here is not a drawing — it is the *specification* of one: a standard to draw
under, a paper, an orientation, a plot scale, and the title block's fields. ``drawing_new``
applies it, and the committed ``templates/*.dxf`` are the artefacts the same specification
builds, kept so a reader can open the thing rather than take the fields on trust.

Why a spec and an artefact rather than one drawing to copy: copying a drawing copies whatever
was in it, and what is in it is a frame. The standard, the layer table and the plot scale are
*settings*, and a specification is the only form that can be checked — which is what
``scripts/build_templates.py`` does, rebuilding each artefact and comparing a geometry
fingerprint, so a template cannot silently drift from the specification that names it.

Three rules, both of them the catalogue's:

* **Loading never raises.** A missing or broken file, an entry with no key, a row that is not
  an object — each is skipped and *counted* in :attr:`Templates.skipped`, never raised. A
  templates file that failed to read must not take the drawing session with it.
* **Later files win, key by key.** The package's file is read first and a project's merges over
  it, so a project adds a template or corrects one and inherits the rest. A project names its
  files in ``acadmcp.json``'s ``templates`` list, exactly the way it names its standards.
* **A key nobody shipped refuses by name, listing what does exist.** ``gb_a2_arch`` is not
  guessed at and not rounded to ``gb_a3_arch``; the refusal names the templates that are there,
  because the caller's next move is to pick one of them.

Pure module: it imports ``json``, ``pathlib`` and the standard library and no CAD library, so
the registry can be read on any machine and the build script stays the only thing that draws.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The templates that ship with the package.
PACKAGE_TEMPLATES = Path(__file__).with_name("data") / "templates.json"


@dataclass(frozen=True)
class TemplateSpec:
    """One template: what a drawing starts from, before anything is drawn on it."""

    key: str
    label: str
    standard: str
    paper: str
    orientation: str
    scale: str
    title_fields: dict[str, str] = field(default_factory=dict)
    artefact: str = ""
    #: The file that declared this spec, empty for a package one. Not in `describe()`: it is
    #: where the artefact resolves from, not a field a caller reads or the output schema pins.
    source_file: str = ""

    def describe(self) -> dict[str, Any]:
        """The row a caller reads, and the shape ``template_list`` returns."""
        return {
            "key": self.key,
            "label": self.label,
            "standard": self.standard,
            "paper": self.paper,
            "orientation": self.orientation,
            "scale": self.scale,
            "title_fields": dict(self.title_fields),
            "artefact": self.artefact,
        }


class Templates:
    """The templates loaded for this process, plus what was skipped getting there."""

    def __init__(
        self,
        templates: dict[str, TemplateSpec] | None = None,
        skipped: list[str] | None = None,
    ) -> None:
        self.templates: dict[str, TemplateSpec] = dict(templates or {})
        #: One line per entry that could not be used, in the order it was read.
        self.skipped: list[str] = list(skipped or [])

    # --- loading -----------------------------------------------------------

    @classmethod
    def load(cls, paths: list[str | Path] | None = None) -> Templates:
        """Read the package templates and then any project ones, later keys winning."""
        registry = cls()
        registry.add_file(PACKAGE_TEMPLATES, package=True)
        for path in paths or []:
            registry.add_file(Path(path))
        return registry

    def add_file(self, path: Path, *, package: bool = False) -> None:
        """Merge one templates file in. A file that cannot be read is reported, not fatal.

        A project entry records the file that declared it so its artefact resolves beside that
        file; a package entry records nothing, since the shipped artefacts are repository paths.
        """
        source_file = Path(path)
        try:
            payload = json.loads(source_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipped.append(f"{source_file}: no such templates file")
            return
        except (OSError, ValueError) as exc:
            self.skipped.append(f"{source_file}: {exc}")
            return

        entries = payload.get("templates") if isinstance(payload, dict) else payload
        if isinstance(entries, dict):
            # Keyed by name rather than a list: the same fact, and a hand-written file is
            # likelier to be written this way.
            entries = [
                dict(spec, key=spec.get("key", name)) if isinstance(spec, dict) else spec
                for name, spec in entries.items()
            ]
        if not isinstance(entries, list):
            self.skipped.append(
                f"{source_file}: expected a list of templates, under 'templates' or at the top"
            )
            return

        for index, entry in enumerate(entries):
            spec = _read_template(
                entry,
                where=f"{source_file}[{index}]",
                skipped=self.skipped,
                source_file="" if package else str(source_file),
            )
            if spec is not None:
                self.templates[spec.key] = spec

    # --- lookup ------------------------------------------------------------

    def get(self, key: str) -> TemplateSpec | None:
        return self.templates.get(str(key or "").strip().lower())

    def keys(self) -> list[str]:
        return sorted(self.templates)

    def specs(self) -> list[TemplateSpec]:
        """Every template, in the order `keys()` reports them — what the builder iterates."""
        return [self.templates[key] for key in self.keys()]

    def resolve(self, key: str) -> TemplateSpec:
        """One template, or ``ValueError`` naming the ones that exist."""
        wanted = str(key or "").strip()
        found = self.get(wanted)
        if found is not None:
            return found
        raise ValueError(
            f"unknown template {key!r}; this package ships: " + (", ".join(self.keys()) or "none")
        )

    def __contains__(self, key: object) -> bool:
        return self.get(str(key)) is not None

    def __iter__(self):
        return iter(self.keys())

    def rows(self) -> list[dict[str, Any]]:
        """The report shape ``template_list`` returns: one describe() per template."""
        return [spec.describe() for spec in self.specs()]

    def describe(self) -> dict[str, Any]:
        return {
            "count": len(self.templates),
            "skipped": list(self.skipped),
            "templates": self.rows(),
        }


def _read_template(
    entry: Any, *, where: str, skipped: list[str], source_file: str = ""
) -> TemplateSpec | None:
    """One template out of a file, or None with the reason appended to `skipped`.

    Tolerant about what is absent and strict about what is wrong: a missing label defaults to
    the key and a missing orientation to landscape, while an entry with no key, no paper or no
    scale cannot be placed by anything and the person reading the report needs the line to fix.
    """
    if not isinstance(entry, dict):
        skipped.append(f"{where}: not an object")
        return None

    key = str(entry.get("key", "")).strip().lower()
    if not key:
        skipped.append(f"{where}: no key")
        return None

    paper = str(entry.get("paper", "")).strip()
    if not paper:
        skipped.append(f"{key}: no paper to draw on")
        return None

    scale = str(entry.get("scale", "")).strip()
    if not scale:
        skipped.append(f"{key}: no scale to plot at")
        return None

    fields = entry.get("title_fields") or {}
    if not isinstance(fields, dict):
        skipped.append(f"{key}: title_fields must be an object")
        fields = {}

    return TemplateSpec(
        key=key,
        label=str(entry.get("label", key)),
        standard=str(entry.get("standard", "")).strip(),
        paper=paper.upper(),
        orientation=str(entry.get("orientation", "landscape")).strip().lower() or "landscape",
        scale=scale,
        title_fields={str(name): str(value) for name, value in fields.items()},
        # Derived rather than required: where the artefact lives is a fact about this package,
        # not something a hand-written entry should have to restate.
        artefact=str(entry.get("artefact", f"templates/{key}.dxf")).strip(),
        source_file=source_file,
    )


def load(paths: list[str | Path] | None = None) -> Templates:
    """Read the templates named by a list of files, over the package's own."""
    return Templates.load(paths)


def current() -> Templates:
    """The templates for this process, loaded once.

    The package's file first, then the project's own, named by its ``acadmcp.json``
    ``templates`` list and merged key by key — the standards rule. `settings` is imported here
    rather than at module scope so this stays a pure module with no import cycle.
    """
    global _current
    if _current is None:
        from . import settings

        _current = Templates.load(settings.current().templates)
    return _current


def reset() -> None:
    """Forget the loaded templates. For tests, and after a project file changes."""
    global _current
    _current = None


def use(loaded: Templates) -> Templates:
    """Install `loaded` as the templates in force, and return it."""
    global _current
    _current = loaded
    return _current


def resolve_template(key: str) -> TemplateSpec:
    """One template by key from the registry in force, or ``ValueError`` naming what exists."""
    return current().resolve(key)


def template_rows() -> list[dict[str, Any]]:
    """Every template as a reportable row — the shape ``template_list`` returns."""
    return current().rows()


def artefact_path(spec: TemplateSpec) -> Path:
    """Where a spec's committed artefact lives.

    A project template resolves `artefact` beside the file that declared it — that file is the
    only thing that knows where the project keeps its sheets, and the path is relative there for
    the same reason a settings path is, because nobody commits an absolute one. A package
    template, which records no source file, resolves against the repository root exactly as
    before, so the shipped artefacts and the fingerprint gate keep the path they always had.
    """
    if spec.source_file:
        return Path(spec.source_file).resolve().parent / spec.artefact
    return Path(__file__).resolve().parents[2] / spec.artefact


#: The repository's `templates/` directory, where the artefacts are committed.
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

_current: Templates | None = None

__all__ = [
    "PACKAGE_TEMPLATES",
    "TEMPLATE_DIR",
    "TemplateSpec",
    "Templates",
    "artefact_path",
    "current",
    "load",
    "reset",
    "resolve_template",
    "template_rows",
    "use",
]
