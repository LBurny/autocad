"""Headless DXF backend built on ezdxf.

Use this when AutoCAD is closed, when you just need a file, or when you want a
fast preview render. It is also the fallback that makes the whole package
testable without a CAD installation.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import const
from ezdxf.enums import TextEntityAlignment

from ..geom import Point, xy
from .base import BackendError

#: Text style registering a CJK-capable TrueType face. SimSun rather than SimHei
#: because SimHei has no superscript-two glyph, which the area labels need.
CJK_STYLE = "HZ"
CJK_FONT = "simsun.ttc"

_ALIGN = {
    "left": TextEntityAlignment.LEFT,
    "center": TextEntityAlignment.MIDDLE_CENTER,
    "right": TextEntityAlignment.MIDDLE_RIGHT,
    "bottom-left": TextEntityAlignment.BOTTOM_LEFT,
    "top-left": TextEntityAlignment.TOP_LEFT,
    "top-center": TextEntityAlignment.TOP_CENTER,
    "middle-left": TextEntityAlignment.MIDDLE_LEFT,
}

#: AutoCAD refuses to create an mtext with zero width; a sane floor-plan default.
DEFAULT_MTEXT_WIDTH = 6000.0

#: Dimension text height in mm, used when the caller does not specify one.
DIM_TEXT_HEIGHT = 250.0


class DxfBackend:
    """Renders into an in-memory DXF document that can be saved and reopened."""

    name = "dxf"

    def __init__(self, path: str | None = None, template: str = "R2010", **_: Any) -> None:
        if path:
            try:
                self.doc = ezdxf.readfile(path)
            except OSError as exc:
                raise BackendError(f"cannot open DXF {path!r}: {exc}") from exc
        else:
            self.doc = ezdxf.new(template, setup=True)
            self.doc.units = ezdxf.units.MM
        self.text_style = CJK_STYLE
        self._ensure_style()
        self.msp = self.doc.modelspace()
        self.path = path
        self._ops = 0

    # --- document ----------------------------------------------------------
    def _ensure_style(self) -> None:
        self.set_text_style(CJK_STYLE, CJK_FONT)

    def set_text_style(
        self, name: str | None = None, font: str | None = None, big_font: str | None = None
    ) -> str:
        """Create or repoint the text style used for drawn text.

        A true-type face is the default because SHX fonts carry no CJK glyphs;
        pass a font/big-font pair (gbenor.shx + gbcbig.shx) to match a client
        standard instead.
        """
        name = name or self.text_style
        font = font or CJK_FONT
        if name in self.doc.styles:
            style = self.doc.styles.get(name)
        else:
            try:
                style = self.doc.styles.add(name, font=font)
            except Exception:  # noqa: BLE001 - a missing font must not be fatal
                style = self.doc.styles.add(name, font="txt.ttf")
        style.dxf.font = font
        if big_font is not None:
            style.dxf.bigfont = big_font
        self.text_style = name
        return name

    def _attribs(self, layer: str, linetype: str | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"layer": layer}
        if linetype and linetype != "CONTINUOUS":
            if linetype not in self.doc.linetypes:
                self.doc.linetypes.add(linetype, pattern=[0.5, 0.25, -0.25])
            d["linetype"] = linetype
        return d

    def ensure_layer(self, name: str, color: int | None = None, linetype: str | None = None) -> Any:
        layer = self.doc.layers.get(name) if name in self.doc.layers else self.doc.layers.add(name)
        if color is not None:
            layer.color = int(color)
        if linetype and linetype != "CONTINUOUS":
            if linetype not in self.doc.linetypes:
                self.doc.linetypes.add(linetype, pattern=[0.5, 0.25, -0.25])
            layer.dxf.linetype = linetype
        return layer

    def set_units_mm(self) -> None:
        self.doc.units = ezdxf.units.MM
        self.doc.header["$INSUNITS"] = 4

    def save(self, path: str | None = None) -> str:
        target = path or self.path
        if not target:
            raise BackendError("no output path given and the document has none")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        self.doc.saveas(target)
        self.path = target
        return target

    def preview(self, path: str, theme: str = "dark") -> str | None:
        """Render to PNG so the drawing can be eyeballed without opening AutoCAD."""
        return self.screenshot(path, theme=theme)

    def screenshot(
        self,
        path: str,
        *,
        window: Sequence[float] | None = None,
        width: int = 1600,
        height: int = 1200,
        theme: str = "dark",
    ) -> str:
        """Render the drawing to PNG, optionally framed on a region.

        `window` is (min_x, min_y, max_x, max_y) in drawing units; without it the
        whole drawing is framed. The window is what makes a detail shot legible —
        a whole floor plan scaled to 1600 px leaves room labels unreadable.

        `width`/`height` give the canvas' long edge; the canvas itself is fitted
        to the framed region's aspect, so no pixels go to empty margin.
        """
        try:
            import matplotlib

            matplotlib.use("Agg")
            matplotlib.rcParams["font.sans-serif"] = [
                "Microsoft YaHei",
                "SimSun",
                "SimHei",
                "DejaVu Sans",
            ]
            matplotlib.rcParams["axes.unicode_minus"] = False
            import matplotlib.pyplot as plt
            from ezdxf.addons.drawing import Frontend, RenderContext
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendError("screenshot needs matplotlib: uv sync --extra preview") from exc

        bg, fg = ("#1e1e1e", "#ffffff") if theme == "dark" else ("#ffffff", "#000000")
        dpi = 100
        fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
        ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
        ax.set_axis_off()
        context = RenderContext(self.doc)
        # The drawing's own colours assume a light background, so on a dark theme
        # everything is forced to the foreground colour instead.
        with contextlib.suppress(Exception):
            context.current_layout_properties.set_colors(fg)
        Frontend(context, MatplotlibBackend(ax)).draw_layout(self.msp, finalize=True)
        if window:
            xs = [float(window[0]), float(window[2])]
            ys = [float(window[1]), float(window[3])]
            ax.set_xlim(min(xs), max(xs))
            ax.set_ylim(min(ys), max(ys))
        # finalize() resizes the figure to ezdxf's own 6.3 x 4.8 default, so the
        # canvas has to be set afterwards. It is fitted to the framed region's
        # aspect rather than to a fixed rectangle: a portrait plan dropped into a
        # 4:3 canvas gets letterboxed, and the drawing ends up a quarter smaller
        # than it needs to be — which is exactly what makes text unreadable.
        x_lo, x_hi = ax.get_xlim()
        y_lo, y_hi = ax.get_ylim()
        span_x, span_y = abs(x_hi - x_lo), abs(y_hi - y_lo)
        long_edge = float(max(width, height))
        if span_x <= 0 or span_y <= 0:
            fig_w, fig_h = float(width), float(height)
        elif span_x >= span_y:
            fig_w, fig_h = long_edge, long_edge * span_y / span_x
        else:
            fig_w, fig_h = long_edge * span_x / span_y, long_edge
        fig.set_size_inches(fig_w / dpi, fig_h / dpi)
        fig.set_facecolor(bg)
        ax.set_facecolor(bg)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, facecolor=bg, dpi=dpi)
        plt.close(fig)
        return path

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "path": self.path,
            "units": "mm",
            "entities": len(self.msp),
            "layers": sorted(layer.dxf.name for layer in self.doc.layers),
            "ops": self._ops,
        }

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        for e in self.msp:
            if layer and e.dxf.get("layer", "0") != layer:
                continue
            out.append({"type": e.dxftype(), "layer": e.dxf.get("layer", "0")})
            if len(out) >= limit:
                break
        return out

    # --- primitives --------------------------------------------------------
    def line(self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None) -> Any:
        self._ops += 1
        return self.msp.add_line(xy(p1), xy(p2), dxfattribs=self._attribs(layer, linetype))

    def polyline(
        self,
        points: Sequence[Point],
        *,
        closed: bool = False,
        layer: str = "0",
        linetype: str | None = None,
    ) -> Any:
        self._ops += 1
        pts = [xy(p) for p in points]
        return self.msp.add_lwpolyline(pts, close=closed, dxfattribs=self._attribs(layer, linetype))

    def circle(self, center: Point, radius: float, *, layer: str = "0") -> Any:
        self._ops += 1
        return self.msp.add_circle(xy(center), float(radius), dxfattribs=self._attribs(layer))

    def arc(
        self,
        center: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        *,
        layer: str = "0",
    ) -> Any:
        self._ops += 1
        return self.msp.add_arc(
            xy(center),
            float(radius),
            float(start_deg),
            float(end_deg),
            dxfattribs=self._attribs(layer),
        )

    def text(
        self,
        pos: Point,
        content: str,
        height: float,
        *,
        layer: str = "0",
        rotation: float = 0.0,
        align: str = "left",
    ) -> Any:
        self._ops += 1
        t = self.msp.add_text(
            str(content),
            height=float(height),
            dxfattribs={
                "layer": layer,
                "style": self.text_style,
                "rotation": float(rotation),
            },
        )
        t.set_placement(xy(pos), align=_ALIGN.get(align, TextEntityAlignment.LEFT))
        return t

    def mtext(
        self,
        pos: Point,
        content: str,
        height: float,
        width: float = 0.0,
        *,
        layer: str = "0",
        align: str = "center",
    ) -> Any:
        self._ops += 1
        attach = {"center": 5, "left": 7, "top-center": 3}.get(align, 5)
        m = self.msp.add_mtext(
            str(content),
            dxfattribs={
                "layer": layer,
                "char_height": float(height),
                "style": self.text_style,
                "attachment_point": attach,
            },
        )
        m.set_location(xy(pos))
        m.dxf.width = float(width) if width else DEFAULT_MTEXT_WIDTH
        return m

    def hatch(
        self,
        points: Sequence[Point],
        pattern: str = "ANSI31",
        scale: float = 1.0,
        *,
        layer: str = "0",
        angle: float = 0.0,
        solid: bool = False,
    ) -> Any:
        self._ops += 1
        h = self.msp.add_hatch(dxfattribs=self._attribs(layer))
        h.paths.add_polyline_path(
            [xy(p) for p in points], is_closed=True, flags=const.BOUNDARY_PATH_EXTERNAL
        )
        if solid:
            h.set_solid_fill(color=7)
        else:
            h.set_pattern_fill(pattern, scale=float(scale), angle=float(angle))
        return h

    def linear_dimension(
        self,
        p1: Point,
        p2: Point,
        offset: float,
        *,
        layer: str = "0",
        text_height: float | None = None,
    ) -> Any:
        self._ops += 1
        a, b = xy(p1), xy(p2)
        distance = float(offset) or 1.0
        h = float(text_height or DIM_TEXT_HEIGHT)
        # AutoCAD's stock dimstyles assume a ~1-unit drawing, so every metric
        # size is overridden here; without this the text renders invisible.
        override = {
            # ezdxf's stock EZDXF dimstyle is built for metre-unit drawings:
            # dimlfac=100 would print a 6000 mm span as "600000".
            "dimlfac": 1.0,
            "dimtxt": h,
            "dimasz": h * 0.8,
            "dimexe": h * 0.5,
            "dimexo": h * 0.5,
            "dimgap": h * 0.3,
        }
        dim = self.msp.add_aligned_dim(
            p1=a, p2=b, distance=distance, override=override, dxfattribs=self._attribs(layer)
        )
        dim.render()
        return dim
