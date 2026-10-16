"""The plugin's step row against the readers that consume it.

Every journal-derived read tool on the plugin backend reads the *wire* row rather than a
local journal, because the plugin owns the journal and the drawing's steps never enter this
process. So a key the plugin does not send is not a missing detail — it is a tool that
answers confidently and wrongly. That is not hypothetical: `RpcServer.Row` used to report
`primitives` as a *count* and omit `params` entirely, and the result was `drawing_critique`
finding nothing to complain about in a plan with a floating door in it, `drawing_finalize`
passing on the back of it, `drawing_summary` reporting an empty plan, and
`drawing_check_standard` collecting no text at all so its text-height rule could never fire.

The C# side is where the row is built, and it cannot be run without AutoCAD. So the shape is
pinned from here, in two halves: a row *shaped the way the plugin sends it* must drive each
reader, and the state vocabularies the two sides compare against each other must be one set.
`tests/live_rpc_check.py` carries the other half — it asserts the same keys against a real
plugin over the real socket.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import critique, server, summary, verify  # noqa: E402
from acadmcp.backends import recording  # noqa: E402
from acadmcp.journal import Journal  # noqa: E402


def _wire_row(index, op, params, primitives, state="done", handles=None):
    """A step row exactly as ``RpcServer.Row`` builds it, as far as the readers go.

    ``state`` is lower-cased and ``primitives`` is the objects, not a count: that is the
    plugin's own vocabulary, and the reason the two are pinned together here.
    """
    return {
        "index": index,
        "op": op,
        "label": f"{op} 1",
        "state": state,
        "params": params,
        "primitives": primitives,
        "handles": handles if handles is not None else [],
        "entities": len(handles) if handles is not None else 0,
    }


WALL = {
    "kind": "line",
    "points": [0.0, 0.0, 6000.0, 0.0],
    "layer": "A-WALL",
}


def test_summary_reads_a_plugin_row():
    # The bug this pins: with no `params` key the summary reported an empty plan and one
    # "recorded no readable parameters" warning per step, for a drawing that was fine.
    rows = [
        _wire_row(
            1,
            "draw_room",
            {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "name": "lab"},
            [
                {
                    "kind": "polyline",
                    "points": [0.0, 0.0, 4000.0, 0.0, 4000.0, 3000.0, 0.0, 3000.0],
                    "closed": True,
                    "layer": "A-WALL",
                }
            ],
        ),
    ]

    out = summary.summarise(rows)

    assert out["steps"]["applied"] == 1 and out["steps"]["undone"] == 0
    assert not [w for w in out["warnings"] if "readable parameters" in w]
    assert [room["name"] for room in out["rooms"]] == ["lab"]


def test_a_planned_plugin_step_is_not_counted_as_drawn():
    # "planned" is the plugin's word for what this package's journal calls "undone". Reading
    # only "undone" counted a rolled-back step as applied.
    rows = [
        _wire_row(1, "draw_wall", {"points": [[0, 0], [6000, 0]]}, [WALL], state="done"),
        _wire_row(2, "draw_wall", {"points": [[0, 0], [6000, 0]]}, [WALL], state="planned"),
        _wire_row(3, "draw_wall", {"points": [[0, 0], [6000, 0]]}, [WALL], state="failed"),
    ]

    out = summary.summarise(rows)

    assert out["steps"]["applied"] == 1
    assert out["steps"]["undone"] == 2


def test_verify_counts_a_planned_plugin_step_as_nothing_drawn():
    rows = [
        _wire_row(1, "draw_line", {}, [WALL], handles=["1A"]),
        _wire_row(2, "draw_line", {}, [WALL], state="planned", handles=[]),
    ]

    report = verify.reconcile(rows, [{"type": "LINE"}])

    assert report["steps"] == {"applied": 1, "undone": 1}
    assert report["verdict"] == "matches"


def test_critique_reads_a_plugin_row_and_finds_the_floating_door():
    # The whole point of the fix: a door whose hinge sits on no wall must be an error on the
    # plugin too, because `drawing_finalize` gates on this check. With `params` missing every
    # step was counted unreadable and `issues` came back empty.
    rows = [
        _wire_row(
            1,
            "draw_wall",
            {"points": [[0, 0], [6000, 0]]},
            [WALL],
            handles=["1A"],
        ),
        _wire_row(
            2,
            "draw_door",
            {"hinge": [5000, 900], "rotation": 0.0, "width": 900},
            [
                {
                    "kind": "line",
                    "points": [5000.0, 900.0, 5000.0, 1800.0],
                    "layer": "A-DOOR",
                }
            ],
            handles=["1B"],
        ),
    ]

    report = critique.run(rows)

    codes = {issue["code"] for issue in report["issues"]}
    assert "door_without_opening" in codes
    assert report["unreadable"] == 0


def test_standard_snapshot_reads_text_off_a_plugin_row():
    # `_standard_snapshot` gathers the texts from the step rows, because `be.entities()`
    # carries no height. With `primitives` a count it gathered nothing, so the text-height
    # rule could never fire on a live drawing.
    rows = [
        _wire_row(
            1,
            "add_text",
            {"text": "lab", "height": 1.0},
            [
                {
                    "kind": "text",
                    "points": [0.0, 0.0],
                    "text": "room",
                    "height": 1.0,
                    "layer": "A-ROOM-NAME",
                }
            ],
        ),
    ]
    be = _StubBackend(rows)

    snapshot = server._standard_snapshot(be)

    assert snapshot["texts"] == [
        {"height": 1.0, "layer": "A-ROOM-NAME", "content": "room", "paper": False}
    ]


def test_capture_gathers_primitives_from_a_plugin_row():
    # The plugin path never fills the local journal, so the row is the only place the
    # geometry can come from.
    backend = _StubBackend([_wire_row(1, "place_element", {}, [WALL], handles=["1A"])])

    gathered = backend.step_primitives([1])

    assert gathered == [WALL]


class _StubBackend:
    """The few things the readers above ask a backend for, and nothing more."""

    def __init__(self, rows):
        self._rows = rows

    def step_rows(self):
        return self._rows

    def info(self):
        return {"document": "t.dwg", "layers": [], "entities": len(self._rows)}

    def entities(self, layer=None, limit=200):
        return []

    @property
    def journal(self):
        # An empty journal, not None: this is what the plugin backend really carries — the
        # drawing's steps are in the DWG and never in this object — and the readers fall back
        # to it precisely because it can hold nothing useful.
        return Journal()

    # The real methods, borrowed: the point is what they do with a plugin-shaped row.
    step_primitives = recording.RecordingBackend.step_primitives


def test_every_state_vocabulary_agrees_on_which_steps_are_on_the_drawing():
    # Four modules used to hold four copies of this decision, and only one of them had been
    # taught the plugin's spelling — the same shape of bug that made a plugin undo report ok
    # while erasing nothing. One table now, and this pins the three that cannot import it.
    from acadmcp import journal, suggest

    assert verify.INACTIVE_STATES == summary.UNDONE_STATES
    assert verify.INACTIVE_STATES == critique.INACTIVE_STATES
    assert verify.INACTIVE_STATES == suggest.INACTIVE_STATES
    assert journal.IN_DRAWING_STATES.isdisjoint(verify.INACTIVE_STATES)
    assert journal.IN_DRAWING_STATES == recording.IN_DRAWING_STATES
    # The plugin's own spelling is in both, or one of the two readers is wrong about a live
    # drawing: `done` is on the drawing, `planned`/`failed` are not.
    assert "done" in journal.IN_DRAWING_STATES
    assert {"planned", "failed"} <= verify.INACTIVE_STATES


@pytest.mark.parametrize("module", [verify, summary])
def test_neither_vocabulary_invents_a_state(module):
    # Guard against a table growing a word the C# StepState enum does not have, which would be
    # a silent no-op exactly like the ones this file exists to prevent.
    known = {"planned", "done", "failed", "applied", "undone", "accepted", "rejected"}
    table = getattr(module, "INACTIVE_STATES", None) or module.UNDONE_STATES
    assert set(table) <= known
