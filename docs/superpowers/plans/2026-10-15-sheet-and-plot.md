# Sheet and Plot Implementation Plan

> **For agentic workers:** Implement this plan in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a drawn plan into a drawing sheet — a named paper size, a plot scale, a frame with a title block on a paperspace layout, and a PDF whose paper size is measured back out of the PDF rather than assumed.

**Architecture:** The sheet's geometry is data and maths in a new pure module (`sheet.py`), the rule `geom.py`, `journal.py`, `summary.py`, `verify.py` and `terms.py` already follow. The dxf driver writes the page setup onto the drawing's own `LAYOUT` entity — which is where AutoCAD reads it, so a `.dxf` saved here opens with the sheet already configured — and draws the frame, the title block and one viewport into `*Paper_Space`. The headless PDF is composed by matplotlib: the sheet at paper size in one axes, the plan in a second axes clipped to the viewport rectangle and scaled by the plot scale. Verification is the idea worth taking from the competition: read the paper size back out of the PDF's own `/MediaBox`, and the scale back out of the saved DXF, instead of trusting what we asked for.

**Tech Stack:** Python 3.10+ (this venv runs 3.14), ezdxf 1.4.4, matplotlib (the `preview` extra), the mcp SDK, pytest, ruff.

## Global Constraints

- Millimetres everywhere; `INSUNITS` is 4. Angles are degrees, counter-clockwise from +X. The area unit is `m²` (U+00B2), never `㎡`.
- `geom.py`, `journal.py`, `summary.py`, `verify.py` and `terms.py` import nothing CAD-related; `verify.py` imports nothing from this package at all. `sheet.py` inherits that rule: pure maths and data, no `ezdxf`, no `backends`, no `ops`. It is imported *by* `ops` and `backends/dxf.py`, never the other way, so there is no cycle and no lazy import is needed.
- `server.py` is thin: each tool parses its input, calls one op or one backend method, and returns a dict. Every drawing tool body runs inside `_auto_step`, so one tool call is one journal step. Every tool carries one of the five annotation sets (`_READONLY` … `_DESTRUCTIVE_IDEMPOTENT`).
- **A standard is the drawing office's, not this package's.** Layer names, model numbers and wall thicknesses come from `acadmcp.json` and the catalogue file, never from `layers.py`. Frame margins and title-block rows follow the same rule: defaults in `sheet.py` with a citation comment, overridable by a project. (Wiring the override into `settings.py` is deliberately **not** in this plan — see Scope Check.)
- A backend capability is **declared, not probed** — `Backend.supports_insert` is a class attribute, and so is the `supports_sheet` this plan adds — because a refusal inside a step is deferred to the commit and a caller cannot find out by trying.
- `backends/base.py` is the only contract the three drivers implement. Every method added there is added to all three: the dxf driver implements it, the com and plugin drivers refuse it by name.
- A caller's mistake raises `ValueError`, converted to `BackendError` by `_surface_input_errors()` so the message survives the SDK. A record that cannot be read is counted and reported, never raised and never silently skipped.
- Comments state a constraint the code cannot show. Match the existing density.
- ruff: `line-length = 100`, `select = ["E", "F", "W", "I", "UP", "B", "SIM"]`, `ignore = ["E501", "RUF001", "RUF002", "RUF003"]`, `evaluations` excluded.
- Tests need no AutoCAD. `uv run pytest -q` is 359 tests in about 12 s and must stay near that.
- **There is no CI.** A pytest test is the only gate that ever runs.
- The three gates already in the tree will **fail** unless this work keeps them fed: `tests/test_terms.py` refuses a tool with no vocabulary entry, `tests/test_tool_surface.py` pins the tool count, `scripts/check_doc_numbers.py` checks the documented counts. Task 5 is scheduled, not optional.

---

## Scope Check

This plan is one subsystem: **a sheet that can be plotted**. Two others are named and left alone.

**Not in this plan, each its own plan later:**

1. **The bundled template library** (`templates/*.dxf` + `*.dwt`, `drawing_template_list`, `drawing_new(template=…)`). A template is a saved sheet configuration, so this becomes small once the sheet exists — and its `.dwt` half can only be produced on a live AutoCAD, since `Documents.Add` accepts only a genuine DWT and the conversion is `SaveAs(path, ac2018_Template)`. That is a live-machine task.
2. **The catalogue's seven block-only entries** (`stud`, `beam_wf`, `beam_i`, `channel`, `light`, `weld_symbol`, `window_elevation`). They name a library block with no parametric twin, so they cannot be placed headless at all and depend on AutoCAD's own sample library being installed. That is a larger problem than the sheet and independent of it.
3. **Page size and scale from a project's own standard.** `sheet.py` defaults are this package's, and a project cannot override them yet. That belongs with the `acadmcp.json` schema work, not here.

---

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `src/acadmcp/sheet.py` | Paper sizes, plot scales, frame margins, frame and title-block geometry, mm↔points, and the two read-backs (a DXF's scale is read by the test, a PDF's `/MediaBox` by this module). Pure. |
| `tests/test_sheet.py` | The sheet maths and both read-backs. |
| `tests/test_sheet_backend.py` | The page setup and the paperspace content, read back out of a saved file. |
| `tests/test_sheet_plot.py` | The composed PDF: size, scale, and the refusals. |

**Modify**

| File | Change |
|---|---|
| `src/acadmcp/backends/base.py` | `supports_sheet`, `set_page_setup`, `paper_primitives`, `paper_viewport`, `plot_pdf` on the protocol. |
| `src/acadmcp/backends/dxf.py` | The real implementations, and `from .. import sheet`. |
| `src/acadmcp/backends/com.py`, `plugin.py` | `supports_sheet = False` and the refusals. |
| `src/acadmcp/ops.py` | `sheet_primitives`, `draw_sheet`. |
| `src/acadmcp/layers.py` | `A-SHEET`, `A-SHEET-TEXT`. |
| `src/acadmcp/server.py` | `drawing_sheet`, `drawing_plot_pdf`, and the sheet in `drawing_status`. |
| `src/acadmcp/terms.py`, `tests/test_tool_surface.py`, `AGENTS.md`, `README.md` | Task 5. |

---

### Task 1: `sheet.py` — the sheet as data and maths

**Files:**
- Create: `src/acadmcp/sheet.py`, `tests/test_sheet.py`

