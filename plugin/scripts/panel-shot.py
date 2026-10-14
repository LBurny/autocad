"""Save a PNG of a live window, by default AutoCAD's main frame.

Used by ``scripts/selftest.sh --shot`` to look at the steps palette, which has no
other way of being verified: it is a WinForms control inside AutoCAD, so there is
nothing to assert about it from outside beyond the fact that it built and is
visible.

Pass a title fragment to photograph some other window. That matters for the
palette: a *floating* PaletteSet is its own top-level window, not a child of the
AutoCAD frame, so capturing the frame misses it entirely — which is exactly what
the first attempt at this did.

Prints the path it wrote.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from acadmcp.backends.base import BackendError  # noqa: E402
from acadmcp.capture import (  # noqa: E402
    capture_window,
    find_autocad_window,
    find_window_by_title,
)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: panel-shot.py <path.png> [title fragment]", file=sys.stderr)
        return 2

    path = sys.argv[1]
    hwnd = 0
    if len(sys.argv) > 2:
        try:
            hwnd = find_window_by_title(sys.argv[2])
        except BackendError as exc:
            # A docked palette is a child of the AutoCAD frame and has no title of
            # its own, and AutoCAD remembers where the user put it. Capturing the
            # frame is then the right picture anyway — it contains the palette.
            print(f"{exc}; falling back to the AutoCAD frame", file=sys.stderr)
    if not hwnd:
        hwnd = find_autocad_window()

    print(capture_window(hwnd, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
