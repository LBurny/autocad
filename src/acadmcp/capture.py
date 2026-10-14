"""Screen capture of a top-level window.

The live-AutoCAD backend has no way to hand back a rendered image through COM, so
it photographs the AutoCAD window instead. That has the side benefit of showing
exactly what the user sees — including the current zoom, view and any UI state.
"""

from __future__ import annotations

import time
from pathlib import Path

from .backends.base import BackendError

#: PrintWindow flag that captures DirectX-composited windows. AutoCAD renders
#: through DirectX, and without this flag the capture comes back solid black.
PW_RENDERFULLCONTENT = 2

#: How long to let a restored window settle before capturing it.
RESTORE_SETTLE_S = 0.6


def capture_window(hwnd: int, path: str) -> str:
    """Save a PNG of the window `hwnd` to `path` and return the path.

    A minimised window is restored first: there is nothing to render while it is
    down, and a caller asking for a picture of a window cannot act on advice to
    put it back up itself.
    """
    try:
        from ctypes import windll

        import win32con
        import win32gui
        import win32ui
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise BackendError(
            "screenshotting a live window needs pywin32 and pillow: uv sync"
        ) from exc

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # The window manager needs a moment before the window has a drawable
        # rect and the application has repainted into it.
        time.sleep(RESTORE_SETTLE_S)
        if win32gui.IsIconic(hwnd):
            raise BackendError(f"window {hwnd} is minimised and would not restore")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise BackendError(f"window {hwnd} has no drawable area ({width}x{height})")

    window_dc = win32gui.GetWindowDC(hwnd)
    src_dc = win32ui.CreateDCFromHandle(window_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bitmap)
        if not windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), PW_RENDERFULLCONTENT):
            raise BackendError(f"PrintWindow refused to capture window {hwnd}")
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        # GetBitmapBits hands back BGRX rows, whatever the window's real format.
        image = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)
    return path