**Interfaces:**
- Produces:
  - `Paper` (frozen dataclass: `name`, `width`, `height`, `binding`, `margin`, `landscape`; property `title_block_origin`)
  - `PAPERS: dict[str, tuple[float, float]]`, `SCALES: dict[str, tuple[int, int]]`
  - `resolve_paper(name: str, orientation: str = "landscape") -> Paper`
  - `resolve_scale(label: str) -> tuple[int, int]`
  - `text_width(text: str, height: float) -> float`
  - `frame_outline(paper) -> list[tuple[Point, Point]]`
  - `title_block(paper, meta: dict[str, str]) -> tuple[list[tuple[Point, Point]], list[tuple[Point, str, float, str]]]`
  - `viewport_rect(paper) -> tuple[float, float, float, float]`
  - `view_height_model(paper, scale: tuple[int, int]) -> float`
  - `mm_to_pt(mm: float) -> float`
  - `read_mediabox(path) -> tuple[float, float]`, `read_pdf_mediabox_mm(path) -> tuple[float, float]`
  - Constants `BINDING_MARGIN`, `EDGE_MARGIN`, `TITLE_BLOCK_WIDTH`, `TITLE_BLOCK_HEIGHT`, `TITLE_BLOCK_ROWS`, `TITLE_BLOCK_TEXT`, `VIEWPORT_GAP`, `CJK_ADVANCE`, `LATIN_ADVANCE`, `MM_PER_INCH`, `PT_PER_INCH`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sheet.py`:

```python
"""The sheet: paper, scale, frame, title block, and the two read-backs."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import sheet  # noqa: E402


def test_the_paper_sizes_are_the_a_series_in_millimetres():
    """GB/T 50001 takes its sheet sizes from the ISO A series, so these are that series."""
    assert sheet.PAPERS["A3"] == (420.0, 297.0)
    assert sheet.PAPERS["A0"] == (1189.0, 841.0)
    assert sheet.PAPERS["A4"] == (297.0, 210.0)


def test_an_unknown_paper_names_what_there_is():
    with pytest.raises(ValueError, match="A3"):
        sheet.resolve_paper("A9")


def test_portrait_swaps_the_sides_and_landscape_does_not():
    assert sheet.resolve_paper("A3").width == 420.0
    portrait = sheet.resolve_paper("A3", "portrait")
    assert (portrait.width, portrait.height) == (297.0, 420.0)
    assert portrait.landscape is False


def test_the_binding_margin_is_on_the_left_and_wider_than_the_others():
    """GB/T 50001 gives the bound edge a wider margin than the other three."""
    paper = sheet.resolve_paper("A3")
    assert paper.binding == 25.0
    assert paper.binding > paper.margin


def test_the_frame_is_inside_the_paper_by_its_margins():
    paper = sheet.resolve_paper("A3")
    xs = [p[0] for line in sheet.frame_outline(paper) for p in line]
    ys = [p[1] for line in sheet.frame_outline(paper) for p in line]
    assert min(xs) == paper.binding and max(xs) == paper.width - paper.margin
    assert min(ys) == paper.margin and max(ys) == paper.height - paper.margin


def test_the_title_block_sits_in_the_bottom_right_corner_of_the_frame():
    paper = sheet.resolve_paper("A3")
    lines, texts = sheet.title_block(paper, {"图名": "生产车间平面图", "比例": "1:100"})
    assert lines and texts
    xs = [p[0] for line in lines for p in line]
    ys = [p[1] for line in lines for p in line]
    frame_xs = [p[0] for line in sheet.frame_outline(paper) for p in line]
    frame_ys = [p[1] for line in sheet.frame_outline(paper) for p in line]
    assert max(xs) == max(frame_xs) and min(ys) == min(frame_ys)


def test_every_title_block_field_reaches_the_paper():
    _, texts = sheet.title_block(
        sheet.resolve_paper("A3"), {"图名": "生产车间平面图", "图号": "JS-01"}
    )
    drawn = [t[1] for t in texts]
    assert "生产车间平面图" in drawn and "JS-01" in drawn


def test_a_chinese_title_fits_inside_its_cell():
    """A Chinese character is 1.42 x its height wide; a title wider than its cell is a bug."""
    _, texts = sheet.title_block(sheet.resolve_paper("A3"), {"图名": "生产车间平面布置图"})
    for _pos, text, height, _align in texts:
        assert sheet.text_width(text, height) <= sheet.TITLE_BLOCK_WIDTH / 2


def test_more_fields_than_rows_are_dropped_not_drawn_over_the_frame():
    """A title block has rows; a fourth field with three rows must not be squeezed in."""
    _, texts = sheet.title_block(
        sheet.resolve_paper("A3"), {"图名": "图", "图号": "1", "日期": "2", "人": "3"}
    )
    assert len(texts) == sheet.TITLE_BLOCK_ROWS * 2


def test_the_view_height_is_the_viewport_span_times_the_scale_denominator():
    """1:100 means one paper millimetre is 100 model millimetres."""
    paper = sheet.resolve_paper("A3")
    left, bottom, right, top = sheet.viewport_rect(paper)
    assert sheet.view_height_model(paper, (1, 100)) == pytest.approx((top - bottom) * 100)
    assert sheet.view_height_model(paper, (1, 100)) == pytest.approx(272.0)


def test_a_scale_label_resolves_and_a_nonsense_one_is_refused():
    assert sheet.resolve_scale("1:100") == (1, 100)
    assert sheet.resolve_scale(" 2:1 ") == (2, 1)
    with pytest.raises(ValueError, match="ratio"):
        sheet.resolve_scale("1 to 100")


def test_millimetres_become_points_the_way_a_pdf_counts_them():
    assert sheet.mm_to_pt(25.4) == pytest.approx(72.0)
    assert sheet.mm_to_pt(420.0) == pytest.approx(1190.5511811024, abs=1e-6)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sheet.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'acadmcp.sheet'`.

- [ ] **Step 3: Write `src/acadmcp/sheet.py`**

```python
"""The sheet: paper, scale, frame, title block, and the read-back that checks them.

A plan drawn in model space is a plan; a drawing is a plan on paper of a named size, at a
named scale, inside a frame with a title block. This module is the data and the maths for
that — no CAD library and nothing from this package — so the same numbers reach the file
through the backend and the PDF through the renderer.

The sheet sizes are the ISO A series, which is what GB/T 50001 takes them from. The margins
and the title-block rows are this package's defaults for a Chinese architectural sheet: a
25 mm bound edge, 10 mm on the other three, a title block 180 mm wide and one row tall in the
bottom-right corner of the frame. They are defaults and not law — a drawing office has its
own, the same way it has its own layer standard.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path

Point = tuple[float, float]

#: Sheet sizes in millimetres, landscape (width, height).
PAPERS: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
}

#: The plot scales a floor plan is drawn at. 1:1 and 2:1 are here for a detail.
SCALES: dict[str, tuple[int, int]] = {
    "1:1": (1, 1),
    "2:1": (2, 1),
    "1:20": (1, 20),
    "1:50": (1, 50),
    "1:100": (1, 100),
    "1:150": (1, 150),
    "1:200": (1, 200),
    "1:500": (1, 500),
}

#: A Chinese character is 1.42 x its text height wide — measured on both sides of the same
#: drawing (生产车间 at height 350 is 1991 mm in AutoCAD and 1973 mm in this package's own
#: preview). Anything sizing a caption to sit inside a box needs it.
CJK_ADVANCE = 1.42
LATIN_ADVANCE = 0.62

#: The bound edge, and the other three.
BINDING_MARGIN = 25.0
EDGE_MARGIN = 10.0

#: The title block: 180 mm wide, one A4 row tall, in the bottom-right corner of the frame.
TITLE_BLOCK_WIDTH = 180.0
TITLE_BLOCK_HEIGHT = 56.0
TITLE_BLOCK_ROWS = 3
TITLE_BLOCK_TEXT = 5.0

#: The gap between the frame and what it holds, so a wall never touches the frame line.
VIEWPORT_GAP = 5.0

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0

_MEDIABOX = re.compile(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)")


@dataclass(frozen=True, slots=True)
class Paper:
    name: str
    width: float
    height: float
    binding: float
    margin: float
    landscape: bool

    @property
    def title_block_origin(self) -> Point:
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
    return Paper(key, width, height, BINDING_MARGIN, EDGE_MARGIN, landscape)


def resolve_scale(label: str) -> tuple[int, int]:
    """`"1:100"` as (1, 100). A label that is not a ratio is refused, not parsed loosely."""
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


def text_width(text: str, height: float) -> float:
    """How wide a string is drawn, in millimetres, at this text height."""
    return sum(height * (CJK_ADVANCE if ch > "\u2e7f" else LATIN_ADVANCE) for ch in text)


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
    and value in the right — 图名 | 生产车间平面图 — and a value too long for its cell is shrunk
    to fit rather than allowed to cross the frame, the rule room names and area labels already
    follow. A field past the last row is dropped: a title block has rows, and squeezing a
    fourth into three would overlap the two above it.
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
    split = origin_x + TITLE_BLOCK_WIDTH * 0.28
    lines.append(((split, origin_y), (split, top)))

    texts: list[tuple[Point, str, float, str]] = []
    for row, (name, value) in enumerate(list(meta.items())[:TITLE_BLOCK_ROWS]):
        y = top - (row + 0.5) * row_height
        cell = (split - origin_x) - 4.0
        label_h = min(TITLE_BLOCK_TEXT, cell / max(text_width(name, 1.0), 1e-6))
        value_cell = (right - split) - 4.0
        value_h = min(TITLE_BLOCK_TEXT, value_cell / max(text_width(value, 1.0), 1e-6))
        texts.append(((origin_x + 2.0, y), name, label_h, "left"))
        texts.append(((split + 2.0, y), value, value_h, "left"))
    return lines, texts


