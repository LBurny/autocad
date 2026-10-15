"""Headless DXF backend built on ezdxf.

Use this when AutoCAD is closed, when you just need a file, or when you want a
fast preview render. It is also the fallback that makes the whole package
testable without a CAD installation.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import const
from ezdxf.enums import TextEntityAlignment
from ezdxf.lldxf.tags import Tags
from ezdxf.lldxf.types import DXFTag

from .. import layers, sheet
from ..catalog import Catalog
from ..geom import Point, xy
from ..journal import Journal
from ..standards import lineweight_centimm
from .base import BackendError
from .recording import RecordingBackend, flatten_points, unflatten_points

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

#: DXF group 75: which standard scale a layout plots at. A plot reads this together with group
#: 147 (`unit_factor`, the actual denominator), and AutoCAD only writes one of a fixed set of
#: rows — so both are set from one place in `set_page_setup`, because a file whose 75 says 1:10
#: while its 147 says 1.0 plots at 1:1 and claims 1:10. A ratio with no row here is written to
#: 147 alone rather than given an invented 75. The rows are the ISO/ANSI set AutoCAD offers,
#: with the code for each; 1:200 is 16, which is the value AutoCAD writes for a 1:200 plot.
STANDARD_SCALE_TYPES: dict[tuple[int, int], int] = {
    (1, 1): 0,
    (1, 2): 1,
    (1, 4): 2,
    (1, 5): 3,
    (1, 10): 4,
    (1, 20): 5,
    (1, 50): 6,
    (1, 100): 7,
    (2, 1): 8,
    (4, 1): 9,
    (8, 1): 10,
    (10, 1): 11,
    (100, 1): 13,
    (1, 200): 16,
}


def _has_cjk(text: str) -> bool:
    """Whether `text` contains a character ezdxf's glyph pipeline may mangle."""
    return any(ord(ch) > 0x2E7F for ch in text)


def _text_placement(entity: Any) -> tuple[str, float, float, str, float]:
    """(align name, x, y, text, height) for a TEXT or MTEXT entity.

    TEXT carries the alignment in its ``halign``/``valign``; MTEXT carries it
    in its attachment point, which is a single enum covering both axes.
    """
    if entity.dxftype() == "MTEXT":
        attach = entity.dxf.get("attachment_point", 1)
        name = _MTEXT_ALIGN.get(attach, "TOP_LEFT")
        point = entity.dxf.insert
        return name, point.x, point.y, entity.plain_text(), float(entity.dxf.char_height)
    align, p1, p2 = entity.get_placement()
    name = align.name if hasattr(align, "name") else str(align)
    if p2 is None or name in ("LEFT", "ALIGNED"):
        x, y = p1.x, p1.y
    else:
        x, y = p2.x, p2.y
    return name, x, y, entity.dxf.text, float(entity.dxf.height)


#: MTEXT attachment point -> the same names TXT alignment uses.
_MTEXT_ALIGN = {
    1: "TOP_LEFT",
    2: "TOP_CENTER",
    3: "TOP_RIGHT",
    4: "MIDDLE_LEFT",
    5: "MIDDLE_CENTER",
    6: "MIDDLE_RIGHT",
    7: "BOTTOM_LEFT",
    8: "BOTTOM_CENTER",
    9: "BOTTOM_RIGHT",
}


def _text_frame(entity: Any) -> list[tuple[float, float]]:
    """The box a text occupies in drawing units, for framing the render.

    Approximate on purpose: it only has to be good enough that a title below
    the plan is not cropped out of a full shot.
    """
    name, x, y, text, height = _text_placement(entity)
    if entity.dxftype() == "MTEXT":
        box_w, box_h = float(entity.dxf.width), float(entity.dxf.height)
    else:
        box_w, box_h = layers.text_width(text, height), height * 1.4
    if name.endswith("LEFT"):
        left = x
    elif name.endswith("RIGHT"):
        left = x - box_w
    else:
        left = x - box_w / 2.0
    bottom = y - box_h if name.startswith("TOP") else y
    rot = math.radians(entity.dxf.get("rotation", 0.0))
    corners = ((0, 0), (box_w, 0), (box_w, box_h), (0, box_h))
    if abs(rot) < 1e-9:
        return [(left + dx, bottom + dy) for dx, dy in corners]
    cos, sin = math.cos(rot), math.sin(rot)
    return [(left + dx * cos - dy * sin, bottom + dx * sin + dy * cos) for dx, dy in corners]


_H_ALIGN = {
    "LEFT": "left",
    "MIDDLE_LEFT": "left",
    "BOTTOM_LEFT": "left",
    "TOP_LEFT": "left",
    "ALIGNED": "left",
    "RIGHT": "right",
    "MIDDLE_RIGHT": "right",
    "BOTTOM_RIGHT": "right",
    "TOP_RIGHT": "right",
    "CENTER": "center",
    "MIDDLE_CENTER": "center",
    "BOTTOM_CENTER": "center",
    "TOP_CENTER": "center",
    "MIDDLE": "center",
}

_V_ALIGN = {
    "LEFT": "baseline",
    "RIGHT": "baseline",
    "ALIGNED": "baseline",
    "MIDDLE_LEFT": "center",
    "MIDDLE_RIGHT": "center",
    "MIDDLE_CENTER": "center",
    "MIDDLE": "center",
    "BOTTOM_LEFT": "bottom",
    "BOTTOM_RIGHT": "bottom",
    "BOTTOM_CENTER": "bottom",
    "TOP_LEFT": "top",
    "TOP_RIGHT": "top",
    "TOP_CENTER": "top",
    "CENTER": "baseline",
}


