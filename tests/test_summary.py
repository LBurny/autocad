"""The design summary, recomputed from the journal's recorded intent.

No backend, no CAD and no files: the rows are hand-built, in the shape both journal owners
report them, because that is what `summarise` reads.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import summary  # noqa: E402


def _step(index, op, params, state="applied", label=""):
    return {"index": index, "op": op, "label": label, "params": params, "state": state}


ROOM = _step(
    1,
    "draw_room",
    {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "name": "会议室", "thickness": 200},
    label="会议室",
)
WALL = _step(
    2,
    "draw_wall_with_openings",
    {
        "points": [[0, 0], [6000, 0]],
        "thickness": 240,
        "openings": [{"start": 1000, "width": 900, "kind": "door", "tag": "M1521"}],
    },
)
DOOR = _step(3, "place_element", {"type": "door", "at": [1000, 0], "variant": "900"})


def test_rooms_carry_the_area_the_room_tool_would_report():
    out = summary.summarise([ROOM])
    assert out["rooms"][0]["name"] == "会议室"
    # 4000x3000 ring, 200 walls -> 3.8 x 2.8. `pytest.approx` rather than `==` because the
    # plan wrote `3.8 * 2.8` and that float is 10.639999999999999, while the ring that is
    # actually drawn measures 10.64 — the room tool's own reply.
    assert out["rooms"][0]["area_m2"] == pytest.approx(3.8 * 2.8, abs=0.001)
    assert out["rooms"][0]["boundary"] == [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
    assert out["rooms"][0]["thickness"] == 200.0


def test_openings_are_listed_with_their_tag_and_width():
    out = summary.summarise([WALL])
    assert out["openings"] == [
        {
            "step": 2,
            "kind": "door",
            "tag": "M1521",
            "width": 900.0,
            "swing": "left",
            "element": None,
        }
    ]


def test_placed_elements_are_listed_once_per_call():
    out = summary.summarise([DOOR])
    assert out["elements"][0]["type"] == "door" and out["elements"][0]["variant"] == "900"
    assert out["elements"][0]["count"] == 1


def test_a_repeating_placement_reports_its_count():
    out = summary.summarise(
        [_step(1, "place_along", {"type": "column", "start": [0, 0], "end": [6000, 0], "count": 3})]
    )
    assert out["elements"][0]["count"] == 3
    assert out["structure"]["columns"] == 3


def test_an_undone_step_is_counted_but_not_in_the_plan():
    out = summary.summarise([ROOM, _step(2, "draw_wall", {"points": [[0, 0], [1, 0]]}, "undone")])
    assert out["steps"] == {"total": 2, "applied": 1, "undone": 1, "next_index": 3}
    assert len(out["rooms"]) == 1
    assert any("undone" in w for w in out["warnings"])


def test_a_room_with_no_name_is_still_reported():
    out = summary.summarise(
        [_step(1, "draw_room", {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]})]
    )
    assert out["rooms"][0]["name"] == "" and out["rooms"][0]["area_m2"] > 0


def test_layers_are_collected_from_the_steps():
    out = summary.summarise(
        [_step(1, "draw_line", {"p1": [0, 0], "p2": [1, 0], "layer": "A-WALL"})]
    )
    assert "A-WALL" in out["layers"]


def test_an_unreadable_step_is_warned_about_not_raised():
    out = summary.summarise([{"op": "draw_room"}])
    assert out["steps"]["total"] == 1
    assert out["warnings"]


# --- the decisions the plan left open --------------------------------------


def test_a_clean_summary_carries_no_warnings():
    """The warnings list is a signal, so an empty one has to stay empty."""
    out = summary.summarise([ROOM, WALL, DOOR])
    assert out["warnings"] == []
    assert out["steps"]["applied"] == 3 and out["steps"]["undone"] == 0


def test_a_directly_drawn_door_is_an_opening_too():
    """ "How many doors, and what sizes" is this module's whole purpose, and `draw_door`
    hangs one just as surely as a wall run's opening does."""
    out = summary.summarise(
        [_step(1, "draw_door", {"hinge": [0, 0], "width": 900, "swing": "right", "tag": "M-1"})]
    )
    assert out["openings"][0]["kind"] == "door" and out["openings"][0]["width"] == 900.0
    assert out["openings"][0]["tag"] == "M-1" and out["openings"][0]["swing"] == "right"


