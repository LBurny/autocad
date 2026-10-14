"""The step journal, from the MCP side.

The journal itself lives inside the drawing (the plugin keeps it in a dictionary of
Xrecords, so it survives save and reopen and travels with the file). This module is
just the remote control for it: one thin method per action, plus a generic
:meth:`StepSession.control` that the ``step_control`` MCP tool dispatches through.

Nothing here computes geometry. A step planned from this side has to arrive with
its primitives resolved, because the plugin replays a step by re-creating exactly
those primitives.
"""

from __future__ import annotations

from typing import Any

from .acad_plugin_client import PluginClient

#: Actions ``step_control`` accepts, mapped to the plugin method that runs them.
#: Kept as data so the MCP tool and this class cannot drift apart.
CONTROL_ACTIONS = {
    "run_next": "run_next",
    "run_all": "run_all",
    "run_to": "run_to",
    "rollback_to": "rollback_to",
    "reject": "reject",
    "accept": "accept",
    "unaccept": "accept",
    "note": "note",
    "update": "update",
    "snapshot": "snapshot",
    "clear_plan": "clear_plan",
    "reset": "reset",
    "reload": "reload",
}

#: The arguments each action understands, so the tool can say what is missing
#: instead of sending a request that fails in the plugin.
CONTROL_ARGUMENTS = {
    "run_next": (),
    "run_all": (),
    "run_to": ("index",),
    "rollback_to": ("index",),
    "reject": ("index",),
    "accept": ("index",),
    "unaccept": ("index",),
    "note": ("index", "note"),
    "update": ("index", "patch"),
    "snapshot": (),
    "clear_plan": (),
    "reset": (),
    "reload": (),
}


class StepSession:
    """Step-journal control for the drawing that is open in AutoCAD."""

    def __init__(self, client: PluginClient) -> None:
        self.client = client

    # --- reading -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Counts and identity: what the journal holds right now."""
        return self.client.call("status")

    def steps(self, offset: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """The step rows, in order."""
        return self.client.call("get_steps", offset=offset, limit=limit)["steps"]

    def step(self, index: int) -> dict[str, Any] | None:
        """One step row, or None when there is no such step."""
        for row in self.steps():
            if row["index"] == index:
                return row
        return None

    # --- planning ----------------------------------------------------------

    def plan(self, steps: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        """Replace the un-run tail of the plan.

        Each step is ``{"op": str, "label": str, "params": {...}, "primitives": [...]}``.
        Steps that have already run are never disturbed: a plan can only ever
        extend the tail.
        """
        return self.client.call("plan", steps=steps, description=description)

    # --- acting ------------------------------------------------------------

    def control(self, action: str, **arguments: Any) -> dict[str, Any]:
        """Run one journal action by name.

        Raises :class:`ValueError` for an unknown action or a missing argument,
        rather than letting the plugin answer with something vaguer.
        """
        if action not in CONTROL_ACTIONS:
            known = ", ".join(sorted(CONTROL_ACTIONS))
            raise ValueError(f"unknown step action {action!r}; known actions: {known}")

        required = CONTROL_ARGUMENTS[action]
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"{action} needs {', '.join(missing)}")

        method = CONTROL_ACTIONS[action]
        payload = {name: arguments[name] for name in required}
        if action == "unaccept":
            payload["accepted"] = False
        return self.client.call(method, **payload)

    def run_all(self) -> dict[str, Any]:
        return self.control("run_all")

    def run_next(self) -> dict[str, Any]:
        return self.control("run_next")

    def run_to(self, index: int) -> dict[str, Any]:
        return self.control("run_to", index=index)

    def rollback_to(self, index: int, force: bool = False) -> dict[str, Any]:
        return self.client.call("rollback_to", index=index, force=force)

    def accept(self, index: int, accepted: bool = True) -> dict[str, Any]:
        return self.control("accept" if accepted else "unaccept", index=index)

    def reject(self, index: int) -> dict[str, Any]:
        return self.control("reject", index=index)

    def snapshot(self, note: str = "") -> dict[str, Any]:
        return self.client.call("snapshot", note=note)

    def note(self, index: int, note: str) -> dict[str, Any]:
        return self.control("note", index=index, note=note)

    def update(self, index: int, patch: dict[str, Any]) -> dict[str, Any]:
        return self.control("update", index=index, patch=patch)

    def clear_plan(self) -> dict[str, Any]:
        return self.control("clear_plan")

    def reset(self) -> dict[str, Any]:
        return self.control("reset")

    def reload(self) -> dict[str, Any]:
        return self.control("reload")


def describe_steps(steps: list[dict[str, Any]]) -> str:
    """One line per step, for a tool result a person has to read."""
    if not steps:
        return "no steps"
    return "\n".join(
        f"{row['index']:>3}  {row['state']:<8} {row.get('row') or row.get('label') or row['op']}"
        for row in steps
    )
