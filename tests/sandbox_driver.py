"""Drive one drawing script against a freshly launched acadmcp server.

Usage:

    .venv/Scripts/python.exe tests/sandbox_driver.py path/to/script.py

The script file must define ``async def draw(tools)``. ``tools.call(name,
**args)`` returns the tool's parsed JSON (raw text when it is not JSON) and
raises ``ToolFailed`` with the server's full message when the call errors.

This is the sandbox for live exercises: every run launches its own server
process from the *current working tree*, so the code under test is the code as
it stands right now — never a server started before the last edit — and
concurrent drivers never share drawing state. The driver prints the git HEAD
it launched from so a report can prove that freshness.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class ToolFailed(RuntimeError):
    pass


class Tools:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, **args):
        result = await self.session.call_tool(tool, args)
        if result.is_error:
            text = result.content[0].text
            # The SDK's error text already starts "Error executing tool <name>".
            raise ToolFailed(text if text.startswith("Error executing tool") else f"{tool}: {text}")
        blocks = []
        for block in result.content:
            if block.type == "text":
                try:
                    blocks.append(json.loads(block.text))
                except json.JSONDecodeError:
                    blocks.append(block.text)
            else:  # image content: don't haul megabytes into the script
                blocks.append({"image": True, "mimeType": block.mime_type})
        return blocks[0] if len(blocks) == 1 else blocks


async def _run(script: Path) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT
    ).stdout.splitlines()
    print(f"sandbox: server code at HEAD {head}, {len(dirty)} uncommitted file(s)")

    spec = importlib.util.spec_from_file_location("drawing_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    server = StdioServerParameters(
        command=str(ROOT / ".venv" / "Scripts" / "python.exe"),
        args=["-m", "acadmcp.server"],
        cwd=str(ROOT),
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await module.draw(Tools(session))
    print("sandbox: draw() finished clean")


if __name__ == "__main__":
    asyncio.run(_run(Path(sys.argv[1]).resolve()))