def viewport_rect(paper: Paper) -> tuple[float, float, float, float]:
    """Where the plan is shown: inside the frame, clear of the title block."""
    left = paper.binding + VIEWPORT_GAP
    bottom = paper.margin + VIEWPORT_GAP
    right = paper.width - paper.margin - TITLE_BLOCK_WIDTH - VIEWPORT_GAP
    top = paper.height - paper.margin - VIEWPORT_GAP
    return (left, bottom, right, top)


def view_height_model(paper: Paper, scale: tuple[int, int]) -> float:
    """The model height a viewport of this size shows at this scale.

    This is the one relationship that makes a sheet a sheet: at 1:100 one paper millimetre is
    100 model millimetres, so a 272 mm tall viewport shows 27.2 m. Get it wrong and the plan is
    the right shape at the wrong size, which nobody notices until it is measured.
    """
    _left, bottom, _right, top = viewport_rect(paper)
    return (top - bottom) * scale[1] / scale[0]


def mm_to_pt(mm: float) -> float:
    return float(mm) * PT_PER_INCH / MM_PER_INCH


def read_mediabox(path: str | Path) -> tuple[float, float]:
    """The paper size a PDF says it has, in points, read out of the file's own bytes.

    This is the check the competition runs against a live plot, and it runs here against the
    file: the size a plot produced is what the PDF says it is, not what the layout was asked
    for. A PDF keeps `/MediaBox` in the page object, sometimes inside a compressed stream, so
    the raw bytes are searched first and the streams inflated only if that finds nothing.

    A file with no readable `/MediaBox` raises rather than returning a guess: a sheet whose
    size cannot be read is not a verified sheet.
    """
    raw = Path(path).read_bytes()
    match = _MEDIABOX.search(raw)
    if match is None:
        for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
            try:
                match = _MEDIABOX.search(zlib.decompress(stream))
            except zlib.error:
                continue
            if match:
                break
    if match is None:
        raise ValueError(f"no readable /MediaBox in {str(path)!r}; the page size is unknown")
    x0, y0, x1, y1 = (float(v) for v in match.groups())
    return (abs(x1 - x0), abs(y1 - y0))


def read_pdf_mediabox_mm(path: str | Path) -> tuple[float, float]:
    """The same size in millimetres, which is the unit this package thinks in."""
    width_pt, height_pt = read_mediabox(path)
    return (width_pt * MM_PER_INCH / PT_PER_INCH, height_pt * MM_PER_INCH / PT_PER_INCH)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_sheet.py -q`
Expected: PASS, 11 tests.

- [ ] **Step 5: Pin the read-back against a real file**

Append to `tests/test_sheet.py`:

```python
def test_a_pdf_is_measured_not_assumed(tmp_path):
    """The whole point of the read-back: the file is asked, not our own arithmetic."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def write(path, width_mm, height_mm):
        fig = plt.figure(figsize=(sheet.mm_to_pt(width_mm) / 72, sheet.mm_to_pt(height_mm) / 72))
        fig.add_axes((0.0, 0.0, 1.0, 1.0)).set_axis_off()
        fig.savefig(path)
        plt.close(fig)

    a3 = sheet.resolve_paper("A3")
    right = tmp_path / "a3.pdf"
    write(right, a3.width, a3.height)
    assert sheet.read_pdf_mediabox_mm(right) == pytest.approx((420.0, 297.0), abs=0.05)

    wrong = tmp_path / "a4.pdf"
    write(wrong, 297.0, 210.0)
    assert sheet.read_pdf_mediabox_mm(wrong) != pytest.approx((420.0, 297.0), abs=0.05)
```

Run: `uv run pytest tests/test_sheet.py -q`
Expected: PASS, 12 tests.

- [ ] **Step 6: Lint and format**

Run: `uv run ruff check src/acadmcp/sheet.py tests/test_sheet.py --output-format concise && uv run ruff format src/acadmcp/sheet.py tests/test_sheet.py`
Expected: `All checks passed!`, then `2 files reformatted` or `left unchanged`.

---

### Task 2: The page setup goes into the file

**Files:**
- Modify: `src/acadmcp/backends/base.py`, `backends/dxf.py`, `backends/com.py`, `backends/plugin.py`
- Test: `tests/test_sheet_backend.py`

**Interfaces:**
- Consumes: `sheet.Paper`, `sheet.resolve_paper`, `sheet.resolve_scale`.
- Produces on the protocol: `supports_sheet: bool`, `set_page_setup(paper, scale, *, layout="", style_sheet="") -> dict`, `paper_primitives(primitives) -> list[str]`, `paper_viewport(*, center, size, view_height) -> str`, `plot_pdf(path, *, paper, scale, theme="light") -> str`. Task 3 implements the middle two on the dxf driver, Task 4 the last.
- Produces on `DxfBackend`: `self.sheet: dict[str, Any]` — the reportable sheet (`paper`, `orientation`, `width`, `height`, `scale`, `layout`, `style_sheet`) — and `self._sheet_layout`, the layout object the sheet is drawn on.

**Why the fields and not a `PLOTSETTINGS` object:** verified in this venv on ezdxf 1.4.4 — a `LAYOUT` entity carries the whole page setup itself: `page_setup_name`, `paper_size`, `paper_width`, `paper_height`, `left_margin`, `bottom_margin`, `right_margin`, `top_margin`, `scale_numerator`, `scale_denominator`, `standard_scale_type`, `unit_factor`, `current_style_sheet`, `plot_paper_units`, `plot_type`, `plot_rotation`. A fresh `ezdxf.new("R2010")` has none of them set and no `PLOTSETTINGS` object at all, which is why a drawing from this package opens on whatever the reader's default is. **The scale is written in three places that must agree**: `standard_scale_type` (DXF group 75) and `unit_factor` (group 147) are what a plot reads, so a file whose 75 says 1:10 and whose 147 says 1.0 plots at 1:1 while claiming 1:10.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sheet_backend.py`:

```python
"""The sheet as it lands in the file: the page setup, read back out of a saved DXF."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import sheet  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


def _layout(path, name="A3"):
    import ezdxf

    return ezdxf.readfile(path).layouts.get(name)


def test_a_new_dxf_has_no_page_setup_at_all():
    """The gap this task closes, pinned so it cannot come back unnoticed."""
    layout = make_backend("dxf").doc.layouts.get("Layout1")
    assert layout.dxf.get("paper_width") is None
    assert layout.dxf.get("unit_factor") is None


def test_the_page_setup_reaches_the_saved_file(tmp_path):
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 100))
    target = str(tmp_path / "sheet.dxf")
    be.save(target)

    layout = _layout(target)
    assert layout.dxf.paper_width == pytest.approx(420.0)
    assert layout.dxf.paper_height == pytest.approx(297.0)
    assert layout.dxf.paper_size == "A3"


