"""The sheet: paper, scale, frame, title block, and the read-back that checks them.

A plan drawn in model space is a plan; a drawing is a plan on paper of a named size, at a
named scale, inside a frame with a title block. This module is the data and the maths for
that — no CAD library, and from this package only :mod:`acadmcp.layers`, which is where the
measured character width already lives — so the same numbers reach the file through a backend
and the PDF through the renderer.

The sheet sizes are the ISO A series, which is what GB/T 50001 takes them from. The margins
follow GB/T 14689 — a 25 mm bound edge on every size, 10 mm on the other three for A0 to A2
and 5 mm for A3 and A4 — and the title block is this package's defaults for a Chinese
architectural sheet: 180 mm wide and four rows tall in the bottom-right corner of the frame.
The title block is a default and not law — a drawing office has its own, the same way it has
its own layer standard.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from .layers import CJK_ADVANCE, LATIN_ADVANCE, text_width

Point = tuple[float, float]

__all__ = [
    "BINDING_MARGIN",
    "CJK_ADVANCE",
    "EDGE_MARGINS",
    "LATIN_ADVANCE",
    "PAPERS",
    "Paper",
    "SCALES",
    "TITLE_BLOCK_HEIGHT",
    "TITLE_BLOCK_ROWS",
    "TITLE_BLOCK_TEXT",
    "TITLE_BLOCK_WIDTH",
    "VIEWPORT_GAP",
    "fits_on_sheet",
    "frame_outline",
    "largest_scale_that_fits",
    "mm_to_pt",
    "model_window",
    "read_mediabox",
    "read_pdf_mediabox_mm",
    "resolve_paper",
    "resolve_scale",
    "text_width",
    "title_block",
    "view_height_model",
    "viewport_rect",
]

#: Sheet sizes in millimetres, landscape (width, height).
PAPERS: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
}

#: The plot scales this package offers, from GB/T 50001's tables: 1:1 and the enlargements
#: are here for a detail, 1:250 and 1:1000 for a site plan. `resolve_scale` accepts any
#: ratio — this is the menu `largest_scale_that_fits` picks from, not a whitelist.
SCALES: dict[str, tuple[int, int]] = {
    "1:1": (1, 1),
    "2:1": (2, 1),
    "1:5": (1, 5),
    "1:10": (1, 10),
    "1:20": (1, 20),
    "1:25": (1, 25),
    "1:50": (1, 50),
    "1:100": (1, 100),
    "1:150": (1, 150),
    "1:200": (1, 200),
    "1:250": (1, 250),
    "1:500": (1, 500),
    "1:1000": (1, 1000),
}

#: The bound edge, the same on every size, and the other three: GB/T 14689 gives 10 mm down
#: to A2 and 5 mm on A3 and A4, where a uniform 10 would eat a tenth of an A4's width.
BINDING_MARGIN = 25.0
EDGE_MARGINS: dict[str, float] = {"A0": 10.0, "A1": 10.0, "A2": 10.0, "A3": 5.0, "A4": 5.0}

#: The title block: 180 mm wide, four rows, in the bottom-right corner of the frame. Four
#: rows, not three, so the scale the sheet claims always has a row beside the caller's
#: 图名/图号/张次 — a plotted sheet without its scale in the title block fails its own point.
TITLE_BLOCK_WIDTH = 180.0
TITLE_BLOCK_HEIGHT = 56.0
TITLE_BLOCK_ROWS = 4
TITLE_BLOCK_TEXT = 5.0
#: The field-name column, as a fraction of the block's width.
TITLE_BLOCK_LABEL_FRACTION = 0.28
#: Clearance between a written field and its cell's ruling.
TITLE_BLOCK_PADDING = 2.0

#: The gap between the frame and what it holds, so a wall never touches the frame line.
VIEWPORT_GAP = 5.0

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0

#: `/MediaBox` is `[x0 y0 x1 y1]` in points. Read out of the bytes rather than through a PDF
#: library: the point of the read-back is that it does not share code with whatever wrote the
#: file, and a PDF's page box is written plainly enough for that to be possible.
_MEDIABOX = re.compile(rb"/MediaBox\s*\[\s*([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)")


@dataclass(frozen=True, slots=True)
class Paper:
    """One sheet of paper, with the margins its frame is drawn inside."""

    name: str
    width: float
    height: float
    binding: float
    margin: float
    landscape: bool

    @property
    def title_block_origin(self) -> Point:
        """The bottom-left corner of the title block: against the frame, bottom-right."""
        return (self.width - self.margin - TITLE_BLOCK_WIDTH, self.margin)


def resolve_paper(name: str, orientation: str = "landscape") -> Paper:
    """The named sheet, landscape unless asked otherwise.

    A size this package has no row for is refused rather than rounded to the nearest one: a
    plan plotted on the wrong paper is discovered at the printer.
    """
    key = (name or "").strip().upper()
    if key not in PAPERS:
        raise ValueError(f"unknown paper {name!r}; this package draws {', '.join(sorted(PAPERS))}")
    width, height = PAPERS[key]
    facing = (orientation or "landscape").strip().lower()
    if facing not in ("landscape", "portrait"):
        raise ValueError(f"orientation must be 'landscape' or 'portrait', got {orientation!r}")
    landscape = facing == "landscape"
    if not landscape:
        width, height = height, width
    return Paper(key, width, height, BINDING_MARGIN, EDGE_MARGINS[key], landscape)


def resolve_scale(label: str) -> tuple[int, int]:
    """``"1:100"`` as (1, 100). A label that is not a ratio is refused, not parsed loosely."""
    text = (label or "").strip().replace(" ", "")
    match = re.fullmatch(r"(\d+):(\d+)", text)
    if not match:
        raise ValueError(
            f"scale must be a ratio like '1:100', got {label!r}; "
            f"this package knows {', '.join(sorted(SCALES))}"
        )
    numerator, denominator = int(match.group(1)), int(match.group(2))
    if numerator < 1 or denominator < 1:
        raise ValueError(f"scale must be positive, got {label!r}")
    return numerator, denominator


def frame_outline(paper: Paper) -> list[tuple[Point, Point]]:
    """The four sides of the drawing frame, inside the paper by its margins."""
    left, bottom = paper.binding, paper.margin
    right, top = paper.width - paper.margin, paper.height - paper.margin
    return [
        ((left, bottom), (right, bottom)),
        ((right, bottom), (right, top)),
        ((right, top), (left, top)),
        ((left, top), (left, bottom)),
    ]


def title_block(
    paper: Paper, meta: dict[str, str]
) -> tuple[list[tuple[Point, Point]], list[tuple[Point, str, float, str]]]:
    """The title block's rulings and its written fields.

    Fields are laid out in rows from the top of the block down, field name in the left column
    and value in the right — 图名 | 生产车间平面图, 图号 | JS-01 — and a value too long for its cell
    is shrunk to fit rather than allowed to cross the frame, the rule room names and area
    labels already follow. A field past the last row is dropped: a title block has rows, and
    squeezing a fourth into three would overlay the two above it.
    """
    origin_x, origin_y = paper.title_block_origin
    right = origin_x + TITLE_BLOCK_WIDTH
    top = origin_y + TITLE_BLOCK_HEIGHT
    lines: list[tuple[Point, Point]] = [
        ((origin_x, origin_y), (right, origin_y)),
        ((right, origin_y), (right, top)),
        ((right, top), (origin_x, top)),
        ((origin_x, top), (origin_x, origin_y)),
    ]
    row_height = TITLE_BLOCK_HEIGHT / TITLE_BLOCK_ROWS
    for row in range(1, TITLE_BLOCK_ROWS):
        y = origin_y + row * row_height
        lines.append(((origin_x, y), (right, y)))
    split = origin_x + TITLE_BLOCK_WIDTH * TITLE_BLOCK_LABEL_FRACTION
    lines.append(((split, origin_y), (split, top)))

    texts: list[tuple[Point, str, float, str]] = []
    for row, (name, value) in enumerate(list(meta.items())[:TITLE_BLOCK_ROWS]):
        y = top - (row + 0.5) * row_height
        texts.append(
            (
                (origin_x + TITLE_BLOCK_PADDING, y),
                str(name),
                _fits(str(name), split - origin_x - 2 * TITLE_BLOCK_PADDING),
                "left",
            )
        )
        texts.append(
            (
                (split + TITLE_BLOCK_PADDING, y),
                str(value),
                _fits(str(value), right - split - 2 * TITLE_BLOCK_PADDING),
                "left",
            )
        )
    return lines, texts


def _fits(text: str, width: float) -> float:
    """The title-block text height at which `text` fits across `width`."""
    per_unit = text_width(text, 1.0)
    if per_unit <= 0 or width <= 0:
        return TITLE_BLOCK_TEXT
    return min(TITLE_BLOCK_TEXT, width / per_unit)


def viewport_rect(paper: Paper) -> tuple[float, float, float, float]:
    """Where the plan is shown: the frame's full width, above the title block.

    (left, bottom, right, top) in paper millimetres. The title block owns the bottom-right
    corner, so the viewport wraps it in an L and takes the strip above it — the larger of the
    two rectangles the corner leaves, and the landscape shape a floor plan usually is. Taking
    the strip *left of* the block instead would give a portrait viewport on a landscape sheet,
    and on a portrait A4 a negative one: 210 mm of paper minus 180 mm of title block.
    """
    left = paper.binding + VIEWPORT_GAP
    bottom = paper.margin + TITLE_BLOCK_HEIGHT + VIEWPORT_GAP
    right = paper.width - paper.margin - VIEWPORT_GAP
    top = paper.height - paper.margin - VIEWPORT_GAP
    return (left, bottom, right, top)


def view_height_model(paper: Paper, scale: tuple[int, int]) -> float:
    """The model height a viewport of this size shows at this scale.

    This is the one relationship that makes a sheet a sheet: at 1:100 one paper millimetre is
    100 model millimetres, so a 267 mm tall viewport on an A3 shows 26.7 m. Get it wrong and
    the plan is the right shape at the wrong size, which nobody notices until it is measured.
    """
    _left, bottom, _right, top = viewport_rect(paper)
    return (top - bottom) * scale[1] / scale[0]


def model_window(paper: Paper, scale: tuple[int, int]) -> tuple[float, float]:
    """How much of the model the viewport shows at this scale, as (width, height).

    Both follow from `view_height_model` and the viewport's own proportions: the viewport fixes
    the scale and its shape fixes what else is in shot, which is the rule AutoCAD plots by.
    """
    left, bottom, right, top = viewport_rect(paper)
    height = view_height_model(paper, scale)
    return (height * (right - left) / (top - bottom), height)


def fits_on_sheet(
    paper: Paper, scale: tuple[int, int], extent: tuple[float, float, float, float]
) -> bool:
    """Whether a plan of this extent is wholly inside the viewport at this scale."""
    min_x, min_y, max_x, max_y = extent
    window_x, window_y = model_window(paper, scale)
    return (max_x - min_x) <= window_x and (max_y - min_y) <= window_y


def largest_scale_that_fits(paper: Paper, extent: tuple[float, float, float, float]) -> str:
    """The largest plot scale this package offers at which the whole plan is on the page.

    Walks the offered scales from detailed to coarse and answers with the first that holds the
    plan. An empty model — an extent of no size — fits at the most detailed scale, a truthful
    answer to a question nobody needed to ask.
    """
    min_x, min_y, max_x, max_y = extent
    width, height = max(max_x - min_x, 0.0), max(max_y - min_y, 0.0)
    ordered = sorted(SCALES.items(), key=lambda kv: kv[1][1] / kv[1][0])
    for label, ratio in ordered:
        window_x, window_y = model_window(paper, ratio)
        if width <= window_x and height <= window_y:
            return label
    return ordered[-1][0]


def mm_to_pt(mm: float) -> float:
    """Millimetres as points, the unit a PDF counts in (72 to the inch)."""
    return float(mm) * PT_PER_INCH / MM_PER_INCH


def read_mediabox(path: str | Path) -> tuple[float, float]:
    """The paper size a PDF says it has, in points, read out of the file's own bytes.

    This is the check the competition runs against a live plot, and it runs here against the
    file: the size a plot produced is what the PDF says it is, not what the layout was asked
    for. A page box is usually written plainly, so the raw bytes are searched first; a PDF that
    compresses its page objects is handled by inflating the streams, and only then.

    A file with no readable `/MediaBox` raises rather than returning a guess — a sheet whose
    size cannot be read is not a verified sheet, and a defaulted size is the failure this
    function exists to catch.
    """
    raw = Path(path).read_bytes()
    match = _MEDIABOX.search(raw)
    if match is None:
        for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
            try:
                match = _MEDIABOX.search(zlib.decompress(stream))
            except zlib.error:
                continue
            if match is not None:
                break
    if match is None:
        raise ValueError(f"no readable /MediaBox in {str(path)!r}; the page size is unknown")
    x0, y0, x1, y1 = (float(v) for v in match.groups())
    return (abs(x1 - x0), abs(y1 - y0))


def read_pdf_mediabox_mm(path: str | Path) -> tuple[float, float]:
    """The same size in millimetres, which is the unit this package thinks in."""
    width_pt, height_pt = read_mediabox(path)
    return (width_pt * MM_PER_INCH / PT_PER_INCH, height_pt * MM_PER_INCH / PT_PER_INCH)
