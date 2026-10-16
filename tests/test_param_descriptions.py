"""Every served tool parameter carries a description, and the table has no ghosts.

The SDK builds each tool's input schema from the bare signature, so a parameter's
type reaches the client but its meaning does not — the description table in
params.py is what fills it in. This test is the gate that keeps the table equal
to the surface: a parameter added without a description fails here, and so does
a table entry that names a tool or parameter that no longer exists.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import params, server  # noqa: E402


def test_every_parameter_has_a_description_and_the_table_has_no_ghosts():
    """apply() reports every bare parameter and every stale entry; both must be empty."""
    issues = params.apply(server.mcp._tool_manager.list_tools())
    assert not issues, "parameter description drift:\n" + "\n".join(issues)


def test_the_imported_server_serves_the_descriptions():
    """Importing server must have applied the table, or the wire schema stays bare."""
    bare = [
        f"{tool.name}.{name}"
        for tool in server.mcp._tool_manager.list_tools()
        for name, prop in (tool.parameters.get("properties") or {}).items()
        if not prop.get("description")
    ]
    assert not bare, f"parameters served without a description: {bare}"
