"""The read-only tools publish their output schemas, and the wire keeps every key.

The SDK derives a tool's ``outputSchema`` from its return annotation, and the
``structuredContent`` it hands the client is the reply *validated against that
schema* — pydantic drops keys a TypedDict does not declare, silently. These
tests pin both ends:

- the published schema names exactly the keys the tool's TypedDict in
  ``acadmcp.replies`` declares (not the vacuous ``DictOutput`` an unannotated
  ``dict[str, Any]`` return publishes), and the return annotation really is
  that TypedDict, so a drift back to a plain dict fails here;
- a call through the tool's own ``convert_result`` — the same code path the
  wire takes — returns a ``structured_content`` carrying every key the tool
  produced, so a reply key no TypedDict declares fails here instead of
  vanishing from a structured-content client.

The drawing side of the round trip is exercised on the dxf backend. The com and
plugin backends contribute their own keys to ``StatusReply`` (document,
sidecar, insunits, ...); those are declared from the backends' source and are
optional under ``total=False``, so their absence here proves nothing and their
presence on a live backend is what the declaration is for.
"""

import sys
import typing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import replies, server  # noqa: E402

#: The tools with a declared reply shape, and which TypedDict declares it.
TOOL_REPLIES = {
    "drawing_status": replies.StatusReply,
    "drawing_history": replies.HistoryReply,
    "drawing_summary": replies.SummaryReply,
    "drawing_verify": replies.VerifyReply,
    "drawing_critique": replies.CritiqueReply,
    "drawing_check_standard": replies.CheckStandardReply,
    "listing_entities": replies.EntitiesReply,
    "element_types": replies.ElementTypesReply,
    "template_list": replies.TemplateListReply,
    "catalog_suggest": replies.SuggestReply,
}


@pytest.fixture()
def drawing():
    """A small drawn plan, so the read tools have something to report.

    The three identical doors are what gives catalog_suggest a group to find;
    everything else feeds summary/verify/critique a row or two.
    """
    server._state.reset(server.make_backend("dxf"), "dxf")
    tool = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    tool["draw_wall_with_openings"].fn(
        points=[[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
        closed=True,
        openings=[{"start": 1000, "width": 900, "kind": "door", "swing": "left"}],
    )
    tool["draw_room"].fn(boundary=[[0, 0], [4000, 0], [4000, 3000], [0, 3000]], name="办公室")
    for _ in range(3):
        tool["draw_door"].fn(hinge=[6000, 0], width=900, rotation=0, swing="left")
    tool["add_text"].fn(pos=[1000, 1500], text="标注", height=300)
    yield tool
    server._state.backend = None


def _declared(reply: type) -> set[str]:
    return set(typing.get_type_hints(reply))


def test_every_mapped_tool_exists():
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    assert set(TOOL_REPLIES) <= set(tools)


def test_published_schema_is_the_declared_one_not_the_vacuous_dict():
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name, reply in TOOL_REPLIES.items():
        schema = tools[name].output_schema
        assert schema, f"{name} publishes no output schema"
        assert not str(schema.get("title", "")).endswith("DictOutput"), (
            f"{name} still publishes the SDK's vacuous dict schema"
        )
        published = set(schema.get("properties", {}))
        assert published == _declared(reply), (
            f"{name}: schema keys {sorted(published)} != declared {sorted(_declared(reply))}"
        )


@pytest.mark.parametrize("name", sorted(TOOL_REPLIES))
def test_wire_round_trip_keeps_every_key(drawing, name):
    tool = drawing[name]
    calls = [({}, None)]
    if name == "listing_entities":
        # The layer-filtered call is the one that carries layer_known.
        calls.append(({"layer": "A-WALL"}, None))
    for arguments, _ in calls:
        raw = tool.fn(**arguments)
        result = tool.fn_metadata.convert_result(raw)
        structured = result.structured_content
        assert structured is not None, f"{name} produced no structured content"
        lost = set(raw) - set(structured)
        assert not lost, f"{name}: the wire drops {sorted(lost)} — declare them in replies.py"
        undeclared = set(structured) - _declared(TOOL_REPLIES[name])
        assert not undeclared, f"{name}: {sorted(undeclared)} validate but are undeclared"


def test_suggest_finds_the_repeated_door(drawing):
    """The round trip above only checks keys; this checks a real reply's content."""
    result = drawing["catalog_suggest"].fn_metadata.convert_result(drawing["catalog_suggest"].fn())
    suggestions = result.structured_content["suggestions"]
    assert any(s.get("op") == "draw_door" and s.get("count") == 3 for s in suggestions)
