"""The accumulation loop, proven end to end over the wire.

This is the sandbox exercise for the library pillar: a drawing starts from a shipped
template, the standard checker reads it, three identical doors are suggested as a capture,
one is captured and *published*, and then a **fresh drawing** places the published element
by key — the geometry having travelled in the fragment, not in the session. Every step goes
through the real MCP tool surface, so what is proven is the wiring a client gets.

It needs a *project directory*, for a reason worth stating: a project catalogue is
discovered by walking up from the **server's** working directory (``acadmcp.json``), and
publishing writes ``./elements.json`` there. So the directory named by
``ACADMCP_SANDBOX_PROJECT`` must also be the server subprocess's cwd — set
``ACADMCP_SANDBOX_CWD`` to it. The driver reads that variable; this script verifies the two
agree, because a mismatch publishes into one place and looks for it in another.

Run it twice. The first run captures and publishes ``lib_door``; the second finds it already
in the project catalogue, skips the capture/publish, and places it straight away — which is
the cross-process accumulation this whole exercise exists to prove::

    ACADMCP_SANDBOX_PROJECT=/tmp/acadmcp_lib \\
    ACADMCP_SANDBOX_CWD=/tmp/acadmcp_lib \\
    .venv/Scripts/python.exe tests/sandbox_driver.py tests/sandbox_library_check.py

The script file defines ``async def draw(tools)``, the shape ``sandbox_driver.py`` runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: The element captured and published, and the block name a capture gives it.
KEY = "lib_door"
BLOCK = f"ACADMCP_{KEY.upper()}"

#: The repository root — the driver's default server cwd when ACADMCP_SANDBOX_CWD is unset.
ROOT = Path(__file__).resolve().parents[1]

PASSED = 0
FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    """One PASS/FAIL line, and the failure recorded so the run can end non-zero."""
    global PASSED
    if ok:
        PASSED += 1
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label}" + (f": {detail}" if detail else ""))


def _project_dir() -> Path:
    """The project directory, refused with the reason when the variable is not set."""
    raw = os.environ.get("ACADMCP_SANDBOX_PROJECT", "").strip()
    if not raw:
        raise SystemExit(
            "ACADMCP_SANDBOX_PROJECT is not set. Point it at the directory this exercise "
            "runs in (it will hold acadmcp.json and elements.json), and set "
            "ACADMCP_SANDBOX_CWD to the same directory so the server discovers the "
            "catalogue it publishes"
        )
    path = Path(raw).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _server_cwd() -> Path:
    """Where the driver actually launched the server, mirroring its own default.

    The driver defaults to the repository root when ``ACADMCP_SANDBOX_CWD`` is unset, so the
    comparison below still reports a real path rather than "None".
    """
    raw = os.environ.get("ACADMCP_SANDBOX_CWD", "").strip()
    return Path(raw).resolve() if raw else ROOT


def _ensure_project_files(project: Path) -> None:
    """Create the project's own files when they are missing. Idempotent by construction."""
    settings_file = project / "acadmcp.json"
    if not settings_file.exists():
        settings_file.write_text(
            json.dumps({"catalogs": ["elements.json"]}, indent=2), encoding="utf-8"
        )
        print(f"created {settings_file}")
    elements = project / "elements.json"
    if not elements.exists():
        elements.write_text(json.dumps({"elements": []}, indent=2), encoding="utf-8")
        print(f"created {elements}")


def _published_keys(project: Path) -> set[str]:
    """The element keys the project catalogue already holds, or none when it will not read."""
    try:
        payload = json.loads((project / "elements.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"note: could not read the project catalogue ({exc}); treating it as empty")
        return set()
    rows = payload.get("elements") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("key", "")).strip().lower()
        for row in rows
        if isinstance(row, dict) and row.get("key")
    }


