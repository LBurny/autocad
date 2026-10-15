"""Repetition mining over the journal's step rows.

No backend, no CAD and no files: the rows are hand-built in the shape both journal owners
report them — ``index``, ``op``, ``label``, ``params``, ``state`` — because that is what
``suggestions`` reads.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import suggest  # noqa: E402


def _step(index, op, params, state="applied", label=""):
    return {"index": index, "op": op, "label": label, "params": params, "state": state}


def _door(index, width=900, hinge=(0, 0), state="applied"):
    return _step(
        index,
        "draw_door",
        {"hinge": list(hinge), "width": width, "swing": "left"},
        state=state,
    )


# --- what groups with what -------------------------------------------------


def test_a_repeated_door_is_suggested():
    out = suggest.suggestions([_door(1), _door(2), _door(3)])
    assert len(out["suggestions"]) == 1
    group = out["suggestions"][0]
    assert group["op"] == "draw_door"
    assert group["count"] == 3
    assert group["steps"] == [1, 2, 3]


def test_two_doors_of_a_different_width_are_two_things_not_three_and_one():
    out = suggest.suggestions([_door(1, 900), _door(2, 900), _door(3, 1200)])
    assert out["suggestions"] == []


def test_a_width_differing_by_a_tenth_of_a_millimetre_is_a_different_thing():
    rows = [_door(1, 900), _door(2, 900), _door(3, 900.2)]
    assert suggest.suggestions(rows)["suggestions"] == []


def test_floats_round_to_a_tenth_of_a_millimetre_before_grouping():
    out = suggest.suggestions([_door(1, 900.0), _door(2, 900.02), _door(3, 899.97)])
    assert out["suggestions"][0]["count"] == 3
    assert out["suggestions"][0]["params"]["width"] == 900.0


def test_nested_parameters_are_canonicalised_by_key_order():
    a = _step(
        1,
        "draw_wall_with_openings",
        {
            "points": [[0, 0], [6000, 0]],
            "openings": [{"start": 1000, "kind": "door", "width": 900}],
        },
    )
    b = _step(
        2,
        "draw_wall_with_openings",
        {
            "openings": [{"width": 900, "kind": "door", "start": 1000}],
            "points": [[0, 0], [6000, 0]],
        },
    )
    c = _step(
        3,
        "draw_wall_with_openings",
        {
            "points": [[0, 0], [6000, 0]],
            "openings": [{"start": 1000, "width": 900, "kind": "door"}],
        },
    )
    out = suggest.suggestions([a, b, c])
    assert out["suggestions"][0]["count"] == 3


def test_a_reordered_list_is_a_different_thing():
    """A ring drawn the other way is not the same ring, so list order is part of the key."""
    forward = [0, 0]
    backward = [6000, 0]
    rows = [
        _step(1, "draw_wall", {"points": [forward, backward]}),
        _step(2, "draw_wall", {"points": [forward, backward]}),
        _step(3, "draw_wall", {"points": [forward, backward]}),
        _step(4, "draw_wall", {"points": [backward, forward]}),
        _step(5, "draw_wall", {"points": [backward, forward]}),
    ]
    out = suggest.suggestions(rows)
    assert len(out["suggestions"]) == 1
    assert out["suggestions"][0]["steps"] == [1, 2, 3]


def test_different_ops_never_group():
    rows = [_door(1), _door(2), _door(3)]
    rows += [_step(i, "draw_window", {"start": [0, 0], "width": 900}) for i in (4, 5, 6)]
    out = suggest.suggestions(rows)
    assert sorted(g["op"] for g in out["suggestions"]) == ["draw_door", "draw_window"]


def test_the_group_params_are_the_canonical_shared_parameters():
    group = suggest.suggestions([_door(1), _door(2), _door(3)])["suggestions"][0]
    assert group["params"] == {"hinge": [0.0, 0.0], "width": 900.0, "swing": "left"}


# --- bookkeeping keys are left out ----------------------------------------


def test_the_label_is_decoration_not_geometry():
    rows = [
        _step(1, "draw_room", {"boundary": [[0, 0], [1000, 0], [1000, 1000]]}, label="A"),
        _step(2, "draw_room", {"boundary": [[0, 0], [1000, 0], [1000, 1000]]}, label="B"),
        _step(3, "draw_room", {"boundary": [[0, 0], [1000, 0], [1000, 1000]]}, label="C"),
    ]
    out = suggest.suggestions(rows)
    assert out["suggestions"][0]["count"] == 3
    assert "label" not in out["suggestions"][0]["params"]


def test_the_capture_step_list_is_not_geometry():
    """Two captures over different spans of identical geometry are the same element."""
    rows = [
        _step(1, "element_capture", {"steps": [1], "key": "door_900"}),
        _step(2, "element_capture", {"steps": [2], "key": "door_900"}),
        _step(3, "element_capture", {"steps": [3], "key": "door_900"}),
    ]
    out = suggest.suggestions(rows)
    assert out["suggestions"][0]["count"] == 3
    assert "steps" not in out["suggestions"][0]["params"]


# --- state: what the drawing actually contains -----------------------------


def test_an_undone_step_is_not_repetition():
    rows = [_door(1), _door(2), _door(3), _door(4, state="undone")]
    out = suggest.suggestions(rows)
    assert out["suggestions"][0]["count"] == 3
    assert out["suggestions"][0]["steps"] == [1, 2, 3]


def test_a_planned_step_is_not_repetition():
    rows = [_door(1, state="planned"), _door(2, state="planned"), _door(3, state="planned")]
    assert suggest.suggestions(rows)["suggestions"] == []


def test_the_plugins_done_state_counts_as_applied():
    """The Python journal writes "applied"; the plugin's in-DWG journal writes "done" for
    the same fact, so the plugin backend must not find nothing."""
    rows = [_door(1, state="done"), _door(2, state="done"), _door(3, state="done")]
    assert suggest.suggestions(rows)["suggestions"][0]["count"] == 3


def test_a_row_with_no_state_is_skipped_and_counted():
    row = {"index": 9, "op": "draw_door", "params": {"width": 900}}
    out = suggest.suggestions([_door(1), _door(2), _door(3), row])
    assert out["skipped"] == 1
    assert out["suggestions"][0]["steps"] == [1, 2, 3]


def test_a_row_that_is_not_a_step_row_is_skipped_and_counted():
    out = suggest.suggestions([_door(1), _door(2), _door(3), "not a row", None])
    assert out["skipped"] == 2
    assert out["suggestions"][0]["count"] == 3


def test_an_applied_row_with_no_op_is_skipped_and_counted():
    out = suggest.suggestions([_door(1), _door(2), _door(3), {"index": 4, "state": "applied"}])
    assert out["skipped"] == 1


# --- min_count -------------------------------------------------------------


def test_min_count_below_two_is_refused():
    for bad in (1, 0, -5):
        with pytest.raises(ValueError, match="min_count"):
            suggest.suggestions([], min_count=bad)


def test_min_count_two_finds_a_pair():
    out = suggest.suggestions([_door(1), _door(2)], min_count=2)
    assert out["suggestions"][0]["count"] == 2


def test_a_group_below_min_count_is_left_out():
    out = suggest.suggestions([_door(1), _door(2)])
    assert out["suggestions"] == []


# --- the answer's shape, order and hint ------------------------------------


def test_the_answer_is_a_dict_of_suggestions_and_skipped():
    out = suggest.suggestions([_door(1), _door(2), _door(3)])
    assert set(out) == {"suggestions", "skipped"}


def test_no_rows_is_an_empty_answer_not_an_error():
    assert suggest.suggestions([]) == {"suggestions": [], "skipped": 0}


def test_groups_are_ordered_by_count_descending():
    rows = [_door(i) for i in range(1, 5)]
    rows += [_step(i, "draw_window", {"start": [0, 0], "width": 1200}) for i in (5, 6, 7)]
    out = suggest.suggestions(rows)
    assert [g["count"] for g in out["suggestions"]] == [4, 3]


def test_the_hint_lists_the_groups_own_steps_and_names_the_calls():
    group = suggest.suggestions([_door(1), _door(2), _door(3)])["suggestions"][0]
    assert str(group["steps"]) in group["hint"]
    assert "element_capture" in group["hint"] and "element_publish" in group["hint"]


def test_the_suggested_key_names_the_element_and_its_size():
    group = suggest.suggestions([_door(1), _door(2), _door(3)])["suggestions"][0]
    assert "key='door_900'" in group["hint"]
    assert "the same door" in group["hint"]


def test_a_placed_element_key_comes_from_its_type_and_variant():
    rows = [
        _step(i, "place_element", {"type": "door", "at": [0, 0], "variant": "900", "params": {}})
        for i in (1, 2, 3)
    ]
    group = suggest.suggestions(rows)["suggestions"][0]
    assert "key='door_900'" in group["hint"]
    # "door", not "element": the type says what was placed.
    assert "the same door" in group["hint"]


def test_a_column_size_is_the_key_when_no_width_or_variant_is_recorded():
    rows = [_step(i, "draw_column", {"center": [0, 0], "size": 600}) for i in (1, 2, 3)]
    assert "key='column_600'" in suggest.suggestions(rows)["suggestions"][0]["hint"]


# --- the constraint the design rests on ------------------------------------


def test_the_suggest_module_stays_cad_free():
    """`suggest.py` may import the standard library and nothing else — never ezdxf, never a
    backend, and it does not even reach `geom`. Checked statically, because by the time this
    runs the rest of the suite has already imported ezdxf."""
    source = Path(suggest.__file__).read_text(encoding="utf-8")
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
    assert absolute <= {"__future__", "json", "re", "collections", "typing"}, absolute
    assert relative == set(), relative
