"""Backend drivers.

Three implementations share one interface:

* :class:`~acadmcp.backends.dxf.DxfBackend` builds a DXF document with ezdxf and
  needs no AutoCAD at all.
* :class:`~acadmcp.backends.com.ComBackend` drives a *running* AutoCAD session
  over ActiveX/COM, drawing straight into the live editor.
* :class:`~acadmcp.backends.plugin.PluginBackend` does not draw as it goes: it
  records primitives and hands whole steps to the in-AutoCAD plugin, which keeps
  them in the drawing's own journal so they can be reviewed and rolled back.
"""

from __future__ import annotations

from .base import Backend, BackendError

__all__ = ["Backend", "BackendError", "make_backend"]


def make_backend(kind: str, **kwargs) -> Backend:
    """Instantiate a backend by name: ``"dxf"``, ``"com"`` or ``"plugin"``."""
    kind = (kind or "dxf").strip().lower()
    if kind == "dxf":
        from .dxf import DxfBackend

        return DxfBackend(**kwargs)
    if kind == "com":
        from .com import ComBackend

        return ComBackend(**kwargs)
    if kind == "plugin":
        from .plugin import PluginBackend

        return PluginBackend(**kwargs)
    raise BackendError(f"unknown backend {kind!r}; expected 'dxf', 'com' or 'plugin'")
