"""Project settings: ``acadmcp.json``.

An institute or a factory has its own standard — layer names, text heights, model
numbers, which block library is the true one. None of that belongs in this package's
source, and none of it belongs in a database either: it has to be a file the draughtsman
can open, read and change.

Settings are found by walking up from the working directory, so a project's file applies
to every drawing inside it. Loading never raises, for the same reason the catalogue's does
not: a broken settings file must not be the end of the drawing session, it must be a line
in the report.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: The file looked for, in the working directory and then upwards.
FILENAME = "acadmcp.json"

#: How far up to look. Deep enough for a project inside a checkout, shallow enough not to
#: pick up an unrelated file in a user's home directory.
MAX_DEPTH = 6


class Settings:
    """What a project overrode, and what it got wrong."""

    def __init__(
        self,
        library: str = "",
        catalogs: list[str] | None = None,
        layers: dict[str, tuple[int, str]] | None = None,
        source: Path | None = None,
        skipped: list[str] | None = None,
    ) -> None:
        self.library = library
        self.catalogs = list(catalogs or [])
        self.layers = dict(layers or {})
        self.source = source
        self.skipped = list(skipped or [])

    @property
    def found(self) -> bool:
        return self.source is not None

    def describe(self) -> dict[str, Any]:
        return {
            "file": str(self.source) if self.source else "",
            "library": self.library,
            "catalogs": list(self.catalogs),
            "layers": {name: list(spec) for name, spec in self.layers.items()},
            "skipped": list(self.skipped),
        }


def find(start: Path | None = None) -> Path | None:
    """The nearest ``acadmcp.json`` at or above this directory, if there is one."""
    directory = (start or Path.cwd()).resolve()
    for _ in range(MAX_DEPTH):
        candidate = directory / FILENAME
        if candidate.is_file():
            return candidate
        if directory.parent == directory:
            break
        directory = directory.parent
    return None


def load(path: Path | None = None) -> Settings:
    """Read the project's settings. A file that cannot be read is reported, never raised."""
    path = path or find()
    if path is None:
        return Settings()
    if not Path(path).is_file():
        # `found` has to mean a file was there to read: a project's settings being absent is
        # the ordinary case, and reporting it as found-but-empty would hide a wrong path.
        return Settings(skipped=[f"{path}: no such settings file"])

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Settings(source=Path(path), skipped=[f"{path}: {exc}"])

    if not isinstance(payload, dict):
        return Settings(source=Path(path), skipped=[f"{path}: expected an object"])

    skipped: list[str] = []
    # The environment wins over the file: a path that differs per machine is the one thing
    # nobody wants to commit, and this is how a colleague points it at their own drive
    # without editing a tracked file.
    library = str(os.environ.get("ACADMCP_LIBRARY") or payload.get("library", ""))

    catalogs = payload.get("catalog") or payload.get("catalogs") or []
    if isinstance(catalogs, str):
        catalogs = [catalogs]

    layers: dict[str, tuple[int, str]] = {}
    raw_layers = payload.get("layers") or {}
    if isinstance(raw_layers, dict):
        for name, spec in raw_layers.items():
            if isinstance(spec, dict):
                layers[str(name)] = (
                    int(spec.get("color", 7)),
                    str(spec.get("linetype", "CONTINUOUS")),
                )
            elif isinstance(spec, list) and len(spec) == 2:
                layers[str(name)] = (int(spec[0]), str(spec[1]))
            else:
                skipped.append(f"{path}: layer '{name}' must be an object or [colour, linetype]")
    elif raw_layers:
        skipped.append(f"{path}: 'layers' must be an object")

    return Settings(
        library=library,
        catalogs=[str(Path(str(path)).parent / name) if not Path(name).is_absolute() else name
                  for name in catalogs],
        layers=layers,
        source=Path(path),
        skipped=skipped,
    )


def current() -> Settings:
    """The project's settings, read once."""
    global _current
    if _current is None:
        _current = load()
    return _current


def reset() -> None:
    """Forget the loaded settings, and the catalogue that was built from them."""
    global _current
    _current = None
    from . import catalog

    catalog.reset()


_current: Settings | None = None