def _draw_cjk_texts(texts: Sequence[Any], ax: Any, color: str, points_per_unit: float) -> None:
    """Draw the CJK texts matplotlib-natively, in the theme's foreground colour.

    `points_per_unit` is points per *vertical* data unit; the axes fills the
    figure, so a text of height h in drawing units is h * points_per_unit
    points tall on the page — which is what keeps a caption the same size
    relative to the plan as it was through the ezdxf path.
    """
    font = _cjk_font_properties()
    if font is None:
        # No CJK face installed at all: leaving the texts to the ezdxf path is
        # better than drawing nothing.
        return
    for entity in texts:
        name, x, y, text, height = _text_placement(entity)
        if not text.strip() or height <= 0:
            continue
        size = height * points_per_unit
        if not math.isfinite(size) or size < 0.1:
            continue
        ax.text(
            x,
            y,
            text,
            color=color,
            fontsize=size,
            rotation=entity.dxf.get("rotation", 0.0),
            rotation_mode="anchor",
            ha=_H_ALIGN.get(name, "center"),
            va=_V_ALIGN.get(name, "center"),
            fontproperties=font,
            zorder=10,
        )


def _cjk_frontend_class() -> Any:
    """A `Frontend` subclass that defers CJK text instead of letting ezdxf tessellate it.

    ezdxf converts every glyph to a filled path, and that pipeline corrupts some CJK outlines —
    SimSun's 图, the commonest character in a drawing title, comes out as an ink blob at some
    canvas scales (the font, matplotlib and AutoCAD all render the same glyph fine). CJK texts
    are collected and drawn natively afterwards; everything else stays on the ezdxf path.

    Built here rather than declared at module level because `Frontend` exists only with the
    preview extra installed, and this module imports without it. Both renderers share it: the
    screenshot and the plot differ in what they frame, not in how they draw a character.
    """
    from ezdxf.addons.drawing import Frontend

    class CjkFrontend(Frontend):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.cjk_texts: list[Any] = []

        def draw_text_entity(self, entity: Any, properties: Any) -> None:
            if _has_cjk(entity.dxf.get("text", "")):
                self.cjk_texts.append(entity)
                return
            super().draw_text_entity(entity, properties)

        def draw_mtext_entity(self, entity: Any, properties: Any) -> None:
            if _has_cjk(entity.plain_text()):
                self.cjk_texts.append(entity)
                return
            super().draw_mtext_entity(entity, properties)

    return CjkFrontend


def _cjk_font_properties() -> Any:
    """Matplotlib FontProperties for an installed CJK face, or None.

    By file, not by family name: matplotlib's family lookup resolves a
    collection like msyh.ttc to a face it then cannot pull a CJK glyph out of
    ("failed to get glyph"), while an explicit file renders the same string
    correctly.
    """
    import matplotlib.font_manager as fm

    for name in ("Microsoft YaHei", "SimSun", "SimHei", "Noto Sans CJK SC", "Noto Sans SC"):
        try:
            path = fm.findfont(fm.FontProperties(family=name), fallback_to_default=False)
        except Exception:  # noqa: BLE001 - not installed is the normal case
            continue
        return fm.FontProperties(fname=path)
    return None