def test_the_scale_is_written_where_a_plot_reads_it(tmp_path):
    """75, 147 and the ratio must agree, or the sheet plots at a scale it does not claim."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 100))
    target = str(tmp_path / "scale.dxf")
    be.save(target)

    layout = _layout(target)
    assert (layout.dxf.scale_numerator, layout.dxf.scale_denominator) == (1, 100)
    assert layout.dxf.unit_factor == pytest.approx(100.0)
    assert layout.dxf.standard_scale_type == 16, "16 is the DXF code for 1:200"


def test_a_scale_with_no_enum_row_keeps_the_denominator_honest(tmp_path):
    """The measured failure mode: group 75 and group 147 disagreeing about the scale."""
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 123))
    layout = _layout(str(tmp_path / "odd.dxf")) if False else be._sheet_layout
    assert layout.dxf.unit_factor == pytest.approx(123.0)
    assert layout.dxf.get("standard_scale_type") in (None, 0), (
        "an enum row this package has no value for must not be invented"
    )


def test_the_layout_is_named_for_its_sheet(tmp_path):
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A2"), (1, 200))
    target = str(tmp_path / "named.dxf")
    be.save(target)
    import ezdxf

    assert "A2" in [layout.name for layout in ezdxf.readfile(target).layouts]


def test_the_margins_follow_the_frame(tmp_path):
    be = make_backend("dxf")
    paper = sheet.resolve_paper("A3")
    be.set_page_setup(paper, (1, 100))
    layout = be._sheet_layout
    assert layout.dxf.left_margin == paper.binding
    assert layout.dxf.bottom_margin == paper.margin
    assert layout.dxf.plot_paper_units == 1, "1 is millimetres"


def test_the_style_sheet_name_is_carried_when_given():
    be = make_backend("dxf")
    be.set_page_setup(sheet.resolve_paper("A3"), (1, 100), style_sheet="monochrome.ctb")
    assert be._sheet_layout.dxf.current_style_sheet == "monochrome.ctb"


def test_the_sheet_the_backend_reports_is_what_a_caller_needs(tmp_path):
    be = make_backend("dxf")
    report = be.set_page_setup(sheet.resolve_paper("A3", "portrait"), (1, 100))
    assert report["paper"] == "A3" and report["orientation"] == "portrait"
    assert report["scale"] == "1:100" and report["layout"] == "A3"
    assert report["width"] == 297.0 and report["height"] == 420.0


def test_the_live_drivers_declare_they_cannot_make_a_sheet():
    """Declared, not probed, for the same reason `supports_insert` is."""
    from acadmcp.backends.com import ComBackend
    from acadmcp.backends.plugin import PluginBackend

    assert ComBackend.supports_sheet is False
    assert PluginBackend.supports_sheet is False


def test_the_live_drivers_refuse_by_name_rather_than_drawing_half_a_sheet():
    from acadmcp.backends.com import ComBackend
    from acadmcp.backends.plugin import PluginBackend

    for driver in (ComBackend, PluginBackend):
        with pytest.raises(BackendError, match="paperspace"):
            driver.set_page_setup(_Standin(driver), sheet.resolve_paper("A3"), (1, 100))


class _Standin:
    """Enough of a backend to reach the refusal without a live AutoCAD.

    The refusal is a shared helper, so it can be exercised on the class that defines it
    rather than by constructing a driver that would connect to something.
    """

    def __init__(self, driver):
        self.name = "stub"
        self._no_sheet = driver._no_sheet
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sheet_backend.py -q`
Expected: FAIL — `AttributeError: 'DxfBackend' object has no attribute 'set_page_setup'`.

- [ ] **Step 3: Declare the capability and the four methods on the protocol**

In `src/acadmcp/backends/base.py`, after `supports_insert`:

```python
    #: Whether this backend can put a sheet on the drawing: a named paper, a plot scale, a frame
    #: and a title block on a paperspace layout. Declared for the same reason `supports_insert`
    #: is — a refusal inside a step is deferred to the commit, so which half to draw has to be
    #: decided before anything is drawn. The dxf driver writes the page setup onto the file's
    #: own LAYOUT entity, which is where AutoCAD reads it; the live drivers have no paperspace
    #: path and say so.
    supports_sheet: bool
```

and, after `set_units_mm`:

```python
def set_page_setup(
    self, paper: Any, scale: tuple[int, int], *, layout: str = "", style_sheet: str = ""
) -> dict[str, Any]:
    """Name a layout for this sheet and give it a paper, a scale and a style sheet."""
    ...


def paper_primitives(self, primitives: list[dict[str, Any]]) -> list[str]:
    """Draw primitives into the sheet's paperspace instead of the model.

    The vocabulary `_apply` takes, so `ops` builds the frame and the title block the way it
    builds everything else and only the destination differs. Returns the handles, so a step
    can undo them like any other.
    """
    ...


def paper_viewport(self, *, center: Point, size: tuple[float, float], view_height: float) -> str:
    """Frame the plan on the sheet: one viewport, sized in paper mm, scaled in model mm."""
    ...


def plot_pdf(self, path: str, *, paper: Any, scale: tuple[int, int], theme: str = "light") -> str:
    """Render the sheet — frame, title block and the plan at the plot scale — to a PDF."""
    ...
```

- [ ] **Step 4: Refuse all four on the live drivers**

In `com.py` and `plugin.py`, beside `supports_insert = False`:

```python
    #: The live drivers have no paperspace path: a page setup and a frame would have to be
    #: written through ActiveX, and a plot would go through AutoCAD's own plotter. Neither is
    #: built, so the tools that need a sheet refuse by name rather than draw half of one.
    supports_sheet = False
```

and, on each of the two classes, one shared refusal used by all four methods:

```python
def _no_sheet(self, what: str) -> NoReturn:
    raise BackendError(
        f"backend {self.name!r} cannot {what}: a sheet needs a paperspace layout, and only "
        "the dxf driver writes one. Use drawing_new(backend='dxf') for a sheet, or open "
        "this drawing in AutoCAD and set the page setup there"
    )


def set_page_setup(self, paper, scale, *, layout="", style_sheet=""):
    self._no_sheet("set a page setup")


def paper_primitives(self, primitives):
    self._no_sheet("draw into paperspace")


def paper_viewport(self, *, center, size, view_height):
    self._no_sheet("frame a plan on a sheet")


def plot_pdf(self, path, *, paper, scale, theme="light"):
    self._no_sheet("plot a sheet")
```

Add `NoReturn` to each file's `typing` import.

- [ ] **Step 5: Implement the page setup on the dxf driver**

At module level in `src/acadmcp/backends/dxf.py`, add `from .. import sheet` to the package imports and, beside `DIM_TEXT_HEIGHT`:

```python
#: DXF group 75: which standard scale a layout plots at. AutoCAD writes one of a fixed enum
#: and a plot reads it together with group 147 (`unit_factor`, the actual denominator), so both
#: are set from one place in `set_page_setup` — a file whose 75 says 1:10 and whose 147 says 1.0
#: plots at 1:1 while claiming 1:10, which is the failure this table exists to prevent. A ratio
#: with no row here is written to 147 alone; see the comment in `set_page_setup`.
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
```

In `__init__`, beside `self.journal_unreadable = ""`:

```python
        #: The sheet, once one is made: the reportable dict a tool returns and the layout the
        #: frame is drawn on. Two names because they are two things — `sheet` leaves the process
        #: in a tool's reply, `_sheet_layout` lives here and is not JSON.
        self.sheet: dict[str, Any] = {}
        self._sheet_layout: Any = None
        self.last_plot: dict[str, Any] = {}
```

and in the class body, beside `supports_insert`:

```python
    #: The dxf driver writes the page setup onto the file's own LAYOUT, which is where AutoCAD
    #: reads it, so a DXF saved here opens with the sheet already configured.
    supports_sheet = True
```

then the two methods:

```python
def _sheet_target(self, name: str = "") -> Any:
    """The layout the sheet goes on, created when the document has none but Model."""
    wanted = (name or "").strip()
    if wanted:
        return (
            self.doc.layouts.get(wanted)
            if wanted in self.doc.layouts
            else self.doc.layouts.new(wanted)
        )
    for layout in self.doc.layouts:
        if layout.name.upper() != "MODEL":
            return layout
    return self.doc.layouts.new("A3")


def set_page_setup(
    self, paper: Any, scale: tuple[int, int], *, layout: str = "", style_sheet: str = ""
) -> dict[str, Any]:
    target = self._sheet_target(layout or paper.name)
    if target.name != (layout or paper.name):
        # A layout AutoCAD already has under another name is renamed, not duplicated: two
        # sheets in one file is how a reader plots the wrong one.
        target.rename(layout or paper.name)
    numerator, denominator = int(scale[0]), int(scale[1])
    target.dxf.paper_size = paper.name
    target.dxf.paper_width = paper.width
    target.dxf.paper_height = paper.height
    target.dxf.plot_paper_units = 1  # millimetres
    target.dxf.plot_type = 1  # what the layout holds, not the extents or a window
    target.dxf.plot_rotation = 0
    target.dxf.scale_numerator = numerator
    target.dxf.scale_denominator = denominator
    target.dxf.unit_factor = denominator / numerator
    known = STANDARD_SCALE_TYPES.get((numerator, denominator))
    if known is not None:
        target.dxf.standard_scale_type = known
    # A ratio with no enum row is *not* given an invented one: 147 alone is what AutoCAD
    # falls back to, and a wrong 75 is worse than an absent one.
    target.dxf.left_margin = paper.binding
    target.dxf.bottom_margin = paper.margin
    target.dxf.right_margin = paper.margin
    target.dxf.top_margin = paper.margin
    if style_sheet:
        target.dxf.current_style_sheet = style_sheet
    self._sheet_layout = target
    self.sheet = {
        "paper": paper.name,
        "orientation": "landscape" if paper.landscape else "portrait",
        "width": paper.width,
        "height": paper.height,
        "scale": f"{numerator}:{denominator}",
        "layout": target.name,
        "style_sheet": style_sheet,
    }
    return dict(self.sheet)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_sheet_backend.py -q`
Expected: PASS, 10 tests.

- [ ] **Step 7: Check the enum value against AutoCAD's own list before trusting the table**

`standard_scale_type` is written from a table transcribed here, and `test_the_scale_is_written_where_a_plot_reads_it` asserts `16` for 1:200 from that same table — which proves only that the table is self-consistent. Confirm the mapping against a drawing AutoCAD has written: open any `.dxf` with a saved page setup (or save one from AutoCAD with 1:100) and read its group 75. If the real enum disagrees, fix `STANDARD_SCALE_TYPES`, not the test. Record what was measured in the comment above the table, in one line, the way the rest of this repository records its measurements.

---

### Task 3: The frame, the title block and the viewport

**Files:**
- Modify: `src/acadmcp/backends/dxf.py` (`paper_primitives`, `paper_viewport`), `src/acadmcp/ops.py`, `src/acadmcp/layers.py`, `src/acadmcp/server.py`
- Test: `tests/test_sheet_backend.py` (append)

**Interfaces:**
- Consumes: `sheet.frame_outline`, `sheet.title_block`, `sheet.viewport_rect`, `sheet.view_height_model`; `DxfBackend.paper_primitives`, `DxfBackend.paper_viewport`.
- Produces: `ops.sheet_primitives(paper, meta) -> list[dict]`, `ops.draw_sheet(be, paper, scale, meta) -> dict`, the tool `drawing_sheet(...) -> dict`, and `drawing_status()["sheet"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sheet_backend.py`:

```python
from acadmcp import server  # noqa: E402


@pytest.fixture()
def fresh_dxf():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_the_frame_and_title_block_land_in_paperspace_not_in_the_plan(fresh_dxf, tmp_path):
    """A frame drawn into the model would scale with the plan; on the layout it is paper."""
    server.draw_line([0, 0], [6000, 0])
    target = str(tmp_path / "paper.dxf")
    server.drawing_sheet(paper="A3", scale="1:100", title="生产车间平面图", number="JS-01")
    server.drawing_save(target)

    import ezdxf

    doc = ezdxf.readfile(target)
    layout = doc.layouts.get("A3")
    text = [e for e in layout if e.dxftype() == "TEXT"]
    assert {e.dxf.text for e in text} >= {"图名", "生产车间平面图", "图号", "JS-01"}
    assert len(list(doc.modelspace())) == 1, "the plan itself is untouched"
    assert not [
        e
        for e in doc.modelspace()
        if e.dxftype() == "TEXT" and "图名" in str(e.dxf.get("text", ""))
    ], "nothing of the sheet may land in the model"


def test_the_sheet_draws_a_frame_four_lines_and_a_title_block(fresh_dxf):
    report = server.drawing_sheet(paper="A3", scale="1:100", title="图")
    assert report["lines"] == 4 + 4, "the frame's four sides and the title block's box"
    assert report["texts"] == 4, "two fields, a name and a value each"


def test_the_layout_holds_one_viewport_framed_on_the_plan(fresh_dxf, tmp_path):
    server.draw_line([0, 0], [30000, 0])
    server.draw_line([0, 20000], [30000, 20000])
    target = str(tmp_path / "vp.dxf")
    server.drawing_sheet(paper="A3", scale="1:100")
    server.drawing_save(target)

    import ezdxf

    layout = ezdxf.readfile(target).layouts.get("A3")
    viewports = [e for e in layout if e.dxftype() == "VIEWPORT"]
    assert len(viewports) == 1
    assert viewports[0].dxf.view_height == pytest.approx(27200.0), "272 mm at 1:100"
    assert viewports[0].dxf.view_center_point[:2] == pytest.approx((15000.0, 10000.0))


def test_a_plan_away_from_the_origin_is_still_centred_on_its_sheet(fresh_dxf, tmp_path):
    """Plans are drawn where the building is, not at 0,0."""
    server.draw_line([400000, 300000], [430000, 300000])
    target = str(tmp_path / "far.dxf")
    server.drawing_sheet(paper="A3", scale="1:100")
    server.drawing_save(target)
    import ezdxf

    viewport = next(
        e for e in ezdxf.readfile(target).layouts.get("A3") if e.dxftype() == "VIEWPORT"
    )
    assert viewport.dxf.view_center_point[:2] == pytest.approx((415000.0, 300000.0))


def test_an_empty_drawing_frames_the_origin_rather_than_failing(fresh_dxf):
    report = server.drawing_sheet(paper="A3", scale="1:100")
    assert report["ok"] is True and report["viewport"]


def test_the_sheet_is_one_undoable_step(fresh_dxf):
    """One tool call is one step, the rule every drawing tool follows."""
    server.draw_line([0, 0], [6000, 0])
    before = server.drawing_verify()["expected_entities"]
    server.drawing_sheet(paper="A3", scale="1:100", title="图")

    rows = server.drawing_history()["steps"]
    assert rows[-1]["op"] == "drawing_sheet" and rows[-1]["entities"] > 0

    server.drawing_undo()
    after = server.drawing_verify()
    assert after["verdict"] == "matches" and after["expected_entities"] == before


def test_the_sheet_is_reported_by_drawing_status(fresh_dxf):
    server.drawing_sheet(paper="A3", scale="1:100", style_sheet="mono.ctb")
    status = server.drawing_status()
    assert status["sheet"]["paper"] == "A3" and status["sheet"]["scale"] == "1:100"


def test_a_paper_or_scale_this_package_lacks_is_refused_by_name(fresh_dxf):
    with pytest.raises(BackendError, match="A9"):
        server.drawing_sheet(paper="A9")
    with pytest.raises(BackendError, match="ratio"):
        server.drawing_sheet(scale="1 to 100")


def test_the_sheet_tool_refuses_on_a_backend_without_paperspace():
    """The refusal a client sees, exercised without a live AutoCAD."""

    class _NoSheet:
        name = "plugin"
        supports_sheet = False

    server._state.reset(_NoSheet(), "plugin")
    try:
        with pytest.raises(BackendError, match="paperspace"):
            server.drawing_sheet(paper="A3", scale="1:100")
    finally:
        server._state.backend = None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sheet_backend.py -q -k "paperspace or viewport or one_undoable or centring or empty_drawing or status or refused_by_name"`
Expected: FAIL — `AttributeError: module 'acadmcp.server' has no attribute 'drawing_sheet'`.

- [ ] **Step 3: The two new layers**

In `src/acadmcp/layers.py`, add to `LAYERS`: `"A-SHEET": (7, "CONTINUOUS")` and `"A-SHEET-TEXT": (7, "CONTINUOUS")`, with `fit_text_height` left alone — a sheet's text is sized by the title block, not by a room.

- [ ] **Step 4: `ops.sheet_primitives` and `ops.draw_sheet`**

In `src/acadmcp/ops.py`, add `sheet` to the package imports and, after `draw_axis_grid`:

```python
def sheet_primitives(paper: sheet.Paper, meta: dict[str, str]) -> list[dict[str, Any]]:
    """The frame and the title block, in paper millimetres.

    Returned in the primitive vocabulary every backend already renders, so the sheet is built
    by the same machinery as everything else and only its destination differs.
    """
    prims: list[dict[str, Any]] = []
    for p1, p2 in sheet.frame_outline(paper):
        prims.append({"kind": "line", "p1": p1, "p2": p2, "layer": "A-SHEET"})
    lines, texts = sheet.title_block(paper, meta)
    for p1, p2 in lines:
        prims.append({"kind": "line", "p1": p1, "p2": p2, "layer": "A-SHEET"})
    for pos, value, height, align in texts:
        prims.append(
            {
                "kind": "text",
                "pos": pos,
                "text": value,
                "height": height,
                "rotation": 0.0,
                "align": align,
                "layer": "A-SHEET-TEXT",
            }
        )
    return prims


def draw_sheet(
    be: Backend, paper: sheet.Paper, scale: tuple[int, int], meta: dict[str, str]
) -> dict[str, Any]:
    """Draw the sheet into paperspace and frame the plan in one viewport."""
    prims = sheet_primitives(paper, meta)
    handles = be.paper_primitives(prims)
    left, bottom, right, top = sheet.viewport_rect(paper)
    viewport = be.paper_viewport(
        center=((left + right) / 2.0, (bottom + top) / 2.0),
        size=(right - left, top - bottom),
        view_height=sheet.view_height_model(paper, scale),
    )
    return {
        "lines": sum(1 for p in prims if p["kind"] == "line"),
        "texts": sum(1 for p in prims if p["kind"] == "text"),
        "handles": len(handles),
        "viewport": viewport,
    }
```

- [ ] **Step 5: The dxf driver draws paperspace and makes the viewport**

In `src/acadmcp/backends/dxf.py`, beside the primitive methods:

```python
def paper_primitives(self, primitives: list[dict[str, Any]]) -> list[str]:
    """Draw the same primitives into the sheet's paperspace.

    `_apply` targets the model because that is where a plan goes; a sheet does not scale
    with the plan, so its frame is drawn on the layout at 1:1 paper millimetres. The
    primitives are the ones `ops` already builds, so no part of the frame is drawn twice or
    differently. A kind with no paperspace path is refused rather than skipped: a frame
    missing its title block is a wrong sheet, and silence is how that ships.
    """
    if self._sheet_layout is None:
        raise BackendError("no sheet is prepared on this drawing; call set_page_setup first")
    layout = self._sheet_layout
    handles: list[str] = []
    for prim in primitives:
        attribs = self._attribs(prim.get("layer", "A-SHEET"))
        kind = prim.get("kind")
        if kind == "line":
            entity = layout.add_line(xy(prim["p1"]), xy(prim["p2"]), dxfattribs=dict(attribs))
        elif kind == "text":
            entity = layout.add_text(
                str(prim.get("text", "")),
                height=float(prim.get("height") or sheet.TITLE_BLOCK_TEXT),
                rotation=float(prim.get("rotation") or 0.0),
                dxfattribs=dict(attribs),
            )
            entity.set_placement(xy(prim["pos"]), align=TextEntityAlignment.LEFT)
        elif kind == "polyline":
            entity = layout.add_lwpolyline(
                [xy(p) for p in prim["points"]],
                close=bool(prim.get("closed")),
                dxfattribs=dict(attribs),
            )
        elif kind == "circle":
            entity = layout.add_circle(
                xy(prim["center"]), float(prim["radius"]), dxfattribs=dict(attribs)
            )
        else:
            raise BackendError(
                f"a sheet cannot draw a {kind!r} primitive; the kinds it knows are line, "
                "polyline, text and circle"
            )
        handles.append(str(entity.dxf.handle))
    return handles


def paper_viewport(self, *, center: Point, size: tuple[float, float], view_height: float) -> str:
    """Frame the plan on the sheet: one viewport, sized in paper mm and scaled in model mm.

    `view_height` in model units is the whole of the plot scale — a 1:100 sheet shows 100
    model millimetres per paper millimetre — so it comes from `sheet.view_height_model`
    rather than from a constant here. The viewport is centred on what the plan occupies, so
    a building drawn away from the origin is still centred on its sheet.
    """
    if self._sheet_layout is None:
        raise BackendError("no sheet is prepared on this drawing; call set_page_setup first")
    viewport = self._sheet_layout.add_viewport(
        center=xy(center),
        size=(float(size[0]), float(size[1])),
        view_center_point=self._plan_center(),
        view_height=float(view_height),
    )
    return str(viewport.dxf.handle)


def _plan_center(self) -> Point:
    """The middle of what the model holds, so the viewport lands on the plan.

    Read off the entities rather than from `$EXTMIN`/`$EXTMAX`, which ezdxf only refreshes
    on its own terms. A model with nothing in it centres on the origin, which is what an
    empty sheet should show.
    """
    xs: list[float] = []
    ys: list[float] = []
    for entity in self.msp:
        for key in ("start", "end", "center", "insert"):
            value = entity.dxf.get(key, None) if entity.dxf.hasattr(key) else None
            if value is not None:
                xs.append(float(value[0]))
                ys.append(float(value[1]))
        getter = getattr(entity, "get_points", None)
        if getter is not None:
            try:
                for point in getter("xy"):
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
            except (TypeError, ValueError):  # a point layout this method cannot read
                continue
    if not xs:
        return (0.0, 0.0)
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
```

`TextEntityAlignment` is already imported in this file. Register the two new primitive kinds in the step's own dispatch so the journal records the sheet under `drawing_sheet` like any other step.

- [ ] **Step 6: The tool, and the sheet in `drawing_status`**

In `src/acadmcp/server.py`, add `sheet` to the package imports. After `drawing_open`:

```python
@mcp.tool(annotations=_MUTATING)
def drawing_sheet(
    paper: str = "A3",
    scale: str = "1:100",
    orientation: str = "landscape",
    title: str = "",
    number: str = "",
    sheet_of: str = "",
    style_sheet: str = "",
) -> dict[str, Any]:
    """Put a sheet on the drawing: a named paper, a plot scale, a frame and a title block.

    The paper is the ISO A series (`A0`…`A4`), which is what GB/T 50001 takes its sheet sizes
    from; `scale` is a ratio like `1:100`. Both are refused by name when this package has no
    row for them, because a plan on the wrong paper or at the wrong scale is discovered at the
    printer. `title`, `number` and `sheet_of` go into the title block.

    The page setup is written onto the file's own layout, so a DXF saved from here opens in
    AutoCAD with the sheet already configured. `style_sheet` names a plot style (a `.ctb`) when
    the office has one.

    Only the dxf backend can do this today; the com and plugin backends refuse by name rather
    than draw half a sheet.
    """
    be = _state.require()
    if not getattr(be, "supports_sheet", False):
        raise BackendError(
            f"backend {be.name!r} cannot make a sheet: a page setup and a frame need a "
            "paperspace layout. Use drawing_new(backend='dxf'), or set the page setup in "
            "AutoCAD"
        )
    resolved = sheet.resolve_paper(paper, orientation)
    ratio = sheet.resolve_scale(scale)
    meta = {k: v for k, v in {"图名": title, "图号": number, "共几张": sheet_of}.items() if v}
    params = {"paper": resolved.name, "scale": f"{ratio[0]}:{ratio[1]}", "orientation": orientation}
    with _auto_step("drawing_sheet", params, label=title) as be:
        report = be.set_page_setup(resolved, ratio, style_sheet=style_sheet)
        report.update(ops.draw_sheet(be, resolved, ratio, meta))
    return {"ok": True, "title_block": list(meta), **report}
```

and in `drawing_status`'s returned dict:

```python
        "sheet": dict(getattr(be, "sheet", {})),
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_sheet_backend.py -q`
Expected: PASS, 18 tests. If `test_the_frame_and_title_block_land_in_paperspace_not_in_the_plan` finds the title block's texts in the model, `paper_primitives` is drawing through `_apply` instead of onto the layout.

---

### Task 4: The PDF, and the paper size read back out of it

**Files:**
- Modify: `src/acadmcp/backends/dxf.py` (`plot_pdf`), `src/acadmcp/server.py` (`drawing_plot_pdf`)
- Test: `tests/test_sheet_plot.py`

**Interfaces:**
- Consumes: `DxfBackend._sheet_layout`, `DxfBackend.sheet`, `DxfBackend._plan_center`, `sheet.viewport_rect`, `sheet.view_height_model`, `sheet.mm_to_pt`, `sheet.read_pdf_mediabox_mm`.
- Produces: `DxfBackend.plot_pdf(path, *, paper, scale, theme="light") -> str`; the tool `drawing_plot_pdf(path=None, theme="light") -> dict` returning `{"ok", "path", "paper_mm", "asked_for_mm", "mismatch_mm", "verified", "scale"}`.

**How the composition works, and why two axes:** the sheet is drawn into a figure whose size *is* the paper, so the PDF's `/MediaBox` is the paper by construction. The plan is drawn into a second axes positioned over the viewport rectangle, with its data limits set to the model extent that viewport shows at the plot scale — at 1:100 a 272 mm tall viewport spans 27200 model mm, so the axes' own limits *are* the plot scale and its own clipping *is* the viewport boundary. No transform is computed by hand and nothing can be scaled twice.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sheet_plot.py`:

```python
"""The plotted sheet: the paper it says it is, and the plan on it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server, sheet  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


