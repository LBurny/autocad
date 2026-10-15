"""The MCP tool surface itself: annotations, naming and pagination metadata.

`@mcp.tool()` returns the decorated function unchanged in the installed mcp
version, so the tools are called directly; the registry is read off the server's
tool manager.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402


def _tools() -> dict:
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


def test_every_tool_carries_annotations():
    tools = _tools()
    assert len(tools) == 52
    missing = [name for name, t in tools.items() if t.annotations is None]
    assert not missing


def test_tool_names_are_snake_case():
    for name in _tools():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name


def test_server_name_follows_the_service_mcp_convention():
    assert server.mcp.name == "autocad_mcp"


def test_annotations_match_behaviour():
    tools = _tools()

    read_only = {
        "drawing_help",
        "drawing_status",
        "drawing_history",
        "drawing_summary",
        "drawing_verify",
        "listing_entities",
        "plugin_status",
        "step_list",
        "element_types",
        "library_list",
    }
    for name in read_only:
        ann = tools[name].annotations
        assert ann.read_only_hint is True and ann.idempotent_hint is True, name

    destructive = {
        "drawing_undo",
        "drawing_rollback_to",
        "step_control",
        "drawing_new",
        "drawing_replay",
        "drawing_open",
        "acad_connect",
        "plugin_install",
    }
    for name in destructive:
        assert tools[name].annotations.destructive_hint is True, name

    # Additive drawing: mutating but not destructive, and two calls draw twice.
    ann = tools["draw_line"].annotations
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False

    for name, t in tools.items():
        assert t.annotations.open_world_hint is False, name


def test_unknown_arguments_are_rejected():
    """A misspelt parameter must fail validation, not be dropped silently.

    The SDK builds the input models with extra="ignore"; server.py flips them
    to forbid. A stale server answering a newer client is exactly the silent
    failure this prevents.
    """
    tool = _tools()["drawing_status"]
    with pytest.raises(Exception, match="bogus"):
        tool.fn_metadata.validate_arguments({"bogus": 1})
    tool.fn_metadata.validate_arguments({})  # a valid call still passes


def test_backend_error_is_a_tool_error():
    """The SDK masks every non-ToolError behind 'Error executing tool <name>'."""
    from mcp.server.mcpserver.exceptions import ToolError

    from acadmcp.backends.base import BackendError

    assert issubclass(BackendError, ToolError)


def test_geometry_value_errors_keep_their_message(fresh_dxf):
    """Bad input raises ValueError deep in geom/ops; the SDK would mask it
    behind 'Error executing tool <name>' if server.py did not re-raise it as a
    ToolError. The message is the only thing the model can correct itself
    with, so it must survive."""
    from acadmcp.backends.base import BackendError

    draw_wall = _tools()["draw_wall"].fn  # the guarded wrapper, not the bare def
    with pytest.raises(BackendError, match="at least two distinct points"):
        draw_wall(points=[[0, 0]])


@pytest.fixture
def fresh_dxf():
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def test_listing_entities_reports_has_more(fresh_dxf):
    server.draw_line([0, 0], [100, 0])
    server.draw_line([0, 0], [0, 100])
    server.draw_line([0, 0], [100, 100])

    cut = server.listing_entities(limit=2)
    assert cut["count"] == 2 and cut["has_more"] is True
    assert cut["next_offset"] == 2 and cut["total_count"] == 3
    assert len(cut["entities"]) == 2

    rest = server.listing_entities(limit=2, offset=2)
    assert rest["count"] == 1 and rest["has_more"] is False
    assert rest["next_offset"] is None and rest["total_count"] == 3

    whole = server.listing_entities(limit=50)
    assert whole["count"] == 3 and whole["has_more"] is False
    assert whole["total_count"] == 3


def test_listing_entities_validates_pagination(fresh_dxf):
    with pytest.raises(ValueError, match="limit"):
        server.listing_entities(limit=0)
    with pytest.raises(ValueError, match="offset"):
        server.listing_entities(offset=-1)

    # An offset past the end is an empty page, and the exhausted walk still
    # knows the true total — it must not report the offset as the count.
    beyond = server.listing_entities(offset=50)
    assert beyond["count"] == 0 and beyond["has_more"] is False
    assert beyond["total_count"] == 0


def test_listing_entities_tells_an_unknown_layer_from_an_empty_one(fresh_dxf):
    server.layer_create("X-EMPTY")

    empty = server.listing_entities(layer="X-EMPTY")
    assert empty["count"] == 0 and empty["layer_known"] is True

    typo = server.listing_entities(layer="X-EMTPY")
    assert typo["count"] == 0 and typo["layer_known"] is False


_BLOCKS = [{"name": f"BLOCK-{i}"} for i in range(4)]


class _FakeBlocksClient:
    """Answers the blocks RPC with a fixed four-block library, limit honoured."""

    def call(self, method: str, **kwargs):
        assert method == "blocks"
        return {"blocks": _BLOCKS[: kwargs["limit"]]}


def test_library_list_reports_has_more(monkeypatch):
    monkeypatch.setattr(server, "_plugin_client", _FakeBlocksClient())

    cut = server.library_list(limit=3)
    assert cut["ok"] and cut["count"] == 3 and cut["has_more"] is True
    assert cut["next_offset"] == 3 and cut["total_count"] is None
    assert len(cut["blocks"]) == 3

    rest = server.library_list(limit=3, offset=3)
    assert rest["count"] == 1 and rest["has_more"] is False
    assert rest["next_offset"] is None and rest["total_count"] == 4

    whole = server.library_list(limit=10)
    assert whole["count"] == 4 and whole["has_more"] is False
    assert whole["total_count"] == 4
