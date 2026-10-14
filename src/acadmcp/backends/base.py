"""The interface every drawing backend implements.

The contract is deliberately small: a backend renders primitives, it never
computes architecture. All of that lives in :mod:`acadmcp.geom` and
:mod:`acadmcp.ops`, so both drivers produce byte-for-byte equivalent geometry.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from ..geom import Point


class BackendError(RuntimeError):
    """Raised when a backend cannot carry out a drawing request."""


@runtime_checkable
class Backend(Protocol):
    name: str

    # --- layers & document -------------------------------------------------
    def ensure_layer(
        self, name: str, color: int | None = None, linetype: str | None = None
    ) -> Any: ...

    def set_units_mm(self) -> None: ...

    def save(self, path: str | None = None) -> str: ...

    def preview(self, path: str, theme: str = "dark") -> str | None: ...

    def screenshot(
        self,
        path: str,
        *,
        window: Sequence[float] | None = None,
        width: int = 1600,
        height: int = 1200,
        theme: str = "dark",
    ) -> str: ...

    def info(self) -> dict[str, Any]: ...

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]: ...

    # --- primitives --------------------------------------------------------
    def line(
        self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None
    ) -> Any: ...

    def polyline(
        self,
        points: Sequence[Point],
        *,
        closed: bool = False,
        layer: str = "0",
        linetype: str | None = None,
    ) -> Any: ...

    def circle(self, center: Point, radius: float, *, layer: str = "0") -> Any: ...

    def arc(
        self,
        center: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        *,
        layer: str = "0",
    ) -> Any: ...

    def text(
        self,
        pos: Point,
        content: str,
        height: float,
        *,
        layer: str = "0",
        rotation: float = 0.0,
        align: str = "left",
    ) -> Any: ...

    def mtext(
        self,
        pos: Point,
        content: str,
        height: float,
        width: float = 0.0,
        *,
        layer: str = "0",
        align: str = "center",
    ) -> Any: ...

    def hatch(
        self,
        points: Sequence[Point],
        pattern: str = "ANSI31",
        scale: float = 1.0,
        *,
        layer: str = "0",
        angle: float = 0.0,
        solid: bool = False,
    ) -> Any: ...

    def linear_dimension(
        self,
        p1: Point,
        p2: Point,
        offset: float,
        *,
        layer: str = "0",
        text_height: float | None = None,
    ) -> Any: ...