@pytest.fixture()
def fresh_dxf():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_the_pdf_is_the_paper_the_sheet_claims(fresh_dxf, tmp_path):
    server.draw_line([0, 0], [30000, 0])
    server.drawing_sheet(paper="A3", scale="1:100")
    report = server.drawing_plot_pdf(str(tmp_path / "sheet.pdf"))

    assert report["verified"] is True
    assert report["paper_mm"] == pytest.approx([420.0, 297.0], abs=0.05)
    assert report["asked_for_mm"] == pytest.approx([420.0, 297.0], abs=0.05)


def test_the_measurement_comes_from_the_file_not_from_our_own_number(fresh_dxf, tmp_path):
    """The read-back is the point: the PDF is asked, and an A2 is not an A3."""
    server.drawing_sheet(paper="A2", scale="1:100")
    target = str(tmp_path / "a2.pdf")
    report = server.drawing_plot_pdf(target)

    assert sheet.read_pdf_mediabox_mm(target) == pytest.approx((594.0, 420.0), abs=0.05)
    assert report["paper_mm"] == pytest.approx([594.0, 420.0], abs=0.05)
    assert report["verified"] is True


def test_a_portrait_sheet_plots_portrait(fresh_dxf, tmp_path):
    server.drawing_sheet(paper="A3", scale="1:100", orientation="portrait")
    report = server.drawing_plot_pdf(str(tmp_path / "portrait.pdf"))
    assert report["paper_mm"] == pytest.approx([297.0, 420.0], abs=0.05)