def test_a_directly_drawn_window_has_no_swing_to_report():
    """`draw_window` has no swing, so the summary invents none."""
    out = summary.summarise([_step(1, "draw_window", {"start": [0, 0], "width": 1200})])
    assert out["openings"][0]["kind"] == "window" and out["openings"][0]["swing"] is None


def test_a_placement_rotation_the_row_never_recorded_is_null():
    """`place_element` does not put its rotation in the step, and a 0.0 would be a fact the
    drawing never promised."""
    out = summary.summarise([DOOR])
    assert out["elements"][0]["rotation"] is None


def test_a_placement_that_did_record_a_rotation_reports_it():
    out = summary.summarise(
        [_step(1, "place_element", {"type": "door", "at": [0, 0], "rotation": 90})]
    )
    assert out["elements"][0]["rotation"] == 90.0 and out["elements"][0]["at"] == [[0.0, 0.0]]


def test_a_run_reports_its_ends_and_derives_its_count_from_a_spacing():
    out = summary.summarise(
        [
            _step(
                1,
                "place_along",
                {"type": "column", "start": [0, 0], "end": [6000, 0], "spacing": 2000},
            )
        ]
    )
    assert out["elements"][0]["count"] == 4
    assert out["elements"][0]["at"] == [[0.0, 0.0], [6000.0, 0.0]]
    assert out["structure"]["columns"] == 4


def test_a_grid_counts_every_intersection():
    out = summary.summarise(
        [_step(1, "place_grid", {"type": "column", "xs": [0, 3000, 6000], "ys": [0, 4000]})]
    )
    assert out["elements"][0]["count"] == 6 and len(out["elements"][0]["at"]) == 6
    assert out["structure"]["columns"] == 6


def test_a_structural_op_counts_into_its_own_bucket():
    out = summary.summarise(
        [
            _step(1, "draw_column", {"center": [0, 0], "size": 600}),
            _step(2, "draw_stair", {"corner1": [0, 0], "corner2": [3000, 1200]}),
            _step(3, "draw_lift", {"corner1": [0, 0], "corner2": [2400, 2200]}),
            _step(4, "draw_equipment", {"corner1": [0, 0], "corner2": [800, 600], "kind": "shaft"}),
            _step(5, "draw_equipment", {"corner1": [0, 0], "corner2": [800, 600]}),
        ]
    )
    assert out["structure"] == {
        "columns": 1,
        "stairs": 1,
        "lifts": 1,
        "shafts": 1,
        "equipment": 1,
    }


def test_annotation_counts_one_per_step_not_one_per_entity():
    out = summary.summarise(
        [
            _step(1, "add_dimension", {"points": [[0, 0], [1000, 0], [3000, 0], [6000, 0]]}),
            _step(2, "add_text", {"pos": [0, 0], "text": "车间"}),
            _step(3, "add_leader_note", {"anchor": [0, 0], "text": "见详图"}),
            # The op names here must be the ones `server.py` actually records. This row read
            # "add_axis_grid" — a name no tool has ever produced — so the handler it was meant
            # to exercise was dead and `axis_grids` was 0 for every real drawing while this
            # test stayed green. See `test_the_op_names_are_the_ones_tools_record` below.
            _step(4, "draw_axis_grid", {"x_positions": [0, 3000], "y_positions": [0]}),
            _step(5, "add_hatch", {"points": [[0, 0], [1000, 0], [1000, 1000]]}),
        ]
    )
    assert out["annotation"] == {
        "dimensions": 1,
        "texts": 1,
        "leaders": 1,
        "axis_grids": 1,
        "hatches": 1,
    }


def test_every_handler_key_is_an_op_a_tool_actually_records():
    """A key no tool produces is dead code that reports a number nobody can move.

    `summary.HANDLERS` is keyed by the op each tool records — `_auto_step("op", …)` for the
    drawing tools and `_placement(be, "op", …)` for the three that place catalogue elements.
    Reading the keys off `server.py` rather than trusting a hand-written list is what keeps
    the two in step; one of them had silently drifted to a name no tool has ever produced.
    """
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "acadmcp" / "server.py"
    text = source.read_text(encoding="utf-8")
    recorded = set(re.findall(r'_auto_step\(\s*"([a-z_]+)"', text))
    recorded |= set(re.findall(r'_placement\(\s*\w+\s*,\s*"([a-z_]+)"', text))
    assert recorded, "no op names found; the patterns above have drifted, not the code"
    unknown = sorted(set(summary.HANDLERS) - recorded)
    assert not unknown, f"summary handlers for ops no tool records: {unknown}"


