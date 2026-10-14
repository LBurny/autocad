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

# Default construction sizes in mm.
WALL_THICKNESS = 200.0
DOOR_WIDTH = 900.0
WINDOW_WIDTH = 1500.0
COLUMN_SIZE = 400.0
AXIS_BUBBLE_R = 400.0
DIM_OFFSET = 1200.0
DIM_TEXT_GAP = 700.0
