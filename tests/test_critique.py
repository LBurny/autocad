"""The component critique, checked against the journal's own account of the drawing.

No backend, no CAD and no files for most of these: the rows are hand-built, in the shape both
journal owners report them — ``index``, ``op``, ``label``, ``params``, ``state`` — because that
is what ``critique.run`` reads. One test drives a real dxf backend, so at least one pass is
honest about the shape a live backend actually produces, and one pins the op names the checks
read against the ops ``server.py`` really records.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import critique, ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402

#: The catalogue's tag prefixes for the two opening kinds, as the orchestrator hands them in.
PREFIXES = {"door": "M", "window": "C"}


def _step(index, op, params, state="applied", label=""):
    return {"index": index, "op": op, "label": label, "params": params, "state": state}


def _door(hinge, tag=None, width=900.0, index=2, state="applied"):
    return _step(
        index,
        "draw_door",
        {
            "hinge": list(hinge),
            "width": width,
            "wall_thickness": 200.0,
            "rotation": 0.0,
            "open_angle": 90.0,
            "swing": "left",
            "tag": tag,
        },
        state=state,
    )


def _run(points, openings, index=1, closed=False, state="applied"):
    return _step(
        index,
        "draw_wall_with_openings",
        {
            "points": [list(p) for p in points],
            "openings": openings,
            "thickness": 200.0,
            "hatch": False,
            "closed": closed,
        },
        state=state,
    )


RUN = _run(
    [(0, 0), (6000, 0)],
    [{"start": 1000, "width": 900, "kind": "door", "tag": "M0921"}],
)
DOOR_IN_OPENING = _door((1000, 0), tag="M0921")
# Tagless doors: the check under test is the door against its wall, and the run above already
# carries the opening's tag, so a second one would be a genuine duplicate tag as well.
DOOR_ON_UNCUT = _door((5000, 0))
DOOR_FLOATING = _door((50000, 50000))


def _codes(report):
    return [issue["code"] for issue in report["issues"]]


# ---------------------------------------------------------------------------
# 1. a door and the opening it belongs in
# ---------------------------------------------------------------------------


def test_a_door_on_its_recorded_opening_is_clean():
    report = critique.run([RUN, DOOR_IN_OPENING], PREFIXES, focus="door_without_opening")
    assert report["issues"] == []
    assert report["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_a_door_on_a_wall_with_no_opening_warns():
    report = critique.run([RUN, DOOR_ON_UNCUT], PREFIXES)
    assert _codes(report) == ["door_without_opening"]
    assert report["issues"][0]["severity"] == "warning"
    assert "uncut" in report["issues"][0]["message"]


def test_a_door_on_a_wall_drawn_without_openings_warns_too():
    wall = _step(1, "draw_wall", {"points": [[0, 0], [6000, 0]], "thickness": 200.0})
    report = critique.run([wall, _door((3000, 0))], PREFIXES, focus="door_without_opening")
    assert report["issues"][0]["severity"] == "warning"


def test_a_door_on_no_recorded_wall_at_all_is_an_error():
    report = critique.run([RUN, DOOR_FLOATING], PREFIXES)
    assert len(report["issues"]) == 1
    assert report["issues"][0]["severity"] == "error"
    assert "wall" in report["issues"][0]["message"]
    assert report["counts"]["error"] == 1


def test_a_room_boundary_counts_as_a_wall_but_carries_no_openings():
    room = _step(
        1,
        "draw_room",
        {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "walls": True},
    )
    on_ring = _door((2000, 0))
    assert (
        critique.run([room, on_ring], PREFIXES, focus="door_without_opening")["issues"][0][
            "severity"
        ]
        == "warning"
    )  # on the ring, but a room records no opening to have cut
    far_away = _door((100000, 100000))
    assert (
        critique.run([room, far_away], PREFIXES, focus="door_without_opening")["issues"][0][
            "severity"
        ]
        == "error"
    )


def test_a_room_whose_walls_were_never_drawn_is_no_place_for_a_door():
    room = _step(
        1,
        "draw_room",
        {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "walls": False},
    )
    report = critique.run([room, _door((2000, 0))], PREFIXES, focus="door_without_opening")
    assert report["issues"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# 2. an opening measured along its run
# ---------------------------------------------------------------------------

PAST_END = _run(
    [(0, 0), (10000, 0)],
    [{"start": 9500, "width": 1000, "kind": "door", "tag": "M0921"}],
)
NEGATIVE = _run(
    [(0, 0), (10000, 0)],
    [{"start": -100, "width": 900, "kind": "door", "tag": "M0921"}],
)


def test_an_opening_past_the_end_of_its_run_is_an_error():
    report = critique.run([PAST_END], PREFIXES, focus="opening_fits_run")
    assert report["issues"][0]["severity"] == "error"
    assert "9500" in report["issues"][0]["message"]


def test_a_negative_opening_start_is_an_error():
    report = critique.run([NEGATIVE], PREFIXES, focus="opening_fits_run")
    assert report["issues"][0]["severity"] == "error"


def test_a_closed_runs_closing_leg_counts_towards_its_length():
    ring = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]  # perimeter 14000
    fits = _run(ring, [{"start": 13000, "width": 500, "kind": "door"}], closed=True)
    past = _run(ring, [{"start": 14000, "width": 500, "kind": "door"}], closed=True)
    assert critique.run([fits], PREFIXES, focus="opening_fits_run")["issues"] == []
    assert (
        critique.run([past], PREFIXES, focus="opening_fits_run")["issues"][0]["severity"] == "error"
    )


# ---------------------------------------------------------------------------
# 3 and 4. tags
# ---------------------------------------------------------------------------


def test_a_tag_that_is_not_a_prefix_and_four_digits_warns():
    report = critique.run([_door((0, 0), tag="M921")], PREFIXES, focus="tag_format")
    issue = report["issues"][0]
    assert issue["code"] == "tag_format" and issue["severity"] == "warning"
    assert "M921" in issue["message"]


def test_a_prefix_and_four_digits_is_clean():
    assert critique.run([_door((0, 0), tag="M0921")], PREFIXES, focus="tag_format")["issues"] == []


def test_a_kind_with_no_catalogue_entry_still_needs_letters_and_four_digits():
    opening = _run(
        [(0, 0), (6000, 0)],
        [
            {"start": 1000, "width": 900, "kind": "opening", "tag": "M0921"},
            {"start": 3000, "width": 900, "kind": "opening", "tag": "921"},
        ],
    )
    report = critique.run([opening], PREFIXES, focus="tag_format")
    assert [issue["detail"]["tag"] for issue in report["issues"]] == ["921"]


def test_an_untagged_opening_is_not_a_tag_problem():
    untagged = _run([(0, 0), (6000, 0)], [{"start": 1000, "width": 900, "kind": "door"}])
    assert critique.run([untagged], PREFIXES, focus="tag_format")["issues"] == []


def test_two_rows_with_the_same_tag_warn_naming_both_steps():
    first = _door((1000, 0), tag="M0921", index=1)
    second = _door((3000, 0), tag="M0921", index=5)
    report = critique.run([first, second], PREFIXES, focus="tag_duplicates")
    assert len(report["issues"]) == 1
    assert report["issues"][0]["detail"]["steps"] == [1, 5]
    assert "1" in report["issues"][0]["message"] and "5" in report["issues"][0]["message"]


# ---------------------------------------------------------------------------
# 5. text that is pretending to be a dimension
# ---------------------------------------------------------------------------


def _text(value, index=1, state="applied"):
    return _step(
        index,
        "add_text",
        {"pos": [0, 0], "text": value, "height": 300.0, "rotation": 0.0, "align": "left"},
        state=state,
    )


def test_text_that_masquerades_as_a_dimension_warns():
    report = critique.run([_text("\u00d878")], PREFIXES, focus="dimension_like_text")
    issue = report["issues"][0]
    assert issue["severity"] == "warning"
    assert "add_dimension" in issue["message"]


def test_a_standard_part_designation_is_not_a_dimension():
    assert (
        critique.run([_text("ISO 4014 - M12x60")], PREFIXES, focus="dimension_like_text")["issues"]
        == []
    )


# ---------------------------------------------------------------------------
# 6. the plan against the sheet's own viewport
# ---------------------------------------------------------------------------

BIG_PLAN = _step(
    1,
    "draw_wall",
    {"points": [[0, 0], [40000, 0]], "thickness": 200.0, "closed": False, "hatch": False},
)


def _sheet(scale, index=3, state="applied"):
    return _step(
        index,
        "drawing_sheet",
        {"paper": "A3", "scale": scale, "orientation": "landscape"},
        state=state,
    )


def test_a_plan_larger_than_the_viewport_warns_and_names_a_scale_that_holds_it():
    report = critique.run([BIG_PLAN, _sheet("1:100")], PREFIXES, focus="sheet_viewport_fit")
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert issue["severity"] == "warning"
    assert "1:150" in issue["message"]
    assert issue["detail"]["largest_scale_that_fits"] == "1:150"


def test_the_same_plan_fits_the_same_sheet_at_a_smaller_scale():
    assert (
        critique.run([BIG_PLAN, _sheet("1:200")], PREFIXES, focus="sheet_viewport_fit")["issues"]
        == []
    )


def test_a_plan_with_no_sheet_is_not_measured_against_one():
    assert critique.run([BIG_PLAN], PREFIXES, focus="sheet_viewport_fit")["issues"] == []


# ---------------------------------------------------------------------------
# focus, unreadable rows, and rows that are not in the drawing
# ---------------------------------------------------------------------------


def test_focus_runs_only_the_named_check():
    rows = [RUN, _door((50000, 50000), tag="M921")]
    only = critique.run(rows, PREFIXES, focus="tag_format")
    assert {issue["code"] for issue in only["issues"]} == {"tag_format"}
    assert only["counts"] == {"error": 0, "warning": 1, "info": 0}
    assert only["focus"] == "tag_format"
    # Without a focus the same rows report both the door and the tag.
    both = critique.run(rows, PREFIXES)
    assert {issue["code"] for issue in both["issues"]} == {"door_without_opening", "tag_format"}


def test_an_unknown_focus_is_refused_by_name():
    with pytest.raises(ValueError) as refused:
        critique.run([], PREFIXES, focus="bogus")
    assert "door_without_opening" in str(refused.value)


def test_a_row_whose_params_cannot_be_read_is_counted_not_raised():
    report = critique.run([RUN, _step(2, "draw_door", None)], PREFIXES)
    assert report["unreadable"] == 1
    assert report["rows_checked"] == 1
    assert _codes(report) == []


def test_a_row_that_is_not_a_mapping_is_counted_too():
    report = critique.run([None, RUN], PREFIXES)
    assert report["unreadable"] == 1
    assert report["rows_checked"] == 1


def test_a_step_the_drawing_does_not_contain_is_invisible_to_every_check():
    rows = [
        _door((50000, 50000), tag="M921", state="undone"),
        _run(
            [(0, 0), (1000, 0)],
            [{"start": 900, "width": 900, "kind": "door", "tag": "M921"}],
            state="undone",
        ),
        _text("\u00d878", state="undone"),
        _sheet("1:100", state="undone"),
        _step(5, "draw_wall", {"points": [[0, 0], [40000, 0]]}, state="undone"),
    ]
    report = critique.run(rows, PREFIXES)
    assert report["issues"] == []
    assert report["rows_checked"] == 0


def test_a_planned_or_failed_step_is_not_in_the_drawing_either():
    # The plugin's own journal calls them "planned" (a promise, not yet run) and "failed"
    # (attempted, aborted). Neither step's geometry is in the drawing, so no check may read it.
    rows = [
        _door((50000, 50000), tag="M921", state="planned"),
        _door((50000, 50000), tag="M921", state="failed"),
    ]
    report = critique.run(rows, PREFIXES)
    assert report["issues"] == []
    assert report["rows_checked"] == 0


# ---------------------------------------------------------------------------
# realism: a real dxf drawing, and the ops the checks name
# ---------------------------------------------------------------------------


def test_a_real_dxf_drawing_critiques_clean_from_its_own_step_rows():
    be = make_backend("dxf")
    run_params = {
        "points": [[0, 0], [6000, 0]],
        "openings": [{"start": 1000, "width": 900, "kind": "door", "tag": "M0921"}],
        "thickness": 200.0,
        "hatch": False,
        "closed": False,
    }
    with be.step("draw_wall_with_openings", params=run_params):
        ops.draw_wall_with_openings(be, run_params["points"], run_params["openings"], 200.0)
    door_params = {
        "hinge": [1000, 0],
        "width": 900.0,
        "wall_thickness": 200.0,
        "rotation": 0.0,
        "open_angle": 90.0,
        "swing": "left",
        "tag": None,  # the opening carries the tag; the symbol need not repeat it
    }
    with be.step("draw_door", params=door_params):
        ops.draw_door(be, (1000, 0), 900.0, 200.0)

    rows = be.step_rows()
    assert [row["state"] for row in rows] == ["applied", "applied"]
    report = critique.run(rows, PREFIXES)
    assert report["issues"] == []
    assert report["rows_checked"] == 2


def test_every_op_the_checks_read_is_an_op_a_real_tool_records():
    # The failure `summary.py`'s HANDLERS once had: a check keyed on an op name no tool
    # produces reports nothing, for every drawing, forever. Read the producers out of the
    # server rather than trusting the list beside the checks.
    server = (Path(__file__).resolve().parents[1] / "src" / "acadmcp" / "server.py").read_text(
        encoding="utf-8"
    )
    recorded = set(re.findall(r'_auto_step\("([a-z_]+)"', server))
    assert recorded >= critique.READ_OPS
