"""Backend drivers.

Two implementations share one interface:

* :class:`~acadmcp.backends.dxf.DxfBackend` builds a DXF document with ezdxf and
  needs no AutoCAD at all.
* :class:`~acadmcp.backends.com.ComBackend` drives a *running* AutoCAD session
  over ActiveX/COM, drawing straight into the live editor.
"""

from __future__ import annotations

from .base import Backend, BackendError

__all__ = ["Backend", "BackendError", "make_backend"]


def make_backend(kind: str, **kwargs) -> Backend:
    """Instantiate a backend by name: ``"dxf"`` or ``"com"``."""
    kind = (kind or "dxf").strip().lower()
    if kind == "dxf":
        from .dxf import DxfBackend

        return DxfBackend(**kwargs)
    if kind == "com":
        from .com import ComBackend

        return ComBackend(**kwargs)
    raise BackendError(f"unknown backend {kind!r}; expected 'dxf' or 'com'")