def test_plotting_without_a_sheet_says_what_is_missing(fresh_dxf, tmp_path):
    server.draw_line([0, 0], [6000, 0])
    with pytest.raises(BackendError, match="no sheet"):
        server.drawing_plot_pdf(str(tmp_path / "nosheet.pdf"))


def test_plotting_refuses_on_a_backend_without_paperspace():
    class _NoSheet:
        name = "plugin"
        supports_sheet = False
        sheet: dict = {}

    server._state.reset(_NoSheet(), "plugin")
    try:
        with pytest.raises(BackendError, match="paperspace"):
            server.drawing_plot_pdf()
    finally:
        server._state.backend = None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sheet_plot.py -q`
Expected: FAIL — `AttributeError: module 'acadmcp.server' has no attribute 'drawing_plot_pdf'`.

- [ ] **Step 3: Implement `plot_pdf` on the dxf driver**

In `src/acadmcp/backends/dxf.py`, beside `screenshot`:

```python
    def plot_pdf(
        self, path: str, *, paper: sheet.Paper, scale: tuple[int, int], theme: str = "light"
    ) -> str:
        """Render the sheet to a PDF the size of its paper, with the plan at the plot scale.

        Two axes, and neither computes a transform by hand: the figure's size *is* the paper, so
        the PDF's MediaBox is the paper by construction, and the plan's axes sits over the
        viewport rectangle with its data limits set to the model extent that viewport shows at
        this scale. The axes' limits are the scale; its own clipping is the viewport's edge.
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
            from ezdxf.addons.drawing.config import ColorPolicy, Configuration
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendError("plotting needs matplotlib: uv sync --extra preview") from exc

        if self._sheet_layout is None:
            raise BackendError(
                "this drawing has no sheet to plot: call set_page_setup first, or open a "
                "drawing saved with one"
            )
        background, ink = ("#ffffff", "#000000") if theme == "light" else ("#1e1e1e", "#ffffff")
        figure = plt.figure(
            figsize=(sheet.mm_to_pt(paper.width) / 72.0, sheet.mm_to_pt(paper.height) / 72.0)
        )
        figure.patch.set_facecolor(background)
        config = Configuration(color_policy=ColorPolicy.BLACK, pdsize=1)

        # The sheet, at 1:1 in paper millimetres: the frame and the title block.
        sheet_axes = figure.add_axes((0.0, 0.0, 1.0, 1.0))
        sheet_axes.set_axis_off()
        sheet_axes.set_xlim(0.0, paper.width)
        sheet_axes.set_ylim(0.0, paper.height)
        Frontend(RenderContext(self.doc), MatplotlibBackend(sheet_axes), config=config).draw_layout(
            self._sheet_layout, finalize=False
        )

        # The plan, inside the viewport: these limits are the plot scale.
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
        plan_axes.set_facecolor(background)
        half = sheet.view_height_model(paper, scale) / 2.0
        center_x, center_y = self._plan_center()
        plan_axes.set_xlim(center_x - half, center_x + half)
        plan_axes.set_ylim(center_y - half, center_y + half)
        Frontend(RenderContext(self.doc), MatplotlibBackend(plan_axes), config=config).draw_layout(
            self.msp, finalize=False
        )

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path)
        plt.close(figure)
        self.last_plot = {
            "path": path,
            "paper_mm": [paper.width, paper.height],
            "scale": f"{scale[0]}:{scale[1]}",
            "viewport_mm": [right - left, top - bottom],
        }
        return path
```

- [ ] **Step 4: The tool, which verifies by reading the file back**

In `src/acadmcp/server.py`, after `drawing_screenshot`:

```python
@mcp.tool(annotations=_IDEMPOTENT)
def drawing_plot_pdf(path: str | None = None, theme: str = "light") -> dict[str, Any]:
    """Plot the sheet to a PDF, then read the paper size back out of the PDF itself.

    `verified` answers "is the file the paper the sheet claims", and the size it answers with is
    measured from the PDF's own `/MediaBox`, not from the paper that was asked for — the size a
    plot produced is what the file says it is, not what the layout was told. `mismatch_mm` says
    by how much the two disagree.

    Needs a sheet: call `drawing_sheet` first. The com and plugin backends refuse by name —
    plotting a live drawing goes through AutoCAD's own plotter, which is not wired up here.
    """
    be = _state.require()
    if not getattr(be, "supports_sheet", False):
        raise BackendError(
            f"backend {be.name!r} cannot plot a sheet: only the dxf driver writes paperspace"
        )
    state = dict(getattr(be, "sheet", {}) or {})
    if not state:
        raise BackendError(
            "this drawing has no sheet to plot: call drawing_sheet(paper=..., scale=...) first"
        )
    paper = sheet.resolve_paper(state["paper"], state["orientation"])
    scale = sheet.resolve_scale(state["scale"])
    target = Path(path) if path else _default_screenshot_path().with_suffix(".pdf")
    be.plot_pdf(str(target), paper=paper, scale=scale, theme=theme)

    measured = sheet.read_pdf_mediabox_mm(target)
    asked = (paper.width, paper.height)
    mismatch = max(abs(measured[0] - asked[0]), abs(measured[1] - asked[1]))
    return {
        "ok": True,
        "path": str(target),
        "paper_mm": [round(v, 3) for v in measured],
        "asked_for_mm": [round(v, 3) for v in asked],
        "mismatch_mm": round(mismatch, 3),
        "verified": bool(mismatch <= 0.5),
        "scale": state["scale"],
        "viewport_mm": [
            round(v, 3) for v in (getattr(be, "last_plot", {}) or {}).get("viewport_mm", [])
        ],
    }
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_sheet_plot.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 6: Look at the sheet, which no assertion can do**

Write `examples/output/agent_test/sheet/probe.py` defining `async def draw(tools)` that calls `drawing_new(backend="dxf")`, draws a small plan (four walls, a room with a name, a door), then `drawing_sheet(paper="A3", scale="1:100", title="生产车间平面图", number="JS-01")`, then `drawing_plot_pdf(path="examples/output/agent_test/sheet/sheet.pdf")`, and prints the returned dict. Run it:

```bash
.venv/Scripts/python.exe tests/sandbox_driver.py examples/output/agent_test/sheet/probe.py
```

Then open the PDF and look at it. The frame must sit inside the paper with the wider margin on the left, the title block in the bottom-right with legible Chinese, and the plan filling the viewport at a scale that reads like 1:100 — not spilling over the frame, not a postage stamp in the middle, and not the frame drawn twice at two sizes. A sheet that measures right and looks wrong is still a bug; the measurement and the eye check different things on purpose.

---

### Task 5: Feed the three gates

**Files:**
- Modify: `src/acadmcp/terms.py`, `tests/test_tool_surface.py`, `AGENTS.md`, `README.md`

**Interfaces:**
- Produces: vocabulary records for `drawing_sheet` and `drawing_plot_pdf`; the tool count 47 → 49 wherever it is written.

**Why this is a task, not a chore:** the gates were built to force it. `tests/test_terms.py` fails on a tool with no record, `tests/test_tool_surface.py` pins the count, and `scripts/check_doc_numbers.py` compares the documented counts against the tree. Running the suite without this task produces three failures, which is the design working.

- [ ] **Step 1: Watch the gates fail**

Run: `uv run pytest -q -p no:warnings 2>&1 | tail -20`
Expected: FAIL — `test_every_registered_tool_has_a_term_record` naming both new tools, `test_every_tool_carries_annotations` on the count, and `test_document_numbers_match_the_tree`.

- [ ] **Step 2: Add the vocabulary**

In `src/acadmcp/terms.py`, in the session-and-document group:

```python
    "drawing_sheet": Term(
        cad=("PAGESETUP", "LAYOUT"),
        phrases=("sheet", "图纸", "图框", "标题栏", "幅面", "比例尺", "图纸设置"),
    ),
    "drawing_plot_pdf": Term(
        cad=("PLOT", "PUBLISH", "EXPORTPDF"),
        phrases=("plot", "出图", "打印", "出 pdf", "导出图纸", "打图", "晒图"),
    ),
```

- [ ] **Step 3: Update the counts and the prose**

- `tests/test_tool_surface.py`: `assert len(tools) == 47` → `== 49`. `drawing_sheet` is `_MUTATING` and `drawing_plot_pdf` is `_IDEMPOTENT`, so neither joins the `read_only` or `destructive` sets that the test names.
- `AGENTS.md`: the `server.py` row 47 → 49; add `sheet.py` to the layout block ("paper, scale, frame, title block and the PDF read-back; no CAD imports and nothing from this package"); add `test_sheet.py`, `test_sheet_backend.py`, `test_sheet_plot.py` to the tests block; note in Commands that plotting needs the `preview` extra.
- `README.md`: the `47-tool` row → `49-tool`, plus a short paragraph on the sheet and the plot.
- `AGENTS.md`, a new paragraph beside the journal one: the sheet lives on the drawing's own `LAYOUT` entity; the scale is written to three places that must agree (group 75, group 147, and the ratio); the paper size is verified by reading the PDF's own `/MediaBox`; and only the dxf driver writes paperspace while the live drivers refuse by name.

- [ ] **Step 4: Let the doc gate tell you the number**

Run: `uv run python scripts/check_doc_numbers.py`
Expected: exit 1, naming the test count. Put the number it derives into `AGENTS.md` and run it again until `ok`.

- [ ] **Step 5: Run everything**

Run: `uv run pytest -q -p no:warnings`, then `uv run ruff check . --output-format concise`, then `uv run ruff format --check .`
Expected: PASS, `All checks passed!`, and every file already formatted.

---

## Self-Review

**Spec coverage.** Sheet sizes, margins, frame and title block → Task 1. A page setup that survives into the file → Task 2. The sheet as drawing content on the layout → Task 3. A PDF at the paper's own size with the plan at the plot scale, measured back → Task 4. Discovery, counters and documentation → Task 5.

**Placeholders.** None. Every step names a file, a command and an expected result, and the four pieces of real logic — the sheet maths, the page-setup fields, the paperspace drawing, and the composition — carry complete code. Two steps are deliberately *not* fully specified because they are measurements rather than code: Task 2 Step 7 (confirm the group-75 enum against a drawing AutoCAD wrote) and Task 4 Step 6 (open the PDF and look at it). Both say exactly what to measure and what would falsify the value in the plan.

**Type consistency.** `sheet.Paper` is what `set_page_setup` and `plot_pdf` both take; `tuple[int, int]` is what `resolve_scale` returns and both take; `paper_primitives` takes and returns what `_apply` does (`list[dict]` in, `list[str]` out); `paper_viewport` returns a handle string. `drawing_plot_pdf` reads `state["paper"]`, `state["orientation"]` and `state["scale"]` — the three keys `set_page_setup` puts in `self.sheet`, which Task 3's `drawing_status` test also asserts. `self._sheet_layout` (the layout object) and `self.sheet` (the JSON-able dict) are distinct everywhere they appear.

**Length.** Within budget. Tasks 1–4 are the design; Task 5 is a checklist, because the gates derive the numbers rather than this document.

**Two things this plan does not do**, both named in Scope Check: the template library, and the catalogue's seven block-only entries. A third is named as a boundary rather than an omission: project-supplied paper sizes and margins belong with the `acadmcp.json` schema, so `sheet.py`'s defaults are this package's for now.

---

## Deviations, as implemented

Kept here rather than in the code, because a plan that disagrees with the tree is worse than no
plan.

1. **One paperspace method, not two.** The plan gave the protocol both `paper_primitives` and
   `paper_viewport`, each returning handles. A step defers its drawing to the commit, so no
   handle exists at call time and neither could: `paper_primitives` now takes the whole batch —
   the frame, the title block *and* a `viewport` primitive — and returns a count. One method, one
   destination, and the viewport is drawn by the same machinery as everything else.
2. **`sheet.py` imports `layers`.** The plan said "nothing from this package". `layers.text_width`
   and `layers.CJK_ADVANCE` are that measured fact's one home, and a second copy of a measured
   number is exactly what drifts. `sheet` re-exports them, so callers still need one import.
   The rule is `summary.py`'s: `geom` and `layers`, nothing else, and no CAD library.
3. **The refusal lives on `RecordingBackend`, not on each live driver.** Both inherit it, so
   there is one message and one place to change. `supports_sheet = False` is the shared default
   and `DxfBackend` overrides it.
4. **`plan_extent` and the fit report are additions.** Not in the plan; found by doing the thing —
   a 24 m bay at 1:100 on an A3 came back `ok: true` with the building running off the page. The
   sheet and the page were each correct and useless together, so `drawing_sheet` now reports
   `plan_mm`, `fits` and, when it does not fit, the largest scale that holds it.
5. **A second `drawing_sheet` is refused.** Also not in the plan; two calls left 22 lines on
   `A-SHEET` where there should be 11.
6. **`entities()` lists the paper layouts too**, and `_erase` routes through the layout that owns
   an entity (`msp.delete_entity` on a paperspace entity raises). Neither was in the plan and both
   were forced by `drawing_verify` and undo.
7. **The plan's numbers were checked and two were wrong**: the viewport span on an A3 is 267 mm
   (not 272), so 1:100 shows 26 700 model mm; and the title block draws 7 rulings, not 4, so a
   two-field sheet records 16 entities and a one-field sheet 14. The tests derive these from
   `sheet` rather than repeating them.
8. **Two measurements remain unverified**, and both say so in the code: `STANDARD_SCALE_TYPES`
   (DXF group 75) was transcribed rather than read off a drawing AutoCAD wrote, and the plot has
   only been eyeballed in this package's own renderer, never printed from AutoCAD.
