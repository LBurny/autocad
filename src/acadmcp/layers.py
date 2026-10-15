"""Drawing standard: layer names, AutoCAD Color Index values and text heights.

The scheme follows the Chinese architectural drafting convention (GB/T 50001)
that the reference drawing uses, so an AI-authored plan lands on familiar
layers instead of everything piling up on layer 0.
"""

from __future__ import annotations

# mm is the unit for every dimension in this package.
UNITS = "mm"

LAYERS: dict[str, tuple[int, str]] = {
    # name: (ACI colour, linetype)
    "A-WALL": (2, "CONTINUOUS"),
    "A-WALL-PATT": (8, "CONTINUOUS"),
    "A-DOOR": (4, "CONTINUOUS"),
    "A-WINDOW": (4, "CONTINUOUS"),
    "A-COLUMN": (7, "CONTINUOUS"),
    "S-COLUMN": (7, "CONTINUOUS"),
    "S-BEAM": (7, "CONTINUOUS"),
    "A-STAIR": (2, "CONTINUOUS"),
    "A-LIFT": (6, "CONTINUOUS"),
    "A-SHAFT": (30, "CONTINUOUS"),
    "A-ROOM-BOUND": (8, "CONTINUOUS"),
    "A-ROOM-NAME": (7, "CONTINUOUS"),
    "A-ROOM-AREA": (7, "CONTINUOUS"),
    "A-DIM": (3, "CONTINUOUS"),
    "A-AXIS": (1, "CENTER"),
    "A-AXIS-TEXT": (1, "CONTINUOUS"),
    "A-EQUIP": (6, "CONTINUOUS"),
    "A-ANNO": (7, "CONTINUOUS"),
    "A-HATCH": (9, "CONTINUOUS"),
}

# Text heights in mm, matched to a 1:100 plot.
H_TEXT = 300.0
H_ROOM_NAME = 350.0
H_ROOM_AREA = 250.0
H_DIM = 250.0
H_TAG = 180.0
H_AXIS = 500.0

#: Unit suffix for reported areas. Superscript-two rather than the single-codepoint
#: U+33A1 square-metre sign: measured in AutoCAD, U+33A1 renders as "?" under the
#: gbcbig.shx big font and under every CJK TrueType face tested, while U+00B2
#: renders under all of them.
AREA_UNIT = "m²"

#: Hatch pattern names every backend knows — the classic acad.pat set plus SOLID.
#: Validated against before a hatch is drawn: a misspelt name is otherwise accepted
#: silently (ezdxf builds a one-line stub pattern), which is exactly the "named
#: variant that does not exist" failure the element catalogue refuses to allow.
HATCH_PATTERNS: frozenset[str] = frozenset(
    [
        "SOLID",
        "ANGLE",
        "ANSI31",
        "ANSI32",
        "ANSI33",
        "ANSI34",
        "ANSI35",
        "ANSI36",
        "ANSI37",
        "ANSI38",
        "AR-B816",
        "AR-B816C",
        "AR-B88",
        "AR-BRELM",
        "AR-BRSTD",
        "AR-CONC",
        "AR-HBONE",
        "AR-PARQ1",
        "AR-RROOF",
        "AR-RSHKE",
        "AR-SAND",
        "BOX",
        "BRASS",
        "BRICK",
        "BRSTONE",
        "CLAY",
        "CORK",
        "CROSS",
        "DASH",
        "DOLMIT",
        "DOTS",
        "EARTH",
        "ESCHER",
        "FLEX",
        "GOST_GLASS",
        "GRND",
        "HOUND",
        "INSUL",
        "LINE",
        "MUDST",
        "NET",
        "NET3",
        "PLAST",
        "PLASTI",
        "SACNCR",
        "SQUARE",
        "STARS",
        "STEEL",
        "SWAMP",
        "TRANS",
        "TRIANG",
        "ZIGZAG",
    ]
)

#: Advance width of a character as a multiple of the text height. Measured rather than
#: assumed: "生产车间" at height 350 is 1991 mm wide in AutoCAD and 1973 mm in the PNG
#: preview, so a Chinese character takes 1.42 x the height, not 1.0. Anything that sizes
#: a caption to sit inside something has to use this, or the caption overflows.
CJK_ADVANCE = 1.42
LATIN_ADVANCE = 0.75


def text_width(text: str, height: float) -> float:
    """How wide `text` draws at `height`, close enough to keep a caption in its box."""
    wide = sum(1 for ch in text if ord(ch) > 0x2E7F)  # CJK, and full-width punctuation
    return height * (wide * CJK_ADVANCE + (len(text) - wide) * LATIN_ADVANCE)


def fit_text_height(text: str, width: float, height: float, *, fit: float = 0.9) -> float:
    """The largest height up to `height` at which `text` fits across `width`.

    For captions meant to sit inside something drawn around them — a shaft, a piece of
    equipment — so a long name shrinks instead of spilling over the line.
    """
    per_unit = text_width(text, 1.0)
    if per_unit <= 0:
        return float(height)
    return min(float(height), width * fit / per_unit)


# Default construction sizes in mm.
WALL_THICKNESS = 200.0
DOOR_WIDTH = 900.0
WINDOW_WIDTH = 1500.0
COLUMN_SIZE = 400.0
AXIS_BUBBLE_R = 400.0
DIM_OFFSET = 1200.0
DIM_TEXT_GAP = 700.0
