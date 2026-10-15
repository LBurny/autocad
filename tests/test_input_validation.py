"""Caller input mistakes must come back as readable errors, not masked crashes.

Every one of these was found by drawing real plans through the sandbox driver:
the SDK masks anything that is not a ToolError behind "Error executing tool
<name>", so a validation that lets an IndexError/KeyError/ZeroDivisionError
escape tells the model nothing. The tools here are called through the tool
manager's wrapped `fn`, which is what converts ValueError into a ToolError —
exactly the wire path a client takes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_dxf():
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def _tool(name: str):
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}[name].fn


def test_opening_without_start_names_the_missing_key():
    with pytest.raises(BackendError, match="'start' and 'width'"):
        _tool("draw_wall_with_openings")(
            points=[[0, 0], [3000, 0]], openings=[{"width": 900, "kind": "door"}]
        )


def test_opening_with_a_bad_kind_is_rejected():
    with pytest.raises(BackendError, match="kind"):
        _tool("draw_wall_with_openings")(
            points=[[0, 0], [3000, 0]],
            openings=[{"start": 100, "width": 900, "kind": "hatch"}],
        )


def test_door_swing_is_an_enum():
    with pytest.raises(BackendError, match="swing"):
        _tool("draw_door")(hinge=[0, 0], swing="sideways")


def test_stair_axis_is_an_enum():
    with pytest.raises(BackendError, match="axis"):
        _tool("draw_stair")(corner1=[0, 0], corner2=[2600, 5000], axis="z")


def test_text_align_is_an_enum():
    with pytest.raises(BackendError, match="align"):
        _tool("add_text")(pos=[0, 0], text="x", align="middle")


def test_circle_needs_a_positive_radius():
    with pytest.raises(BackendError, match="radius"):
        _tool("draw_circle")(center=[0, 0], radius=-5)


def test_room_boundary_needs_three_corners():
    with pytest.raises(BackendError, match="three"):
        _tool("draw_room")(boundary=[[0, 0], [1000, 0]], name="x", walls=False)


def test_self_intersecting_room_boundary_is_rejected():
    bowtie = [[0, 0], [3000, 3000], [3000, 0], [0, 3000]]
    with pytest.raises(BackendError, match="crosses itself"):
        _tool("draw_room")(boundary=bowtie, name="x", walls=False)
    with pytest.raises(BackendError, match="crosses itself"):
        _tool("draw_room")(boundary=bowtie, name="x", walls=True)


def test_dimension_needs_two_distinct_points():
    with pytest.raises(BackendError, match="distinct"):
        _tool("add_dimension")(points=[[3000, 0], [3000, 0]])


def test_axis_grid_extent_needs_four_numbers():
    with pytest.raises(BackendError, match="extent"):
        _tool("draw_axis_grid")(x_positions=[0], y_positions=[0], extent=[0, 0])


def test_axis_grid_labels_must_match_positions():
    with pytest.raises(BackendError, match="labels"):
        _tool("draw_axis_grid")(
            x_positions=[0, 3600],
            y_positions=[0],
            extent=[0, 0, 7200, 7200],
            x_labels=["1"],
        )


def test_listing_limit_must_be_at_least_one():
    with pytest.raises(BackendError, match="limit"):
        _tool("listing_entities")(limit=-1)


def test_hatch_pattern_must_exist():
    with pytest.raises(BackendError, match="unknown hatch pattern"):
        _tool("add_hatch")(points=[[0, 0], [1000, 0], [1000, 1000], [0, 1000]], pattern="NOPE-XX")
    # Case does not matter: acad.pat names are upper-case by convention only.
    out = _tool("add_hatch")(points=[[0, 0], [2000, 0], [2000, 1000], [0, 1000]], pattern="ansi31")
    assert out["ok"] is True


def test_closed_wall_reports_its_full_perimeter():
    out = _tool("draw_wall")(points=[[0, 0], [5000, 0], [5000, 5000], [0, 5000]], closed=True)
    assert out["segments"] == 4
    assert out["length"] == 20000.0


def test_starting_a_new_drawing_over_content_says_so():
    _tool("draw_line")(p1=[0, 0], p2=[1000, 0])
    out = _tool("drawing_new")(backend="dxf")
    assert "note" in out and "no longer the target" in out["note"]
    # And with nothing to lose there is no note.
    out = _tool("drawing_new")(backend="dxf")
    assert "note" not in out


def test_a_long_room_name_is_shrunk_to_its_room():
    """A fixed text height spilled 无障碍卫生间 across a 2 m toilet and into
    the rooms either side. The label now follows the same shrink-to-fit the
    equipment and shaft captions already used."""
    from acadmcp import layers

    be = server._state.backend
    server.draw_room(
        boundary=[[0, 0], [2000, 0], [2000, 2000], [0, 2000]],
        name="无障碍卫生间",
        walls=False,
    )
    heights = [
        e.dxf.height for e in be.msp if e.dxftype() == "TEXT" and e.dxf.layer == "A-ROOM-NAME"
    ]
    assert heights, "the room name was not drawn"
    # 6 CJK characters must fit across a 2000 mm ring's inner width.
    assert heights[0] < layers.H_ROOM_NAME
    assert layers.text_width("无障碍卫生间", heights[0]) < 2000


def test_a_comfortable_room_keeps_the_standard_label_height():
    from acadmcp import layers

    be = server._state.backend
    server.draw_room(
        boundary=[[0, 0], [6000, 0], [6000, 4000], [0, 4000]], name="主卧", walls=False
    )
    heights = [
        e.dxf.height for e in be.msp if e.dxftype() == "TEXT" and e.dxf.layer == "A-ROOM-NAME"
    ]
    assert heights[0] == layers.H_ROOM_NAME


def test_a_room_given_in_metres_is_refused_with_the_reason():
    """A client that passes metres used to draw a 4 mm room and get `ok: true`, and an
    AI that cannot see the drawing had no way to tell. The refusal has to name the value
    and the likely unit, or it cannot correct itself from the message alone."""
    with pytest.raises(BackendError) as caught:
        _tool("draw_room")(boundary=[[0, 0], [4, 0], [4, 3], [0, 3]], name="会议室", thickness=0.2)
    text = str(caught.value)
    assert "0.2" in text and "millimetre" in text


def test_a_metre_ring_with_a_plausible_thickness_is_still_refused():
    """The thickness is not the only place a unit slip shows: a ring in metres with an
    ordinary 200 mm wall still measures a few mm across inside, which is the number the
    area is measured to and the one a metres-valued boundary destroys."""
    with pytest.raises(BackendError) as caught:
        _tool("draw_room")(boundary=[[0, 0], [4, 0], [4, 3], [0, 3]], name="会议室")
    text = str(caught.value)
    assert "inner width" in text and "millimetre" in text


def test_a_millimetre_room_still_draws():
    out = _tool("draw_room")(boundary=[[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="会议室")
    assert out["ok"] and out["area_m2"] > 0


def test_a_column_smaller_than_a_stud_is_refused():
    with pytest.raises(BackendError) as caught:
        _tool("draw_column")(center=[0, 0], size=0.6)
    assert "millimetre" in str(caught.value)


def test_a_wall_too_thin_to_draw_is_refused():
    with pytest.raises(BackendError):
        _tool("draw_wall")(points=[[0, 0], [5000, 0]], thickness=0.2)
