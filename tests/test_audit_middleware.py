"""The audit middleware and the server instructions.

The middleware follows the mcp 2.3.0 ``ServerMiddleware`` protocol — an async
``(ctx, call_next) -> result`` callable handed to the constructor's
``middleware`` list — so these tests fake the two protocol objects and exercise
the logic without a wire session.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402


def _ctx(method: str = "tools/call", name: str = "drawing_status") -> SimpleNamespace:
    return SimpleNamespace(method=method, params={"name": name, "arguments": {}})


def test_middleware_logs_a_tool_call_with_elapsed(caplog):
    seen = {}

    async def call_next(ctx):
        seen["ctx"] = ctx
        return {"content": [], "isError": False}

    with caplog.at_level(logging.INFO, logger="acadmcp"):
        result = asyncio.run(server._AuditMiddleware()(_ctx(), call_next))
    assert result == {"content": [], "isError": False}
    assert seen["ctx"].method == "tools/call"
    record = next(r for r in caplog.records if "drawing_status" in r.getMessage())
    assert "TOOL OK" in record.getMessage()
    assert re.search(r"\d+\.\d\s*ms", record.getMessage())


def test_middleware_marks_a_refused_result(caplog):
    async def call_next(ctx):
        return {"content": [], "isError": True}

    with caplog.at_level(logging.INFO, logger="acadmcp"):
        asyncio.run(server._AuditMiddleware()(_ctx(), call_next))
    record = next(r for r in caplog.records if "drawing_status" in r.getMessage())
    assert "TOOL ERR" in record.getMessage()
    assert record.levelno == logging.WARNING


def test_middleware_reads_is_error_off_a_result_object(caplog):
    async def call_next(ctx):
        return SimpleNamespace(is_error=True)

    with caplog.at_level(logging.INFO, logger="acadmcp"):
        asyncio.run(server._AuditMiddleware()(_ctx(), call_next))
    record = next(r for r in caplog.records if "drawing_status" in r.getMessage())
    assert "TOOL ERR" in record.getMessage()


def test_middleware_passes_non_tool_methods_through_unlogged(caplog):
    async def call_next(ctx):
        return {"prompts": []}

    with caplog.at_level(logging.INFO, logger="acadmcp"):
        asyncio.run(server._AuditMiddleware()(_ctx(method="prompts/list"), call_next))
    assert not [r for r in caplog.records if r.name == "acadmcp"]


def test_middleware_logs_and_reraises_a_failure(caplog):
    async def call_next(ctx):
        raise RuntimeError("boom")

    with (
        caplog.at_level(logging.WARNING, logger="acadmcp"),
        pytest.raises(RuntimeError, match="boom"),
    ):
        asyncio.run(server._AuditMiddleware()(_ctx(), call_next))
    assert any("TOOL ERR" in r.getMessage() for r in caplog.records)


def test_server_carries_the_middleware_and_the_instructions():
    assert any(isinstance(mw, server._AuditMiddleware) for mw in server.mcp.middleware)
    assert server.mcp.instructions


def test_instructions_name_only_registered_tools_and_point_at_the_prompts():
    tools = {t.name for t in server.mcp._tool_manager.list_tools()}
    snake_call = re.compile(r"\b([a-z][a-z0-9_]{2,})\(")
    for word in snake_call.findall(server.mcp.instructions):
        assert word in tools, f"instructions name {word}(...), which is not a registered tool"
    for prompt in ("floor_plan", "headless_to_live", "drawing_review"):
        assert prompt in server.mcp.instructions
