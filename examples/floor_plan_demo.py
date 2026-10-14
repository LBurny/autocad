"""Generate a floor plan that mirrors the reference sheet: structural grid,
walled envelope with tagged doors and windows, an aseptic suite, a stair core,
two goods lifts and stacked riser shafts — every room labelled with its
computed area.

    python examples/floor_plan_demo.py            # writes DXF (+ PNG if matplotlib)
    python examples/floor_plan_demo.py --com      # draws it into running AutoCAD

All dimensions are millimetres and all walls are 200 mm thick, so the areas the
tools report are exactly the areas the geometry encloses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402

# --- overall envelope, on a 0/6000/12000 x 0/6000/14000 structural grid -------
W, D = 12000.0, 14000.0
T = 200.0  # every wall, exterior and interior alike
Y_BOT, Y_MID = 3600.0, 8000.0


def build(be) -> dict:
    ops.setup_layers(be)
    report: dict = {"rooms": []}

    # --- structural grid (echoes the 7-8-9 / D-E-F axes of the reference) -----
    ops.draw_axis_grid(
        be,
        x_positions=[0.0, 6000.0, 12000.0],
        y_positions=[0.0, 6000.0, 14000.0],
        x_labels=["7", "8", "9"],
        y_labels=["D", "E", "F"],
        extent=(-1200.0, -1200.0, W + 1200.0, D + 1200.0),
    )

    # --- envelope, drawn as four runs so the openings are real ----------------
    ops.draw_wall_with_openings(
        be,
        [(0.0, 0.0), (W, 0.0)],
        [
            {"start": 1200, "width": 1500, "kind": "door", "swing": "left", "tag": "FM\u4e591521"},
            {"start": 4200, "width": 1200, "kind": "window", "tag": "C1212"},
            {"start": 8400, "width": 1200, "kind": "window", "tag": "C1212"},
        ],
        T,
    )
    ops.draw_wall_with_openings(
        be,
        [(W, 0.0), (W, D)],
        [
            # Window tags are pushed outboard of the facade so they clear the
            # lift lobbies drawn against the inside face.
            {"start": 2800, "width": 1800, "kind": "window", "tag": "C1821", "tag_offset": -500},
            {"start": 9800, "width": 1800, "kind": "window", "tag": "C1821", "tag_offset": -500},
        ],
        T,
    )
    ops.draw_wall_with_openings(
        be,
        [(W, D), (0.0, D)],
        [{"start": 2400, "width": 1800, "kind": "window", "tag": "C1821"}],
        T,
    )
    ops.draw_wall_with_openings(
        be,
        [(0.0, D), (0.0, 0.0)],
        [
            {"start": 2000, "width": 1200, "kind": "window", "tag": "C1212"},
            {"start": 8000, "width": 1200, "kind": "window", "tag": "C1212"},
        ],
        T,
    )

    # --- interior partitions --------------------------------------------------
    # Middle band / top band, with the doors into 原料间 and 冷库.
    ops.draw_wall_with_openings(
        be,
        [(0.0, Y_MID), (W, Y_MID)],
        [
            {"start": 1200, "width": 1500, "kind": "door", "swing": "left", "tag": "M1522"},
            {"start": 8000, "width": 1200, "kind": "door", "swing": "right", "tag": "M122"},
        ],
        T,
    )
    # Middle band / bottom band: the fire door out to the lift lobby and the
    # stair's open mouth.
    ops.draw_wall_with_openings(
        be,
        [(0.0, Y_BOT), (W, Y_BOT)],
        [
            {"start": 1500, "width": 1000, "kind": "door", "swing": "right", "tag": "M1022"},
            {"start": 6700, "width": 2700, "kind": "opening"},
        ],
        T,
    )
    # 无菌室 west wall of the buffer/changing pair.
    ops.draw_wall_with_openings(
        be,
        [(3000.0, Y_BOT), (3000.0, Y_MID)],
        [{"start": 2400, "width": 900, "kind": "door", "swing": "left", "tag": "M0921"}],
        T,
    )
    # Buffer / changing split, and the balance-room / lab split. Both tags are
    # pushed through to the far side of the wall so they do not pile up in the
    # middle of this tight room cluster.
    ops.draw_wall_with_openings(
        be,
        [(3000.0, 5900.0), (4400.0, 5900.0)],
        [
            {
                "start": 350,
                "width": 700,
                "kind": "door",
                "swing": "left",
                "tag": "M0821",
                "tag_offset": -350,
            }
        ],
        T,
    )
    ops.draw_wall_with_openings(
        be,
        [(4400.0, 5900.0), (6600.0, 5900.0)],
        [
            {
                "start": 250,
                "width": 750,
                "kind": "door",
                "swing": "left",
                "tag": "M0821",
                "tag_offset": -350,
            }
        ],
        T,
    )
    ops.draw_wall_with_openings(
        be,
        [(4400.0, Y_BOT), (4400.0, Y_MID)],
        [{"start": 3300, "width": 900, "kind": "door", "swing": "left", "tag": "M0921"}],
        T,
    )
    ops.draw_wall_with_openings(
        be,
        [(6600.0, Y_BOT), (6600.0, Y_MID)],
        [{"start": 600, "width": 1000, "kind": "door", "swing": "left"}],
        T,
    )

    # --- rooms: label only, the walls above already bound them ----------------
    for ring, name in ROOMS:
        report["rooms"].append(ops.add_room_label(be, ring, name, T))

    # --- core: stair, goods lifts, riser shafts -------------------------------
    ops.draw_stair(
        be,
        (6700.0, Y_BOT + 100),
        (9500.0, Y_MID - 100),
        steps=9,
        axis="y",
        up_label="\u4e0a",
        down_label="\u4e0b",
    )
    ops.draw_lift(be, (6100.0, 100.0), (8900.0, Y_BOT - 100), tag="1#\u8d27\u68af1.6T")
    ops.draw_lift(be, (9100.0, 100.0), (11900.0, Y_BOT - 100), tag="2#\u8d27\u68af1.6T")

    ops.draw_shaft(be, (9700.0, 6400.0), (11900.0, Y_MID - 100), "\u5f3a\u7535\u4e95")
    ops.draw_shaft(be, (9700.0, 5050.0), (11900.0, 6300.0), "\u5f31\u7535\u4e95")
    ops.draw_shaft(be, (9700.0, Y_BOT + 100), (11900.0, 4950.0), "\u6c34\u4e95")

    # --- fixed equipment and notes -------------------------------------------
    ops.draw_equipment(be, (4550.0, 5250.0), (6450.0, 5750.0), "\u5b9e\u9a8c\u53f0")
    ops.draw_equipment(be, (7400.0, 8300.0), (9000.0, 8900.0))
    ops.leader_note(be, (8200.0, 8600.0), "\u8d27\u67b6 2400\u00d7800", direction=45.0)

    # --- dimensions -----------------------------------------------------------
    ops.dimension_chain(be, [(0.0, 0.0), (6000.0, 0.0), (W, 0.0)], offset=-1600.0)
    ops.dimension_chain(be, [(0.0, 0.0), (0.0, 6000.0), (0.0, D)], offset=1600.0)

    report["entities"] = be.info()["entities"]
    return report


#: (centreline ring, room name) — kept apart from the wall calls so the plan's
#: room schedule stays readable.
ROOMS: list[tuple[list[tuple[float, float]], str]] = [
    ([(0, Y_MID), (7200, Y_MID), (7200, D), (0, D)], "\u539f\u6599\u95f4"),
    ([(7200, Y_MID), (W, Y_MID), (W, D), (7200, D)], "\u51b7\u5e93"),
    ([(0, Y_BOT), (3000, Y_BOT), (3000, Y_MID), (0, Y_MID)], "\u65e0\u83cc\u5ba4"),
    ([(3000, 5900), (4400, 5900), (4400, Y_MID), (3000, Y_MID)], "\u7f13\u51b2"),
    ([(3000, Y_BOT), (4400, Y_BOT), (4400, 5900), (3000, 5900)], "\u66f4\u8863"),
    ([(4400, 5900), (6600, 5900), (6600, Y_MID), (4400, Y_MID)], "\u5929\u5e73\u5ba4"),
    ([(4400, Y_BOT), (6600, Y_BOT), (6600, 5900), (4400, 5900)], "\u7406\u5316\u5ba4"),
    ([(0, 0), (6000, 0), (6000, Y_BOT), (0, Y_BOT)], "\u95e8\u5385"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--com", action="store_true", help="draw into the running AutoCAD")
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "output" / "floor_plan.dxf"),
        help="output DXF path (dxf backend only)",
    )
    ap.add_argument("--no-preview", action="store_true", help="skip the PNG render")
    args = ap.parse_args()

    be = make_backend("com", new_document=True) if args.com else make_backend("dxf")

    report = build(be)
    print(f"backend           : {be.name}")
    print(f"rooms labelled    : {len(report['rooms'])}")
    for room in report["rooms"]:
        print(f"  {room['name']:<6} {room['area_m2']:>7.2f} m2")
    print(f"entities drawn    : {report['entities']}")

    if args.com:
        zoom = getattr(be, "zoom_extents", None)
        if zoom:
            zoom()
        print("drawn into the live AutoCAD drawing")
        return 0

    out = be.save(args.out)
    print(f"saved DXF         : {out}")
    if not args.no_preview:
        try:
            png = be.preview(str(Path(out).with_suffix(".png")))
            print(f"rendered preview  : {png}")
        except Exception as exc:  # noqa: BLE001 - preview is optional
            print(f"preview skipped   : {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
