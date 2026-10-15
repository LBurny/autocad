"""The plugin backend's journal delegation, against a client that keeps the steps.

The DWG journal is the plugin's, not this process's, so undo/redo/history here are
not a local bookkeeping exercise: they must translate into the plugin's own
rollback and run_to calls. What is easy to get wrong — and what this file pins
down — is the numbering. The plugin's steps are 1-based and index 0 means "before
the first step", so undoing the very first step targets 0, never -1.

No AutoCAD is needed: the fake answers the same wire methods the plugin does.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp.backends.plugin import PluginBackend  # noqa: E402


class FakeClient:
    """Keeps the steps the way the plugin's journal does: 1-based, with state."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.steps: list[dict[str, Any]] = []

    def connect(self) -> FakeClient:
        return self

    def ping(self) -> dict[str, Any]:
        return {"pong": True, "queue": "attached"}

    def call(self, method: str, **payload: Any) -> Any:
        self.calls.append((method, payload))
        if method == "apply":
            step = dict(payload["step"], index=len(self.steps) + 1, state="applied")
            self.steps.append(step)
            return {"result": {"ok": True, "message": "", "step": step}}
        if method == "get_steps":
            return {"steps": list(self.steps)}
        if method == "rollback_to":
            for s in self.steps:
                if s["index"] > payload["index"] and s["state"] == "applied":
                    s["state"] = "planned"
            return {"result": {"ok": True, "message": "", "touched": []}}
        if method in ("run_next", "run_to", "ensure_layer", "set_units_mm", "status", "info", "entities"):
            return {"result": {"ok": True}, "entities": [], "ok": True}
        raise AssertionError(f"unexpected call {method}")

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def _backend() -> tuple[PluginBackend, FakeClient]:
    client = FakeClient()
    return PluginBackend(client=client), client


def _draw(be: PluginBackend, op: str) -> None:
    """One committed step, the way an op draws inside backend.step()."""
    with be.step(op):
        be._record("line", [(0, 0), (1000, 0)])


def test_undo_rolls_back_to_before_the_last_applied_step():
    be, client = _backend()
    _draw(be, "draw_line")
    _draw(be, "draw_circle")

    report = be.undo(1)

    assert ("rollback_to", {"index": 1}) in client.calls
    assert client.steps[1]["state"] == "planned"
    assert client.steps[0]["state"] == "applied"
    assert report["undone"] == [{"index": 2, "op": "draw_circle", "label": ""}]


def test_undo_of_the_first_step_targets_zero():
    be, client = _backend()
    _draw(be, "draw_line")

    be.undo(1)

    # Index 0 is "before the first step" in Journal.cs, so this is the legal wipe
    # rather than a negative index that the plugin would refuse.
    assert ("rollback_to", {"index": 0}) in client.calls


def test_redo_runs_the_plan_forward():
    be, client = _backend()
    _draw(be, "draw_line")
    be.undo(1)

    report = be.redo(1)

    assert ("run_to", {"index": 1}) in client.calls
    assert client.steps[0]["state"] == "planned"  # the fake does not run the plan
    assert report["redone"] == [{"index": 1, "op": "draw_line", "label": ""}]


def test_undo_with_nothing_applied_is_a_no_op():
    be, client = _backend()

    report = be.undo(1)

    assert "rollback_to" not in client.methods()
    assert report["undone"] == []


def test_history_lists_the_plugin_steps():
    be, client = _backend()
    _draw(be, "draw_line")
    _draw(be, "draw_room")

    rendered = be.history()

    assert "draw_room" in rendered and "draw_line" in rendered
    assert len(rendered.splitlines()) == 2