async def draw(tools) -> None:  # noqa: ANN001 - the SDK's client wrapper, as its siblings take it
    project = _project_dir()
    launched_in = _server_cwd()
    _check(
        "the server's working directory is the project directory",
        launched_in == project,
        f"ACADMCP_SANDBOX_CWD={launched_in} but ACADMCP_SANDBOX_PROJECT={project}; a publish "
        "writes into one and the next call looks in the other",
    )
    _ensure_project_files(project)
    already = _published_keys(project)

    # 1. a drawing from the shipped template, which brings the sheet and the standard's layers.
    new = await tools.call("drawing_new", backend="dxf", template="gb_a3_arch")
    _check("drawing_new(template='gb_a3_arch') reports a sheet", bool(new.get("sheet")), str(new))
    _check(
        "the template that was applied is named in the reply",
        (new.get("template") or {}).get("key") == "gb_a3_arch",
        str(new.get("template")),
    )
    frame = await tools.call("listing_entities", layer="A-SHEET", limit=1000)
    _check("the sheet frame is drawn on A-SHEET", int(frame.get("count", 0)) > 0, str(frame))

    # 2. the standard in force, reported by status and checked by the checker.
    status = await tools.call("drawing_status")
    _check(
        "drawing_status reports the standard in force",
        bool((status.get("standard") or {}).get("in_force")),
        str(status.get("standard")),
    )
    checked = await tools.call("drawing_check_standard")
    _check(
        "drawing_check_standard is ok and returns an issues list",
        checked.get("ok") is True and isinstance(checked.get("issues"), list),
        str(checked),
    )
    _check("the check names the standard it used", bool(checked.get("standard")), str(checked))

    # 3. a wall with three identical doors, which is the repetition suggest exists to find.
    await tools.call("draw_wall", points=[[0, 0], [9000, 0]], thickness=200)
    for _ in range(3):
        await tools.call("draw_door", hinge=[2000, 0], width=900)
    found = await tools.call("catalog_suggest", min_count=3)
    suggestions = found.get("suggestions") or []
    top = suggestions[0] if suggestions else {}
    _check(
        "catalog_suggest finds three steps that drew the same door",
        int(top.get("count", 0)) >= 3 and top.get("op") == "draw_door",
        str(suggestions),
    )

    # 4. capture one of those doors and publish it — only the first run, so the second run
    #    proves the element survived in the project catalogue rather than in this session.
    if KEY not in already:
        steps = list(top.get("steps") or [])
        captured = await tools.call(
            "element_capture", steps=steps[:1], key=KEY, layer="A-DOOR", category="door"
        )
        _check(
            "element_capture wrote the block definition",
            captured.get("block") == BLOCK,
            str(captured),
        )
        verified = await tools.call("drawing_verify")
        _check(
            "the drawing still matches its journal after the capture",
            verified.get("verdict") == "matches",
            str(verified.get("verdict")),
        )
        published = await tools.call("element_publish", key=KEY)
        file_on_disk = Path(str(published.get("file", ""))).is_file()
        fragment_on_disk = Path(str(published.get("fragment", ""))).is_file()
        _check("element_publish wrote the catalogue file", file_on_disk, str(published.get("file")))
        _check(
            "element_publish wrote the geometry fragment",
            fragment_on_disk,
            str(published.get("fragment")),
        )
        _check(
            "the fragment is the element's own file",
            Path(str(published.get("fragment", ""))).stem == KEY,
            str(published.get("fragment")),
        )
        _check(
            "the published key is in the project catalogue",
            KEY in _published_keys(project),
            str(project / "elements.json"),
        )
    else:
        print(f"SKIP the capture and publish of '{KEY}': already published in {project}")

    # 5. a brand-new drawing — a fresh backend with no block of ours — places the element by
    #    key, its geometry imported from the published fragment.
    await tools.call("drawing_new", backend="dxf", template="gb_a3_arch")
    placed = await tools.call("place_element", type=KEY, at=[1000, 1000])
    _check("place_element placed the published element", placed.get("ok") is True, str(placed))
    verified = await tools.call("drawing_verify")
    _check(
        "the new drawing matches its journal",
        verified.get("verdict") == "matches",
        str(verified.get("verdict")),
    )
    listed = await tools.call("listing_entities", limit=10000)
    refs = [
        row
        for row in listed.get("entities", [])
        if row.get("type") == "INSERT" and row.get("block") == BLOCK
    ]
    _check(
        f"the block reference for '{BLOCK}' is on the new drawing",
        len(refs) == 1,
        str([r.get("block") for r in listed.get("entities", []) if r.get("type") == "INSERT"]),
    )

    print(f"library check: {PASSED} passed, {len(FAILURES)} failed")
    if FAILURES:
        raise SystemExit("FAILED: " + "; ".join(FAILURES))