def test_the_catalogue_identity_is_echoed_but_not_guessed():
    identity = {"count": 9, "digest": "sha256:abc", "library": "C:/lib"}
    out = summary.summarise([DOOR], catalog=identity)
    assert out["catalog"] == {"count": 9, "digest": "sha256:abc", "library": "C:/lib"}
    assert summary.summarise([DOOR])["catalog"] is None


def test_a_catalogue_that_reclassifies_an_element_stops_counting_it_as_structural():
    """The entry is the authority on what it is, not its key."""
    rows = [_step(1, "place_element", {"type": "column", "at": [0, 0]})]
    other = {"elements": [{"key": "column", "category": "other"}]}
    report = {"categories": {"structure": [{"key": "column", "category": "structure"}]}}
    assert summary.summarise(rows, catalog=other)["structure"]["columns"] == 0
    assert summary.summarise(rows, catalog=report)["structure"]["columns"] == 1
    assert summary.summarise(rows)["structure"]["columns"] == 1


def test_the_undone_tail_is_one_warning_however_long_it_is():
    out = summary.summarise(
        [ROOM, _step(2, "draw_line", {}, "undone"), _step(3, "draw_line", {}, "undone")]
    )
    assert out["warnings"] == ["steps 2-3 are undone (2 steps): the drawing is mid-edit"]


def test_a_row_that_is_not_a_step_row_is_counted_and_warned():
    out = summary.summarise([ROOM, "not a step", None])
    assert out["steps"]["total"] == 3 and len(out["rooms"]) == 1
    assert len(out["warnings"]) == 2 and all("not a step row" in w for w in out["warnings"])


def test_malformed_params_are_warned_about_by_step_number_and_never_raised():
    out = summary.summarise(
        [
            _step(4, "draw_room", "not a mapping"),
            _step(5, "draw_room", {"boundary": "nonsense"}),
        ]
    )
    assert out["rooms"] == [] and out["steps"]["applied"] == 2
    assert any(w.startswith("step 4") for w in out["warnings"])
    assert any(w.startswith("step 5") for w in out["warnings"])


def test_a_wall_run_whose_recorded_openings_are_unreadable_is_warned_about():
    """The op always records its openings, so a row without them is damaged — and the doors
    it cut are unknown, which a caller has to be told."""
    out = summary.summarise(
        [
            _step(2, "draw_wall_with_openings", {"points": [[0, 0], [6000, 0]]}),
            _step(3, "draw_wall_with_openings", {"points": [[0, 0], [6000, 0]], "openings": []}),
        ]
    )
    assert out["openings"] == []
    assert [w for w in out["warnings"] if w.startswith("step 2")]
    assert not [w for w in out["warnings"] if w.startswith("step 3")]


def test_no_rows_at_all_is_an_empty_summary_not_an_error():
    out = summary.summarise([])
    assert out["steps"] == {"total": 0, "applied": 0, "undone": 0, "next_index": 1}
    assert out["rooms"] == [] and out["layers"] == [] and out["warnings"] == []


def test_a_boundary_layer_is_a_layer_the_plan_uses():
    out = summary.summarise(
        [
            _step(
                1,
                "draw_room",
                {"boundary": [[0, 0], [4000, 0], [4000, 3000]], "bound_layer": "A-ROOM-BOUND"},
            )
        ]
    )
    assert out["layers"] == ["A-ROOM-BOUND"]


def test_the_summary_module_stays_cad_free():
    """The constraint the whole design rests on: `summary.py` may import `geom` and
    `layers` and nothing else — never ezdxf, never a backend. Checked statically, because
    by the time this test runs the rest of the suite has already imported ezdxf."""
    source = Path(summary.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    absolute, relative = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.update(alias.name.split(".")[0] for alias in node.names)
            else:
                absolute.add((node.module or "").split(".")[0])
    assert absolute <= {"__future__", "collections", "dataclasses", "typing"}, absolute
    assert relative <= {"geom", "layers"}, relative
