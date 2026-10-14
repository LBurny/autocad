"""Drive a live AutoCAD through the plugin's RPC port, end to end.

Deliberately not collected by pytest: it needs AutoCAD running with the AcadMcp
plugin loaded, and the repository's rule is that no test may require AutoCAD to
pass. Run it directly, or let ``plugin/scripts/selftest.sh --rpc`` launch AutoCAD
and call it.

It exercises the real path — this repository's client, backend and journal control
talking to the real socket in the real plugin — because that is the only way to
know the wire format, the queue and the journal agree with each other. It creates
its own drawing and leaves it behind; the harness closes AutoCAD afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script, not through pytest
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp.acad_plugin_client import PluginClient, PluginError, PluginUnavailable  # noqa: E402
from acadmcp.backends.plugin import PluginBackend  # noqa: E402
from acadmcp.steps import StepSession  # noqa: E402


class Report:
    """Collects checks the same way the plugin's own self-test does."""

    def __init__(self) -> None:
        self.failures = 0
        self.lines: list[str] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        if not passed:
            self.failures += 1
        suffix = f" -- {detail}" if detail else ""
        self.lines.append(f"[{'pass' if passed else 'FAIL'}] {name}{suffix}")

    def finish(self) -> str:
        summary = (
            "\nACADMCP-RPC: all checks passed\n"
            if self.failures == 0
            else f"\nACADMCP-RPC: {self.failures} CHECK(S) FAILED\n"
        )
        return "\n".join(self.lines) + "\n" + summary


def line(x1: float, y1: float, x2: float, y2: float, layer: str = "A-WALL") -> dict[str, Any]:
    return {"kind": "line", "points": [x1, y1, x2, y2], "layer": layer}


def entity_count(backend: PluginBackend) -> int:
    return len(backend.entities(limit=1000))


