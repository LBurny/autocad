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

import os
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

    def note(self, text: str) -> None:
        """A measured name or number, for the log rather than for pass or fail."""
        self.lines.append(f"[note] {text}")

    def finish(self) -> str:
        summary = (
            "\nACADMCP-RPC: all checks passed\n"
            if self.failures == 0
            else f"\nACADMCP-RPC: {self.failures} CHECK(S) FAILED\n"
        )
        return "\n".join(self.lines) + "\n" + summary


#: Where AutoCAD 2026 keeps its block libraries on this machine. Only ever used for the
#: survey at the end, and overridable because another release or another language puts
#: them somewhere else.
SAMPLE_DIR = Path(
    os.environ.get("ACADMCP_SAMPLE_DIR", r"F:\AutoCAD\AutoCAD 2026\Sample\zh-CN\Dynamic Blocks")
)

#: The files worth reading: a factory is architectural and structural before it is anything
#: else.
SURVEY = [
    "Architectural - Metric.dwg",
    "Structural - Metric.dwg",
    "Mechanical - Metric.dwg",
]


def line(x1: float, y1: float, x2: float, y2: float, layer: str = "A-WALL") -> dict[str, Any]:
    return {"kind": "line", "points": [x1, y1, x2, y2], "layer": layer}


def entity_count(backend: PluginBackend) -> int:
    return len(backend.entities(limit=1000))


