"""Headless DXF backend built on ezdxf.

Use this when AutoCAD is closed, when you just need a file, or when you want a
fast preview render. It is also the fallback that makes the whole package
testable without a CAD installation.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import const
from ezdxf.enums import TextEntityAlignment
from ezdxf.lldxf.tags import Tags
from ezdxf.lldxf.types import DXFTag

from ..geom import Point, xy
from ..journal import Journal
from .base import BackendError
from .recording import RecordingBackend, unflatten_points

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


class DxfBackend(RecordingBackend):
    """Renders into an in-memory DXF document that can be saved and reopened."""

    name = "dxf"

    #: Where the journal lives inside the file: root dictionary → ACADMCP → JOURNAL.
    #: Same idea as the plugin's DWG journal: the steps travel with the drawing, so
    #: opening a saved file can still undo what a previous session drew.
    JOURNAL_DICT = "ACADMCP"
    JOURNAL_KEY = "JOURNAL"
    #: A group-code-1 string tops out at 2049 bytes; JSON is escaped to ASCII first,
    #: so chunk by characters with margin.
    JOURNAL_CHUNK = 1900

    def __init__(self, path: str | None = None, template: str = "R2010", **_: Any) -> None:
        super().__init__()
        # Set before any read so a backend with no path still reports the field.
        self.journal_unreadable = ""
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
        if path:
            self._read_journal()

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
        self._write_journal()
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
            "journal": {
                "steps": len(self.journal.steps),
                "applied": sum(s.state == "applied" for s in self.journal.steps),
            },
            "journal_unreadable": self.journal_unreadable,
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
    # Hybrid: outside a step the method draws eagerly (unchanged behaviour for
    # the existing tests, examples and scripts); inside a step it records a
    # plugin-format primitive and the commit draws the batch as one unit.
    def line(self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None) -> Any:
        if self.open_step:
            return self._record("line", [p1, p2], layer=layer, linetype=linetype)
        return self._draw_line(p1, p2, layer=layer, linetype=linetype)

    def _draw_line(
        self, p1: Point, p2: Point, *, layer: str = "0", linetype: str | None = None
    ) -> Any:
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
        if self.open_step:
            return self._record("polyline", points, layer=layer, closed=closed, linetype=linetype)
        return self._draw_polyline(points, closed=closed, layer=layer, linetype=linetype)

    def _draw_polyline(
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
        if self.open_step:
            return self._record("circle", [center], layer=layer, radius=radius)
        return self._draw_circle(center, radius, layer=layer)

    def _draw_circle(self, center: Point, radius: float, *, layer: str = "0") -> Any:
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
        if self.open_step:
            return self._record(
                "arc",
                [center],
                layer=layer,
                radius=radius,
                start_angle=start_deg,
                end_angle=end_deg,
            )
        return self._draw_arc(center, radius, start_deg, end_deg, layer=layer)

    def _draw_arc(
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
        if self.open_step:
            return self._record(
                "text",
                [pos],
                layer=layer,
                text=content,
                height=height,
                rotation=rotation,
                align=align,
            )
        return self._draw_text(pos, content, height, layer=layer, rotation=rotation, align=align)

    def _draw_text(
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
        if self.open_step:
            return self._record(
                "mtext", [pos], layer=layer, text=content, height=height, width=width, align=align
            )
        return self._draw_mtext(pos, content, height, width, layer=layer, align=align)

    def _draw_mtext(
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
        if self.open_step:
            return self._record(
                "hatch",
                points,
                layer=layer,
                pattern=pattern,
                scale=scale,
                angle=angle,
                solid=solid,
            )
        return self._draw_hatch(points, pattern, scale, layer=layer, angle=angle, solid=solid)

    def _draw_hatch(
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
        if self.open_step:
            return self._record(
                "dimension",
                [p1, p2],
                layer=layer,
                offset=offset,
                text_height=text_height or 0.0,
            )
        return self._draw_linear_dimension(p1, p2, offset, layer=layer, text_height=text_height)

    def _draw_linear_dimension(
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
        # add_aligned_dim hands back a DimStyleOverride wrapping the DIMENSION
        # entity; the entity is returned because the journal needs its handle and
        # every other _draw_* method returns the entity it created.
        return dim.dimension

    def insert(
        self,
        block: str,
        at: Point,
        *,
        source: str | None = None,
        rotation: float = 0.0,
        scale: float = 1.0,
        scale_x: float | None = None,
        scale_y: float | None = None,
        properties: dict[str, Any] | None = None,
        layer: str = "0",
    ) -> Any:
        """Refused: this backend draws from nothing, so there is no definition to copy.

        ezdxf could hold a block, but the definition has to come from a library .dwg, which
        ezdxf cannot read. Reading one is worth doing and is not done yet.
        """
        # Inside a step the refusal is deferred to the commit, where the recorded
        # primitive reaches _apply_primitive and raises there — same outcome, but
        # the step machinery owns the failure path.
        if self.open_step:
            return self._record(
                "insert",
                [at],
                layer=layer,
                block=block,
                source=source,
                rotation=rotation,
                scale_x=scale if scale_x is None else scale_x,
                scale_y=scale if scale_y is None else scale_y,
                properties=properties,
            )
        raise BackendError(
            "the 'dxf' backend cannot insert blocks: it cannot read a library .dwg to copy "
            "a definition from. Use backend='plugin' to place library elements in AutoCAD."
        )

    # --- journal application ------------------------------------------------
    def _apply_primitive(self, prim: dict[str, Any]) -> Any:
        pts = unflatten_points(prim.get("points", []))
        kind = prim["kind"]
        if kind == "line":
            return self._draw_line(
                pts[0], pts[1], layer=prim.get("layer", "0"), linetype=prim.get("linetype")
            )
        if kind == "polyline":
            return self._draw_polyline(
                pts,
                closed=bool(prim.get("closed", False)),
                layer=prim.get("layer", "0"),
                linetype=prim.get("linetype"),
            )
        if kind == "circle":
            return self._draw_circle(pts[0], float(prim["radius"]), layer=prim.get("layer", "0"))
        if kind == "arc":
            return self._draw_arc(
                pts[0],
                float(prim["radius"]),
                float(prim["start_angle"]),
                float(prim["end_angle"]),
                layer=prim.get("layer", "0"),
            )
        if kind == "text":
            return self._draw_text(
                pts[0],
                str(prim["text"]),
                float(prim["height"]),
                layer=prim.get("layer", "0"),
                rotation=float(prim.get("rotation", 0.0)),
                align=str(prim.get("align", "left")),
            )
        if kind == "mtext":
            return self._draw_mtext(
                pts[0],
                str(prim["text"]),
                float(prim["height"]),
                width=float(prim.get("width", 0.0)),
                layer=prim.get("layer", "0"),
                align=str(prim.get("align", "center")),
            )
        if kind == "hatch":
            return self._draw_hatch(
                pts,
                str(prim.get("pattern", "ANSI31")),
                float(prim.get("scale", 1.0)),
                layer=prim.get("layer", "0"),
                angle=float(prim.get("angle", 0.0)),
                solid=bool(prim.get("solid", False)),
            )
        if kind == "dimension":
            # text_height travels as 0.0 when unset; the drawer treats None as "default".
            height = float(prim.get("text_height") or 0.0) or None
            return self._draw_linear_dimension(
                pts[0],
                pts[1],
                float(prim.get("offset", 0.0)),
                layer=prim.get("layer", "0"),
                text_height=height,
            )
        raise BackendError(
            f"the 'dxf' backend cannot apply primitive kind {kind!r}: it cannot read a "
            "library .dwg to copy a block definition from. Use backend='plugin'."
        )

    def _apply(self, primitives: list[dict[str, Any]]) -> list[str]:
        return [self._apply_primitive(p).dxf.handle for p in primitives]

    def _erase(self, handles: list[str]) -> tuple[int, int]:
        erased = missing = 0
        for handle in handles:
            entity = self.doc.entitydb.get(handle)
            if entity is None or not entity.is_alive:
                missing += 1  # a hand edit already removed it; count, don't fail
                continue
            self.msp.delete_entity(entity)
            erased += 1
        return erased, missing

    # --- journal persistence ------------------------------------------------
    def _write_journal(self) -> None:
        payload = json.dumps(self.journal.to_dict(), ensure_ascii=True)
        dictionary = self.doc.rootdict.get_required_dict(self.JOURNAL_DICT)
        # add_xrecord always appends, hence the get-or-add: never two JOURNAL records.
        xrecord = dictionary.get(self.JOURNAL_KEY) or dictionary.add_xrecord(self.JOURNAL_KEY)
        xrecord.tags = Tags(
            [
                DXFTag(1, payload[i : i + self.JOURNAL_CHUNK])
                for i in range(0, len(payload), self.JOURNAL_CHUNK)
            ]
        )

    def _read_journal(self) -> None:
        self.journal_unreadable = ""
        dictionary = self.doc.rootdict.get(self.JOURNAL_DICT)
        xrecord = dictionary.get(self.JOURNAL_KEY) if dictionary else None
        if xrecord is None:
            return
        payload = "".join(str(tag.value) for tag in xrecord.tags if tag.code == 1)
        if not payload:
            return
        try:
            self.journal = Journal.from_dict(json.loads(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            # The rule from the plugin journal applies here too: an unreadable
            # record is counted and reported, never silently skipped.
            self.journal_unreadable = f"journal in {self.path!r} ignored: {exc}"