class DxfBackend(RecordingBackend):
    """Renders into an in-memory DXF document that can be saved and reopened."""

    name = "dxf"

    #: The one driver that writes paperspace: the page setup goes onto the file's own LAYOUT
    #: entity, which is where AutoCAD reads it, so a DXF saved here opens already configured.
    supports_sheet = True

    #: The one driver that writes a block definition from primitives. A definition is document
    #: state — ezdxf keeps it in the file's own BLOCKS table, beside the journal — so a captured
    #: element travels inside the drawing exactly as its catalogue entry does.
    supports_block_create = True

    #: Where the journal lives inside the file: root dictionary → ACADMCP_JOURNAL →
    #: JOURNAL. Same idea as the plugin's DWG journal: the steps travel with the drawing,
    #: so opening a saved file can still undo what a previous session drew.
    #: The name is a compound on purpose — a bare `ACADMCP` is claimed by other tooling in
    #: this space, which registers XDATA appids under that prefix and a root dictionary of
    #: its own. Two applications sharing one name is how a reader ends up reading the wrong
    #: record; a compound nobody else has picked cannot be misread.
    JOURNAL_DICT = "ACADMCP_JOURNAL"
    #: The name this package wrote before the above existed. Read, never written.
    LEGACY_JOURNAL_DICT = "ACADMCP"
    JOURNAL_KEY = "JOURNAL"
    #: A group-code-1 string tops out at 2049 bytes; JSON is escaped to ASCII first,
    #: so chunk by characters with margin.
    JOURNAL_CHUNK = 1900

    def __init__(
        self, path: str | None = None, template: str = "R2010", create: bool = False, **_: Any
    ) -> None:
        super().__init__()
        # Set before any read so a backend with no path still reports the field.
        self.journal_unreadable = ""
        if path and not create:
            try:
                self.doc = ezdxf.readfile(path)
            except OSError as exc:
                raise BackendError(f"cannot open DXF {path!r}: {exc}") from exc
        else:
            # create=True starts an empty document that *will live at* path: the
            # file is written on save, not read now — that is drawing_new's job.
            self.doc = ezdxf.new(template, setup=True)
            self.doc.units = ezdxf.units.MM
        self.text_style = CJK_STYLE
        self._ensure_style()
        self.msp = self.doc.modelspace()
        self.path = path
        self._ops = 0
        #: The sheet, once one is made: `sheet` is the reportable dict a tool returns and leaves
        #: the process as JSON, `_sheet_layout` is the layout object the frame is drawn on and
        #: never does. Two names because they are two things.
        self.sheet: dict[str, Any] = {}
        self._sheet_layout: Any = None
        #: The last plot's own numbers, so a tool can report the viewport it asked for.
        self.last_plot: dict[str, Any] = {}
        if path:
            self._read_journal()
            self._read_sheet()

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

    def ensure_layer(
        self,
        name: str,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
    ) -> Any:
        layer = self.doc.layers.get(name) if name in self.doc.layers else self.doc.layers.add(name)
        if color is not None:
            layer.color = int(color)
        if linetype and linetype != "CONTINUOUS":
            if linetype not in self.doc.linetypes:
                self.doc.linetypes.add(linetype, pattern=[0.5, 0.25, -0.25])
            layer.dxf.linetype = linetype
        if lineweight is not None:
            # DXF group 370 counts 1/100 mm, and so does AutoCAD's own line-weight list. The
            # conversion lives in one place so this driver and the live one cannot differ.
            layer.dxf.lineweight = lineweight_centimm(lineweight)
        return layer

    def set_units_mm(self) -> None:
        self.doc.units = ezdxf.units.MM
        self.doc.header["$INSUNITS"] = 4

    # --- sheets ------------------------------------------------------------
    def _sheet_target(self, wanted: str = "") -> Any:
        """The layout the sheet goes on: the named one, or the document's own paper layout.

        A stock document already has one paper layout beside Model, so it is renamed rather than
        added to — two paper layouts in one file is how a reader plots the wrong one.
        """
        name = (wanted or "").strip()
        if name and name in self.doc.layouts:
            return self.doc.layouts.get(name)
        paper_layouts = [layout for layout in self.doc.layouts if layout.name.upper() != "MODEL"]
        if not paper_layouts:
            return self.doc.layouts.new(name or "A3")
        target = paper_layouts[0]
        if not name or target.name == name:
            return target
        # `Layouts.rename`, not `Layout.rename`: the object method sets the LAYOUT entity's name
        # and leaves the ACAD_LAYOUT dictionary key alone, so the file carries a tab reading "A3"
        # under an entry called "Layout1" — and ezdxf keys its own layout table by that entry, so
        # reopening the file cannot find its own sheet by name. Measured on ezdxf 1.4.4.
        self.doc.layouts.rename(target.name, name)
        return self.doc.layouts.get(name)

    @staticmethod
    def _looks_like_a_page_setup(layout: Any, paper_name: str) -> bool:
        """Whether a layout carries the *evidence* of a page setup, not just its fields.

        A save/load round trip materialises ezdxf's own defaults onto every paper layout:
        `ezdxf.new("R2010")` has empty page-setup fields in memory, but a `Layout1` comes
        back from the file as an A3 at 1:1 with a paper width and height — so the paper
        fields alone cannot tell a sheet from a drawing that never had one, and a sheetless
        file reopened used to report a phantom A3 and even plot.

        The evidence is the layout's own **name**. `set_page_setup` renames the layout to
        its paper name (`Layouts.rename`, not `Layout.rename` — see `_sheet_target`), so a
        real sheet reopens as "A3" while the phantom keeps "Layout1". A caller that named
        the layout something else falls back on `plot_type == 1` ("plot the layout's own
        contents"), which the setup writes and ezdxf's default is not (the default is 5).
        Measured 2026-10-15 on ezdxf 1.4.4: the phantom comes back `plot_type=5`, margins
        7.5/20, `unit_factor=1.0`; a real 1:100 A3 comes back named "A3", `plot_type=1`,
        `unit_factor=100.0`.
        """
        if layout.name.strip().upper() == paper_name.strip().upper():
            return True
        return int(layout.dxf.get("plot_type", 0) or 0) == 1

    @classmethod
    def _sheet_from_layout(cls, layout: Any) -> dict[str, Any]:
        """The reportable sheet a layout describes, or {} when it is not set up as one."""
        name = str(layout.dxf.get("paper_size", "") or "").strip().upper()
        width = layout.dxf.get("paper_width", None)
        height = layout.dxf.get("paper_height", None)
        if name not in sheet.PAPERS or not width or not height:
            return {}
        if not cls._looks_like_a_page_setup(layout, name):
            return {}
        landscape = (float(width), float(height)) == sheet.PAPERS[name]
        numerator = int(layout.dxf.get("scale_numerator", 1) or 1)
        denominator = int(layout.dxf.get("scale_denominator", 1) or 1)
        return {
            "paper": name,
            "orientation": "landscape" if landscape else "portrait",
            "width": float(width),
            "height": float(height),
            "scale": f"{numerator}:{denominator}",
            "layout": layout.name,
            "style_sheet": str(layout.dxf.get("current_style_sheet", "") or ""),
        }

    def _read_sheet(self) -> None:
        """Adopt the sheet a reopened drawing already carries, so a second session can plot it."""
        for layout in self.doc.layouts:
            if layout.name.upper() == "MODEL":
                continue
            found = self._sheet_from_layout(layout)
            if found:
                self.sheet = found
                self._sheet_layout = layout
                return

    def set_page_setup(
        self, paper: Any, scale: tuple[int, int], *, layout: str = "", style_sheet: str = ""
    ) -> dict[str, Any]:
        """Write the page setup onto the layout, which is where AutoCAD reads a sheet from."""
        target = self._sheet_target(layout or paper.name)
        numerator, denominator = int(scale[0]), int(scale[1])
        target.dxf.paper_size = paper.name
        target.dxf.paper_width = paper.width
        target.dxf.paper_height = paper.height
        target.dxf.plot_paper_units = 1  # millimetres
        # group 76: which plot area — 1 is "extents" (0 display, 2 limits, 3 view, 4 window,
        # 5 layout). ezdxf's own round-trip default here is 5, so writing 1 is also one of
        # the marks `_looks_like_a_page_setup` reads as evidence of a real setup.
        target.dxf.plot_type = 1
        target.dxf.plot_rotation = 0
        target.dxf.scale_numerator = numerator
        target.dxf.scale_denominator = denominator
        target.dxf.unit_factor = denominator / numerator
        known = STANDARD_SCALE_TYPES.get((numerator, denominator))
        if known is not None:
            target.dxf.standard_scale_type = known
        # A ratio with no enum row is *not* given one: group 147 alone is what a plot falls back
        # to, and a wrong group 75 is worse than an absent one.
        target.dxf.left_margin = paper.binding
        target.dxf.bottom_margin = paper.margin
        target.dxf.right_margin = paper.margin
        target.dxf.top_margin = paper.margin
        if style_sheet:
            target.dxf.current_style_sheet = style_sheet
        self._sheet_layout = target
        self.sheet = self._sheet_from_layout(target)
        return dict(self.sheet)

    def save(self, path: str | None = None) -> str:
        target = path or self.path
        if not target:
            raise BackendError("no output path given and the document has none")
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            self._write_journal()
            self.doc.saveas(target)
        except OSError as exc:
            # Unwrapped, this is an OSError and the SDK masks it behind a bare "Error
            # executing tool drawing_save" — the same silence `_read_journal` refuses for a
            # read. The path and the reason are the whole recovery signal.
            raise BackendError(f"cannot write DXF {target!r}: {exc}") from exc
        self.path = target
        return target

    def preview(self, path: str, theme: str = "dark") -> str | None:
        """Render to PNG so the drawing can be eyeballed without opening AutoCAD."""
        return self.screenshot(path, theme=theme)

    def paper_primitives(self, primitives: list[dict[str, Any]]) -> int:
        """Record the sheet's primitives into the open step, marked for paperspace.

        Recorded rather than drawn: a step defers everything to its commit, and that is what
        makes the frame, the title block and the viewport one undoable step. The `paper` flag
        travels in the journal with them, so `_apply` draws them on the layout and a replay
        onto a backend that has no paperspace is reported rather than silently dropped.
        """
        if self._sheet_layout is None:
            raise BackendError(
                "this drawing has no sheet to draw on: call set_page_setup first, or open a "
                "drawing saved with one"
            )
        for prim in primitives:
            fields = {k: v for k, v in prim.items() if k not in ("kind", "points", "paper")}
            self._record(prim["kind"], prim["points"], paper=True, **fields)
        return len(primitives)

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
            from ezdxf.addons.drawing import RenderContext
            from ezdxf.addons.drawing.config import ColorPolicy, Configuration
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendError("screenshot needs matplotlib: uv sync --extra preview") from exc

        bg, fg = ("#1e1e1e", "#ffffff") if theme == "dark" else ("#ffffff", "#000000")
        dpi = 100
        fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
        ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
        ax.set_axis_off()
        context = RenderContext(self.doc)
        # Every entity is forced to the foreground colour: the layer palette is a
        # drafting convention, not data, and either background makes half of it
        # illegible (yellow on white, dark blue on dark). The render is for
        # eyeballing, so legibility wins over colour fidelity.
        policy = ColorPolicy.WHITE if theme == "dark" else ColorPolicy.BLACK
        # pdsize=1 only pre-answers a question the Frontend would otherwise warn
        # about ("relative point size is not supported") on every render.
        config = Configuration(color_policy=policy, pdsize=1)
        frontend = _cjk_frontend_class()(context, MatplotlibBackend(ax), config=config)
        frontend.draw_layout(self.msp, finalize=True)
        if frontend.cjk_texts:
            # The deferred texts must still count towards the framing, or a
            # title sitting below the plan gets cropped out of a full shot.
            ax.update_datalim([pt for ent in frontend.cjk_texts for pt in _text_frame(ent)])
            ax.autoscale(True)
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
        if frontend.cjk_texts:
            # Only now is the data-to-points mapping known (it depends on the
            # figure size and the limits), so the deferred texts can be sized.
            # The axes fills the figure, so the whole vertical span is
            # fig_h points tall and one drawing unit is that divided by span_y.
            axis_points = (fig_h / dpi) * 72.0
            _draw_cjk_texts(frontend.cjk_texts, ax, fg, points_per_unit=axis_points / span_y)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, facecolor=bg, dpi=dpi)
        plt.close(fig)
        return path

    def plot_pdf(
        self, path: str, *, paper: Any, scale: tuple[int, int], theme: str = "light"
    ) -> str:
        """Render the sheet to a PDF the size of its paper, with the plan at the plot scale.

        Two axes, and neither computes a transform by hand. The figure's size *is* the paper, so
        the PDF's own `/MediaBox` is the paper by construction and can be measured against what
        was asked for. The frame and the title block are drawn into an axes spanning the whole
        page in paper millimetres, so they come out at 1:1. The plan is drawn into an axes sitting
        exactly on the viewport rectangle, with its data limits set to the model window that
        viewport shows — so the axes' own limits *are* the plot scale, and its own clipping is the
        viewport's edge. Nothing can be scaled twice.
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
            from ezdxf.addons.drawing import RenderContext
            from ezdxf.addons.drawing.config import ColorPolicy, Configuration
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendError("plotting needs matplotlib: uv sync --extra preview") from exc

        if self._sheet_layout is None:
            raise BackendError(
                "this drawing has no sheet to plot: call set_page_setup first, or open a "
                "drawing saved with one"
            )
        page, ink = ("#ffffff", "#000000") if theme == "light" else ("#1e1e1e", "#ffffff")
        figure = plt.figure(
            figsize=(sheet.mm_to_pt(paper.width) / 72.0, sheet.mm_to_pt(paper.height) / 72.0)
        )
        figure.patch.set_facecolor(page)
        config = Configuration(
            color_policy=ColorPolicy.BLACK if theme == "light" else ColorPolicy.WHITE, pdsize=1
        )
        # Points per drawing millimetre. Both axes fill their rectangle exactly and the figure
        # *is* the paper, so one paper millimetre is always this many points; the plan's own texts
        # are this much smaller because at 1:100 a model millimetre is a hundredth of a paper one.
        points_per_mm = sheet.mm_to_pt(1.0)

        # The sheet at 1:1 in paper millimetres: the frame and the title block. The VIEWPORT is
        # filtered out — it is a window onto the model, not something a plot draws.
        sheet_axes = figure.add_axes((0.0, 0.0, 1.0, 1.0))
        sheet_axes.set_axis_off()
        sheet_axes.set_facecolor(page)
        sheet_axes.set_xlim(0.0, paper.width)
        sheet_axes.set_ylim(0.0, paper.height)
        sheet_frontend = _cjk_frontend_class()(
            RenderContext(self.doc), MatplotlibBackend(sheet_axes), config=config
        )
        sheet_frontend.draw_layout(
            self._sheet_layout, finalize=False, filter_func=lambda e: e.dxftype() != "VIEWPORT"
        )

        # The plan, inside the viewport: these limits are the plot scale. The vertical span is
        # `view_height_model` and the horizontal follows from the viewport's own proportions,
        # which is the rule AutoCAD plots by — the viewport fixes the scale, its shape fixes what
        # else is in shot. Stretching both spans to the box would print a plan of the wrong
        # proportions, which is the one error a reader would not catch by eye.
        left, bottom, right, top = sheet.viewport_rect(paper)
        plan_axes = figure.add_axes(
            (
                left / paper.width,
                bottom / paper.height,
                (right - left) / paper.width,
                (top - bottom) / paper.height,
            )
        )
        plan_axes.set_axis_off()
        plan_axes.set_facecolor(page)
        span_y = sheet.view_height_model(paper, scale)
        span_x = span_y * (right - left) / (top - bottom)
        center_x, center_y = self._plan_center()
        plan_axes.set_xlim(center_x - span_x / 2.0, center_x + span_x / 2.0)
        plan_axes.set_ylim(center_y - span_y / 2.0, center_y + span_y / 2.0)
        plan_frontend = _cjk_frontend_class()(
            RenderContext(self.doc), MatplotlibBackend(plan_axes), config=config
        )
        plan_frontend.draw_layout(self.msp, finalize=False)

        _draw_cjk_texts(sheet_frontend.cjk_texts, sheet_axes, ink, points_per_unit=points_per_mm)
        _draw_cjk_texts(
            plan_frontend.cjk_texts,
            plan_axes,
            ink,
            points_per_unit=points_per_mm * scale[0] / scale[1],
        )

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path)
        plt.close(figure)
        self.last_plot = {
            "path": path,
            "paper_mm": [paper.width, paper.height],
            "scale": f"{scale[0]}:{scale[1]}",
            "viewport_mm": [round(right - left, 3), round(top - bottom, 3)],
            "plan_span_mm": [round(span_x, 3), round(span_y, 3)],
        }
        return path

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "path": self.path,
            "units": "mm",
            "entities": sum(1 for _ in self._all_entities()),
            "layers": sorted(layer.dxf.name for layer in self.doc.layers),
            "ops": self._ops,
            "journal": {
                "steps": len(self.journal.steps),
                "applied": sum(s.state == "applied" for s in self.journal.steps),
            },
            "journal_unreadable": self.journal_unreadable,
            "catalog": self.recorded_catalog(),
            # Empty until a sheet is made: a drawing with no page setup is a plan, not a sheet.
            "sheet": dict(self.sheet),
        }

    def recorded_catalog(self) -> dict[str, Any] | None:
        """The catalogue identity this drawing carries, as a summary, or None.

        A summary rather than the whole payload: `info()` is what a caller reads to see the
        state of a drawing, and the entries themselves are already reachable through
        :meth:`embedded_catalog`. The digest is what makes "is this the same catalogue?" a
        one-line question.
        """
        payload = self.journal.meta.get("catalog")
        if not isinstance(payload, dict):
            return None
        return {
            "library": str(payload.get("library", "")),
            "count": payload.get("count"),
            "digest": str(payload.get("digest", "")),
        }

    def embedded_catalog(self) -> Catalog | None:
        """The catalogue that drew this file, rebuilt from the journal, or None.

        None when the drawing carries none — a file saved before the catalogue was recorded,
        or one drawn with no catalogue loaded at all. Adopting is tolerant, so a damaged
        embedded copy yields a partly-read catalogue with its `skipped` lines rather than an
        exception, the same rule the journal and the loader follow.
        """
        payload = self.journal.meta.get("catalog")
        if not isinstance(payload, dict) or not payload:
            return None
        return Catalog.adopt(payload)

    def entities(self, layer: str | None = None, limit: int = 200) -> list[dict]:
        """Every entity the drawing holds, model and paper alike.

        The sheet is part of the drawing, so a listing that stopped at the modelspace would
        report a frame the journal recorded as missing and a sheet's text as never drawn. The
        model comes first, which is the order a tag scan and a reader both expect.
        """
        out: list[dict] = []
        for e in self._all_entities():
            if layer and e.dxf.get("layer", "0") != layer:
                continue
            row: dict[str, Any] = {"type": e.dxftype(), "layer": e.dxf.get("layer", "0")}
            # Text has to come back, or `ops.existing_tags` sees an empty drawing and
            # restarts tag numbering at 1 on every call — so two runs of placed elements
            # would share tags M-1, M-2. Both TEXT and MTEXT carry their content in group
            # code 1.
            if e.dxftype() in ("TEXT", "MTEXT"):
                row["text"] = e.dxf.get("text", "")
            if e.dxftype() == "INSERT":
                # Reported under both names for parity with the plugin's `entities`: a
                # dynamic block can resolve to an anonymous `*U` record, so neither name
                # is on its own the one that was asked for. ezdxf resolves both to the
                # reference's own name here.
                row["block"] = row["definition"] = e.dxf.get("name", "")
                # Where the reference landed. Without this a caller could place an element
                # and still have to save the DXF and read it with ezdxf to confirm *where*
                # it went — the one question the read-only surface could not answer about
                # its own placement. Placement keys are added to INSERT rows only; every
                # other row's shape is unchanged.
                row["at"] = [float(e.dxf.insert[0]), float(e.dxf.insert[1])]
                row["rotation"] = float(e.dxf.get("rotation", 0.0))
                row["xscale"] = float(e.dxf.get("xscale", 1.0))
                row["yscale"] = float(e.dxf.get("yscale", 1.0))
            out.append(row)
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
        """Place a block reference, where this drawing already holds the definition.

        The dxf backend cannot read a library .dwg, so `source` cannot bring a definition in
        — but a definition an earlier :meth:`create_block` wrote into *this* drawing can be
        placed, which is how a captured element reaches the paper. A name the drawing has not
        got is refused rather than substituted: drawing a parametric twin in place of the
        block that was asked for is a right-looking element of the wrong identity, which is
        the failure the whole catalogue exists to prevent.
        """
        if block not in self.doc.blocks:
            raise BackendError(
                "the dxf backend cannot import block definitions; capture the element first "
                "(element_capture) or place it on the plugin backend"
            )
        if properties:
            # ezdxf has no dynamic-block API, so a size given here would be dropped without a
            # word — a right-looking element of the wrong size, which the catalogue exists to
            # prevent. A captured block is plain geometry and needs no parameters.
            raise BackendError(
                "the dxf backend cannot set a dynamic block's parameters: ezdxf has no "
                "dynamic block API, so they would be silently dropped. Place a captured "
                "element, or set the parameters in AutoCAD"
            )
        # Hybrid like every other primitive: eager outside a step, recorded inside one, so a
        # captured element's reference is one undoable step like any other entity.
        if self.open_step:
            return self._record(
                "insert",
                [at],
                layer=layer,
                block=block,
                rotation=rotation,
                # Both scales always travel, so no reader has to guess whether a missing one
                # means 1.0 — the shape the plugin's wire format already uses.
                scale_x=scale if scale_x is None else scale_x,
                scale_y=scale if scale_y is None else scale_y,
            )
        return self._draw_insert(
            block,
            at,
            rotation=rotation,
            scale_x=scale if scale_x is None else scale_x,
            scale_y=scale if scale_y is None else scale_y,
            layer=layer,
        )

    def _draw_insert(
        self,
        block: str,
        at: Point,
        *,
        rotation: float = 0.0,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        layer: str = "0",
    ) -> Any:
        self._ops += 1
        reference = self.msp.add_blockref(str(block), xy(at))
        reference.dxf.rotation = float(rotation)
        reference.dxf.layer = layer
        reference.dxf.xscale = float(scale_x)
        reference.dxf.yscale = float(scale_y)
        return reference

    def _import_published_block(self, name: str) -> None:
        """Bring a published element's definition in, the way `insert_captured` does.

        Delegates to the catalogue's own fragment importer rather than reimplementing the
        lookup: one place knows how an entry's `geometry_file` resolves against the catalogue
        that carries it, and it is the same call a fresh `place_element` makes. A no-op (and
        never reached in the normal path) when the drawing already holds the definition; it
        is the replay path that needs it, because a definition is document state and is not
        among the primitives a step records.
        """
        from .. import catalog_ops

        catalog_ops._import_fragment_if_needed(self, name)

    def create_block(
        self, name: str, primitives: list[dict[str, Any]], base_point: Point
    ) -> dict[str, Any]:
        """Write a block definition from recorded primitives, `base_point` its origin.

        A definition is **document state**, not a step's to own: this is called directly,
        never through a recorded draw method, so nothing journals it and a rollback never
        erases it — the same rule the page setup and the plugin's `EnsureBlock` follow. The
        step that later places a reference to it is an ordinary journalled step, and rolling
        that step back leaves the definition standing.

        The primitives are the journal's own, in the vocabulary :meth:`_apply` takes, with
        every point translated so `base_point` becomes the origin. A name the drawing already
        holds is refused rather than replaced: other references point at that definition, and
        silently redefining it would move geometry somebody else placed.
        """
        name = str(name or "").strip()
        if not name:
            raise ValueError("a block definition needs a name")
        primitives = [dict(p) for p in primitives]
        if not primitives:
            raise ValueError(f"block {name!r} would be empty; there was nothing to capture")
        for prim in primitives:
            # A frame's coordinates are paper millimetres, drawn at 1:1 on a layout; a block is
            # model geometry by definition, so sheet furniture in one is a category error.
            if prim.get("paper"):
                raise BackendError(
                    f"block {name!r} cannot hold sheet furniture: a {prim.get('kind', '?')} "
                    "primitive marked 'paper' is drawn in paper millimetres, and a block is "
                    "model geometry"
                )
        if name in self.doc.blocks:
            raise BackendError(
                f"this drawing already has a block definition named {name!r}; a capture does "
                "not replace one that other references may point at"
            )

        block = self.doc.blocks.new(name)
        origin_x, origin_y = xy(base_point)
        for prim in primitives:
            self._apply_on(block, self._rebase_primitive(prim, origin_x, origin_y))
        return {"block": name, "entities": len(primitives)}

    @staticmethod
    def _rebase_primitive(prim: dict[str, Any], origin_x: float, origin_y: float) -> dict[str, Any]:
        """One primitive with every point moved so `(origin_x, origin_y)` becomes the origin.

        Points travel flat in the journal, so they are rebuilt flat: the `_draw_*` methods
        unflatten them again, and the two must agree or the definition lands somewhere else.
        """
        if origin_x == 0.0 and origin_y == 0.0:
            return prim
        points = unflatten_points(prim.get("points", []))
        return {
            **prim,
            "points": flatten_points([(x - origin_x, y - origin_y) for x, y in points]),
        }

    # --- journal application ------------------------------------------------
    def _apply_primitive(self, prim: dict[str, Any]) -> Any:
        """Draw one primitive where it belongs: the model, or the sheet's paperspace.

        A primitive marked `paper` is the frame, the title block or the viewport, and belongs on
        the layout at 1:1 in paper millimetres. Every `_draw_*` method below reads `self.msp` and
        nothing else, so the destination is swapped around them rather than threaded through
        eight signatures — the drawing code stays destination-agnostic, which is what lets a
        replay draw the same primitive somewhere else.
        """
        if prim.get("paper"):
            if self._sheet_layout is None:
                raise BackendError(
                    "this drawing records a sheet's frame but carries no sheet: it was drawn "
                    "before set_page_setup or the file lost its page setup. Call set_page_setup"
                )
            if prim["kind"] == "viewport" and "view_center" not in prim:
                # Where to look is decided *before* the destination is swapped, because
                # `_plan_center` reads whatever `self.msp` is: resolved after the swap it would
                # measure the frame and the title block, and the viewport would frame the sheet
                # instead of the plan on it. Copied rather than written in, so no drawing's own
                # extent is ever journalled as another drawing's.
                prim = {**prim, "view_center": list(self._plan_center())}
            return self._apply_on(self._sheet_layout, prim)
        return self._apply_on(self.msp, prim)

    def _apply_on(self, layout: Any, prim: dict[str, Any]) -> Any:
        saved = self.msp
        self.msp = layout
        try:
            return self._apply_primitive_here(prim)
        finally:
            self.msp = saved

    def _apply_primitive_here(self, prim: dict[str, Any]) -> Any:
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
        if kind == "insert":
            # A captured element's reference, placed from a definition this drawing already
            # holds. A definition it has not got is refused here, in the applier, so replaying
            # another drawing's library insert onto this backend is *reported* by
            # `drawing_replay` rather than raising out of ezdxf unmasked.
            name = str(prim["block"])
            if name not in self.doc.blocks:
                # A capture's definition is document state and is not journalled, so
                # replaying a `place_element` step for a *published* element onto a fresh
                # drawing finds the INSERT but not the block it references. The op layer's
                # `insert_captured` resolves that by importing the published geometry
                # fragment before it inserts; replay applies the primitives directly and
                # never reaches that, so the same import is attempted here — the catalogue
                # and its fragments resolve from the session exactly as a fresh placement
                # does. A name the catalogue still cannot resolve is refused below.
                self._import_published_block(name)
            if name not in self.doc.blocks:
                raise BackendError(
                    f"the 'dxf' backend cannot apply an insert of {name!r}: this drawing has no "
                    "such block definition, and ezdxf cannot read a library .dwg to import one. "
                    "Capture the element on this backend first, or place it on the plugin backend"
                )
            return self._draw_insert(
                name,
                pts[0],
                rotation=float(prim.get("rotation", 0.0)),
                scale_x=float(prim.get("scale_x", 1.0)),
                scale_y=float(prim.get("scale_y", 1.0)),
                layer=prim.get("layer", "0"),
            )
        if kind == "block":
            # A capture's recorded intent. The definition is document state, so applying this
            # only makes sure it is there — idempotent, and nothing of the model's comes out,
            # which is why `_apply` records no handle for it and a rollback cannot erase it.
            # Replaying a capture onto a fresh drawing is what creates the definition there.
            name = str(prim["name"])
            if name not in self.doc.blocks:
                self.create_block(
                    name, list(prim.get("primitives", [])), pts[0] if pts else (0.0, 0.0)
                )
            return None
        if kind == "viewport":
            return self._draw_viewport(pts[0], prim)
        raise BackendError(
            f"the 'dxf' backend cannot apply primitive kind {kind!r}: it cannot read a "
            "library .dwg to copy a block definition from. Use backend='plugin'."
        )

    def _draw_viewport(self, center: Point, prim: dict[str, Any]) -> Any:
        """Frame the plan on the sheet: one viewport, sized in paper mm and scaled in model mm.

        `view_height` in model units is the whole of the plot scale — a 1:100 sheet shows 100
        model millimetres per paper millimetre — and it comes from the `sheet` module rather than
        from a constant here. `view_center` is always resolved by `_apply_primitive` before the
        destination is swapped, so a building drawn away from the origin is still centred on its
        sheet and a replay frames the target drawing's own content.
        """
        self._ops += 1
        return self._sheet_layout.add_viewport(
            center=xy(center),
            size=(float(prim["size"][0]), float(prim["size"][1])),
            view_center_point=xy(prim["view_center"]),
            view_height=float(prim["view_height"]),
            status=2,  # on and active: what a plot of this layout shows
        )

    def _plan_extent(self) -> tuple[float, float, float, float] | None:
        """(min_x, min_y, max_x, max_y) of what the model holds, or None when it holds nothing.

        Measured off the entities rather than read from `$EXTMIN`/`$EXTMAX`, which ezdxf only
        refreshes on its own terms.
        """
        xs: list[float] = []
        ys: list[float] = []
        for entity in self.msp:
            for key in ("start", "end", "center", "insert"):
                if not entity.dxf.hasattr(key):
                    continue
                value = entity.dxf.get(key)
                if value is not None:
                    xs.append(float(value[0]))
                    ys.append(float(value[1]))
            getter = getattr(entity, "get_points", None)
            if getter is None:
                continue
            try:
                for point in getter("xy"):
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
            except (TypeError, ValueError):  # a point layout this method cannot read
                continue
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def plan_extent(self) -> tuple[float, float, float, float] | None:
        return self._plan_extent()

    def _plan_center(self) -> Point:
        """The middle of what the model holds, so the viewport lands on the plan.

        An empty model centres on the origin, which is what an empty sheet should show.
        """
        extent = self._plan_extent()
        if extent is None:
            return (0.0, 0.0)
        min_x, min_y, max_x, max_y = extent
        return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)

    def _apply(self, primitives: list[dict[str, Any]]) -> list[str]:
        handles: list[str] = []
        for prim in primitives:
            entity = self._apply_primitive(prim)
            # A `block` intent yields nothing of the model's, so it contributes no handle:
            # the definition it ensures is document state, and an undo that cannot erase it
            # is the point. Everything else draws an entity and is journalled by handle.
            if entity is not None:
                handles.append(entity.dxf.handle)
        return handles

    def _paper_layouts(self) -> list[Any]:
        """Every layout but the model, in the order the file lists them.

        The sheet lives here rather than in the modelspace, and it is part of the drawing, so
        both the entity listing and the erase path have to know about it — a paperspace entity
        deleted through the modelspace raises `ValueError: list.remove(x): x not in list`.
        """
        return [layout for layout in self.doc.layouts if layout.name.upper() != "MODEL"]

    def _all_entities(self) -> Iterator[Any]:
        yield from self.msp
        for layout in self._paper_layouts():
            yield from layout

    def _erase(self, handles: list[str]) -> tuple[int, int]:
        erased = missing = 0
        for handle in handles:
            entity = self.doc.entitydb.get(handle)
            if entity is None or not entity.is_alive:
                missing += 1  # a hand edit already removed it; count, don't fail
                continue
            # Erased through the layout that owns it: an entity lives in the modelspace or in a
            # paper block record, and only its own space will let go of it. A sheet's frame and
            # viewport are paper entities, so aiming every erase at the modelspace would leave
            # them standing and report them missing.
            self.doc.layouts.get_layout_for_entity(entity).delete_entity(entity)
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
        self._drop_legacy_journal()

    def _drop_legacy_journal(self) -> None:
        """Move a pre-rename record out of the way once this build has rewritten it.

        Leaving it would put two JOURNAL records in one file, and a reader that prefers the
        old name — an older build, or this one if the lookup order were ever flipped — would
        read steps this build has already replaced. Only our own key moves: a dictionary of
        that name holding anyone else's data is theirs and stays, and it is discarded only
        when ours was the only thing in it.
        """
        legacy = self.doc.rootdict.get(self.LEGACY_JOURNAL_DICT)
        if legacy is None:
            return
        if self.JOURNAL_KEY in legacy:
            legacy.discard(self.JOURNAL_KEY)
        if not len(legacy):
            self.doc.rootdict.discard(self.LEGACY_JOURNAL_DICT)

    def _find_journal_record(self) -> Any:
        """Our record, under the current name and then under the one we used to write."""
        for name in (self.JOURNAL_DICT, self.LEGACY_JOURNAL_DICT):
            dictionary = self.doc.rootdict.get(name)
            if dictionary is None:
                continue
            record = dictionary.get(self.JOURNAL_KEY)
            if record is not None:
                return record
        return None

    def _read_journal(self) -> None:
        self.journal_unreadable = ""
        xrecord = self._find_journal_record()
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