def _abbreviate(values: list[Any], keep: int = 8) -> str:
    """A few of the values a parameter accepts, so the log stays readable."""
    if len(values) <= keep:
        return str(values)
    return f"{values[:keep]} … {len(values)} in all"


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

        # --- blocks: an insert is a step, its definition is document state ---
        # The engine checks this in AutoCAD's own process against a library of its own; here
        # it is a real element from a library AutoCAD shipped, placed at a chosen size, which
        # is the thing the element catalogue is for.
        library = SAMPLE_DIR / SURVEY[1]
        if library.exists():
            catalogued = client.call("blocks", source=str(library))["blocks"]
            dynamic = [b for b in catalogued if b.get("is_dynamic")]
            report.check(
                "a library file can be listed without opening it as a drawing",
                len(catalogued) > 0,
                f"{len(catalogued)} definition(s), {len(dynamic)} dynamic",
            )

            name = dynamic[0]["name"] if dynamic else catalogued[0]["name"]
            first = client.call("ensure_block", block=name, source=str(library))
            report.check("a block can be imported from the library", first["created"] is True, str(first))
            again = client.call("ensure_block", block=name, source=str(library))
            report.check("importing it a second time does nothing", again["created"] is False, str(again))

            imported = client.call("blocks")["blocks"]
            report.check("the imported definition is now in this drawing", name in [b["name"] for b in imported])

            # A dynamic block has to arrive still dynamic, with its parameters intact:
            # importing it is useless if what lands is a frozen picture of it.
            definition = next((b for b in imported if b["name"] == name), {})
            report.check(
                "it arrived still dynamic, with the same parameters",
                definition.get("is_dynamic") is True
                and [p["name"] for p in definition.get("properties", [])]
                == [p["name"] for p in next((b for b in dynamic if b["name"] == name), {"properties": []}).get(
                    "properties", []
                )],
                f"dynamic={definition.get('is_dynamic')}, "
                f"{len(definition.get('properties', []))} parameter(s)",
            )

            # Place it at a chosen size. Which parameter to use is read off the definition
            # rather than written down here: the library is AutoCAD's, not ours, and a test
            # that hardcodes Chinese parameter names would be a test of this machine.
            settable = [p for p in definition.get("properties", []) if not p["read_only"]]
            sized = next((p for p in settable if p["values"]), settable[0] if settable else None)
            wanted = (
                sized["values"][len(sized["values"]) // 2]
                if sized and sized["values"]
                else 250.0
            )
            report.note(
                f"placing '{name}' with {sized['name']}={wanted}" if sized else f"'{name}' has no settable parameter"
            )

            placed = client.call(
                "apply",
                step={
                    "op": "place_element",
                    "label": f"{name} from the library",
                    "primitives": [
                        {
                            "kind": "insert",
                            "points": [0, 0],
                            "block": name,
                            "source": str(library),
                            "layer": "S-COLUMN",
                            "properties": {sized["name"]: wanted} if sized else {},
                        }
                    ],
                },
            )
            result = placed["result"]
            report.check("a step that inserts a library block commits", result["ok"], result["message"])

            handle = (result.get("step") or {}).get("handles", [""])[0]
            drawn = [row for row in client.call("entities")["entities"] if row["handle"] == handle]
            report.note(
                f"the placed reference resolves to '{drawn[0]['block']}'"
                f" (definition '{drawn[0]['definition']}', dynamic={drawn[0]['dynamic']})"
                if drawn
                else "the placed reference is not in the drawing"
            )
            report.check(
                "it is in the drawing as a dynamic block reference",
                bool(drawn)
                and drawn[0]["type"] == "AcDbBlockReference"
                and drawn[0]["dynamic"] is True,
                str(drawn or handle),
            )
            if sized and drawn:
                actual = drawn[0].get("properties", {}).get(sized["name"])
                report.check(
                    "the size it was placed at is the size it has",
                    actual is not None and abs(float(actual) - float(wanted)) < 1e-6,
                    f"{sized['name']}={actual}, wanted {wanted}",
                )

            back = client.call("rollback_to", index=0, force=True)
            report.check("rolling the insert back succeeds", back["result"]["ok"], back["result"]["message"])
            report.check(
                "the definition is still there after the rollback, and the reference is not",
                name in [b["name"] for b in client.call("blocks")["blocks"]]
                and not [
                    row
                    for row in client.call("entities")["entities"]
                    if row["handle"] == handle
                ],
            )
            client.call("reset")
        else:
            report.note(f"no library to read at {library}; the block checks were skipped")

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
    place_elements(report)
    survey_libraries(report)


def place_elements(report: Report) -> None:
    """Place a catalogue's worth of real library elements into the open drawing.

    The end of the journey, and the only check that uses the whole stack at once: the
    catalogue says which block is a door, the plugin imports the definition out of AutoCAD's
    library, the reference is sized through its dynamic properties, the number comes from the
    tags the drawing already has, and the whole thing is one step that can be rolled back
    without taking the definition with it.

    It also leaves the drawing in a state worth photographing, which is the point of taking
    the picture here rather than of an empty sheet.
    """
    from acadmcp import server

    listed = server.element_types()
    report.check(
        "the catalogue loads and matches the library it names",
        listed["ok"] and listed["count"] > 0 and listed.get("matches_library") is True,
        f"{listed['count']} element(s); problems: {listed.get('problems')} "
        f"skipped: {listed['skipped']}",
    )

    # A frame of studs, a wall of windows and a pair of doors, at sizes the catalogue's
    # variants select rather than sizes written here.
    frame = server.place_grid("stud", xs=[0, 6000, 12000], ys=[0, 6000], variant="50 x 150")
    report.check("a grid of elements places in one step", frame["ok"], frame["message"])
    report.check(
        "one per axis intersection, each numbered from the drawing",
        frame["placed"] == 6 and frame["tags"] == [f"G-{i}" for i in range(1, 7)],
        f"placed={frame['placed']} tags={frame['tags']}",
    )

    wall = server.place_along("window", start=[0, 6000], end=[12000, 6000], spacing=3000, variant="1200")
    report.check("a run of elements places in one step", wall["ok"], wall["message"])
    report.check(
        "an element with no tags in the drawing yet starts at one",
        wall["tags"] == ["C-1", "C-2", "C-3", "C-4", "C-5"],
        str(wall["tags"]),
    )

    door = server.place_element("door", at=[3000, 6000], variant="900", rotation=90)
    report.check("a single element places", door["ok"], door["message"])
    report.check(
        "it is one step owning the reference and its tag",
        len((door.get("step") or {}).get("handles") or []) == 2,
        str(door.get("step")),
    )

    # What actually landed in the drawing, read back off the entities: the size asked for
    # and nothing else's.
    placed = [
        row
        for row in server.listing_entities(limit=500)["entities"]
        if row.get("block")
    ]
    sizes = [row for row in placed if row.get("properties", {}).get("立柱尺寸") == "50 x 150"]
    report.check(
        "every placed element is a block reference at the size it was asked for",
        len(sizes) == 6,
        f"{len(sizes)} of {len(placed)} block reference(s) are 50 x 150 studs",
    )
    report.check(
        "the drawing's tags are exactly the ones that were placed",
        sorted(text for text in _texts(server) if text.startswith(("G-", "C-", "M-")))
        == [f"C-{i}" for i in range(1, 6)] + [f"G-{i}" for i in range(1, 7)] + ["M-1"],
        str(sorted(text for text in _texts(server) if text.startswith(("G-", "C-", "M-")))),
    )

    # The picture is taken here, with the elements still in the drawing: photographing
    # after the rollback below would show an empty sheet.
    _photograph(report)

    # And now the invariant that matters, one more time, on elements: rolling back removes
    # the references and leaves every definition they came from.
    before = [row["handle"] for row in placed]
    rolled = server.step_control("rollback_to", index=0, force=True)
    report.check("rolling the elements back succeeds", rolled["ok"], rolled["message"])
    after = [row["handle"] for row in server.listing_entities(limit=500)["entities"]]
    report.check(
        "the rollback removed every element it placed",
        not set(before) & set(after),
        f"{len(set(before) & set(after))} of {len(before)} left",
    )
    still_defined = {block["name"] for block in server.library_list()["blocks"]}
    report.check(
        "and left the definitions it imported standing",
        {"立柱 - 公制", "窗 - 公制", "门 - 公制"} <= still_defined,
        f"definitions now: {sorted(still_defined)}",
    )


def _texts(server: Any) -> list[str]:
    return [str(row.get("text", "")) for row in server.listing_entities(limit=500)["entities"]]


def _photograph(report: Report) -> None:
    """Take the picture the user asked for, if one was asked for."""
    path = os.environ.get("ACADMCP_SHOT_ELEMENTS")
    if not path:
        return
    from acadmcp import server

    try:
        framed = server.acad_zoom_extents()
        report.check("the view can be framed before photographing it", framed["ok"], str(framed))
        server.drawing_screenshot(path)
        report.note(f"photographed the placed elements to {path}")
    except Exception as exc:  # noqa: BLE001 - a missing picture is not a failed check
        report.note(f"could not photograph the elements: {type(exc).__name__}: {exc}")


def survey_libraries(report: Report) -> None:
    """Read the block libraries AutoCAD shipped with, and report what is in them.

    Reported as notes rather than checks, because this is data: the element catalogue is
    written against these names, so they have to be looked at rather than guessed. The one
    thing here that is a check is the one that decides whether a placed element can be
    sized at all — that a dynamic block's parameters come back, with the values they accept.
    """
    if not SAMPLE_DIR.is_dir():
        report.note(f"no sample libraries at {SAMPLE_DIR}")
        return

    client = PluginClient()
    try:
        client.connect()
    except PluginUnavailable as exc:
        report.note(f"could not survey the libraries: {exc}")
        return

    try:
        seen_parameters = False
        for filename in SURVEY:
            path = SAMPLE_DIR / filename
            if not path.exists():
                continue
            blocks = client.call("blocks", source=str(path))["blocks"]
            dynamic = [b for b in blocks if b.get("is_dynamic")]
            report.note(f"{filename}: {len(blocks)} definition(s), {len(dynamic)} dynamic")
            for block in blocks[:80]:
                mark = "dynamic" if block.get("is_dynamic") else f"{block.get('entities', '?')} entities"
                report.note(f"  {block['name']}  ({mark})")
            for block in dynamic[:10]:
                properties = block.get("properties") or []
                seen_parameters = seen_parameters or bool(properties)
                described = "; ".join(
                    f"{p['name']} [{p['units']}]"
                    + ("" if not p["read_only"] else " read-only")
                    + (f" in {_abbreviate(p['values'])}" if p["values"] else "")
                    for p in properties[:6]
                )
                report.note(f"  params of {block['name']}: {described or '(none)'}")

        report.check(
            "a dynamic block's parameters and their allowed values can be read",
            seen_parameters,
        )
    except PluginError as exc:
        report.check("the library survey completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        client.close()


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