def run(report: Report) -> None:
    client = PluginClient()
    try:
        client.connect()
    except PluginUnavailable as exc:
        report.check("a plugin is listening", False, str(exc))
        return
    report.check("a plugin is listening", True)

    try:
        pong = client.ping()
        report.check(
            "ping answers without AutoCAD",
            pong.get("pong") is True and pong.get("queue") == "attached",
            f"version={pong.get('version')} queue={pong.get('queue')}",
        )

        document = client.call("new_document")
        report.check(
            "a drawing can be created over RPC",
            bool(document.get("document")),
            str(document.get("document")),
        )

        # Built after the drawing exists, so the layer cache describes this file.
        backend = PluginBackend(client=client)
        session = StepSession(client)

        first = backend.ensure_layer("A-WALL", color=3)
        again = backend.ensure_layer("A-WALL", color=3)
        report.check("a layer is created once", first.get("created") is True, str(first))
        report.check(
            "asking for it twice is not an error", again.get("created") is False, str(again)
        )

        # With a linetype as well: that path loads it from acad.lin first, and it used
        # to raise a NullReferenceException because the linetype table was read
        # outside a transaction.
        centred = backend.ensure_layer("A-AXIS", color=1, linetype="CENTER")
        report.check("a layer can carry a linetype", centred.get("created") is True, str(centred))

        backend.set_units_mm()
        report.check(
            "units can be set to millimetres",
            backend.info()["insunits"] == 4,
            f"insunits={backend.info()['insunits']}",
        )

        # --- a step applies through the recorder ---------------------------
        with backend.step("draw_wall", label="wall") as wall:
            backend.line((0, 0), (6000, 0), layer="A-WALL")
        report.check("a recorded step commits as one step", wall.ok, wall.message)
        report.check(
            "that step owns one entity",
            len(wall.step.get("handles", [])) == 1,
            str(wall.step.get("handles")),
        )

        # --- a step with several kinds of primitive ------------------------
        with backend.step("draw_room", label="room") as room:
            backend.polyline(
                [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], closed=True, layer="A-WALL"
            )
            backend.text(
                (2000, 1500), "\u65e0\u83cc\u5ba4", 350, layer="A-ROOM-NAME", align="center"
            )
            backend.hatch(
                [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], "ANSI31", 40, layer="A-HATCH"
            )
        report.check("a mixed step commits", room.ok, room.message)
        report.check(
            "it owns three entities",
            len(room.step.get("handles", [])) == 3,
            str(room.step.get("handles")),
        )
        report.check(
            "the drawing holds both steps' entities",
            entity_count(backend) == 4,
            f"entities={entity_count(backend)}",
        )

        status = session.status()
        report.check(
            "the journal reports two applied steps",
            status["count"] == 2 and status["applied"] == 2,
            f"count={status['count']} applied={status['applied']}",
        )

        # --- planning ahead, then running the plan -------------------------
        planned = session.plan(
            [
                {
                    "op": "draw_stair",
                    "label": "stair",
                    "primitives": [line(7000, 2000, 8000, 2000, "A-STAIR")],
                }
            ],
            description="rpc check",
        )
        report.check(
            "a step can be planned without drawing it",
            planned["result"]["ok"],
            planned["result"]["message"],
        )
        report.check(
            "planning changes no geometry",
            entity_count(backend) == 4,
            f"entities={entity_count(backend)}",
        )
        report.check(
            "the planned step is waiting",
            session.status()["planned"] == 1,
            f"planned={session.status()['planned']}",
        )

        ran = session.run_all()
        report.check("run_all runs the planned step", ran["result"]["ok"], ran["result"]["message"])
        report.check(
            "running it drew the entity",
            entity_count(backend) == 5,
            f"entities={entity_count(backend)}",
        )

        # --- rollback ------------------------------------------------------
        reverted = session.rollback_to(2)
        report.check(
            "rollback_to reverts the step above the target",
            reverted["result"]["ok"],
            reverted["result"]["message"],
        )
        report.check(
            "rolling back erased exactly that entity",
            entity_count(backend) == 4,
            f"entities={entity_count(backend)}",
        )
        report.check(
            "the reverted step is planned again",
            session.status()["planned"] == 1,
            f"planned={session.status()['planned']}",
        )

        report.check(
            "the journal and the drawing agree after the rollback",
            session.status()["applied"] == 2,
            f"applied={session.status()['applied']}",
        )

        # --- replay --------------------------------------------------------
        report.check("run_all replays it", session.run_all()["result"]["ok"])
        report.check(
            "the replay drew it again",
            entity_count(backend) == 5,
            f"entities={entity_count(backend)}",
        )

        # --- the accepted soft-lock ---------------------------------------
        report.check("a step can be accepted", session.accept(1)["result"]["ok"])
        refused = session.rollback_to(0)
        report.check(
            "rolling back across an accepted step is refused",
            refused["result"]["ok"] is False,
            refused["result"]["message"],
        )
        forced = session.rollback_to(0, force=True)
        report.check(
            "force overrides the refusal", forced["result"]["ok"], forced["result"]["message"]
        )
        report.check(
            "the whole drawing is empty again",
            entity_count(backend) == 0,
            f"entities={entity_count(backend)}",
        )

        report.check("reset empties the journal", session.reset()["result"]["ok"])
        report.check(
            "the journal is empty",
            session.status()["count"] == 0,
            f"count={session.status()['count']}",
        )

        # --- the step rows the panel and the tools read --------------------
        rows = session.steps()
        report.check("steps can be listed", isinstance(rows, list), f"rows={len(rows)}")

        # --- an unknown method is refused, not guessed ---------------------
        try:
            client.call("no_such_method")
            report.check("an unknown method is refused", False, "it was accepted")
        except PluginError as exc:
            report.check("an unknown method is refused", "no_such_method" in str(exc), str(exc))
    except PluginError as exc:
        report.check("the RPC session completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        client.close()

    check_mcp_tools(report)


def check_mcp_tools(report: Report) -> None:
    """The same journey again, through the MCP tools an AI client actually calls.

    Kept separate from the checks above on purpose. Those prove the wire format and
    the client; this proves the tool layer — that step_begin and step_commit bracket
    the ordinary drawing tools and land as one step, which is the whole shape of
    drawing with this plugin.
    """
    from acadmcp import server

    opened = server.plugin_connect(new_document=True)
    report.check("plugin_connect attaches to a drawing", opened["ok"], str(opened.get("journal")))

    server.step_begin("draw_wall", label="wall", params={"length": 6000})
    server.draw_line([0, 0], [6000, 0], layer="A-WALL")
    first = server.step_commit()
    report.check("a step drawn through the tools commits", first["ok"], first["message"])
    report.check(
        "it is one step owning one entity",
        first["journal"]["count"] == 1 and len(first["step"]["handles"]) == 1,
        str(first["step"].get("handles")),
    )

    # Planned rather than drawn: the drawing must not change yet.
    server.step_begin("draw_room", label="room")
    server.draw_polyline([[0, 0], [4000, 0], [4000, 3000], [0, 3000]], closed=True, layer="A-WALL")
    planned = server.step_commit(planned=True)
    report.check("a step can be planned without drawing it", planned["ok"], planned["message"])
    report.check(
        "planning drew nothing",
        planned["journal"]["count"] == 2 and planned["journal"]["applied"] == 1,
        str(planned["journal"]),
    )

    ran = server.step_control("run_all")
    report.check("step_control runs the plan", ran["ok"], ran["message"])
    report.check(
        "the planned step is now applied", ran["journal"]["applied"] == 2, str(ran["journal"])
    )

    listed = server.step_list()
    report.check("step_list reports both steps", listed["count"] == 2, listed["rendered"])
    report.check(
        "its rows carry state and handles",
        all("state" in row and "handles" in row for row in listed["steps"]),
    )

    back = server.step_control("rollback_to", index=1)
    report.check("step_control rolls back to a step", back["ok"], back["message"])
    report.check(
        "the rollback left one step applied", back["journal"]["applied"] == 1, str(back["journal"])
    )

    accepted = server.step_control("accept", index=1)
    report.check("a step can be accepted", accepted["ok"], accepted["message"])
    refused = server.step_control("rollback_to", index=0)
    report.check(
        "the accepted step blocks a rollback",
        refused["ok"] is False,
        refused.get("refusal") or refused["message"],
    )
    forced = server.step_control("rollback_to", index=0, force=True)
    report.check("force goes through it anyway", forced["ok"], forced["message"])

    cleared = server.step_control("reset")
    report.check("step_control resets the journal", cleared["ok"], cleared["message"])

    status = server.plugin_status()
    report.check(
        "plugin_status reports the plugin",
        status["ok"] and status["available"],
        f"document={status.get('document')} count={status.get('count')}",
    )


def main() -> int:
    report = Report()
    try:
        run(report)
    except Exception as exc:  # noqa: BLE001 - a crash is a failure to report, not a traceback
        report.check("the check itself ran", False, f"{type(exc).__name__}: {exc}")
    print(report.finish(), end="")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
