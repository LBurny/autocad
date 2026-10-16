"""The prompt templates: registered, renderable, and naming only real tools.

A prompt that names a tool the server does not have is the same bug shape as a
handler keyed on an op name no tool produces — so the tool names are read out of
the registry, never trusted.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402

PROMPTS = {p.name: p for p in server.mcp._prompt_manager.list_prompts()}
TOOLS = {t.name for t in server.mcp._tool_manager.list_tools()}

EXPECTED = {"floor_plan", "headless_to_live", "drawing_review"}


def _render(name: str, args: dict | None = None) -> str:
    result = asyncio.run(server.mcp.get_prompt(name, args or {}))
    return result.messages[0].content.text


def test_the_three_prompts_are_registered():
    assert set(PROMPTS) == EXPECTED


def test_every_prompt_carries_a_description():
    missing = [name for name, p in PROMPTS.items() if not (p.description or "").strip()]
    assert not missing


def test_every_prompt_renders_to_substantial_text():
    for name in PROMPTS:
        assert len(_render(name)) > 400, name


def test_prompts_name_only_registered_tools():
    snake_call = re.compile(r"\b([a-z][a-z0-9_]{2,})\(")
    for name in PROMPTS:
        for word in snake_call.findall(_render(name)):
            assert word in TOOLS, f"{name} names {word}(...), which is not a registered tool"


def test_floor_plan_names_the_core_ops():
    text = _render("floor_plan")
    for tool in [
        "draw_wall_with_openings",
        "draw_room",
        "place_element",
        "drawing_sheet",
        "drawing_check",
        "drawing_summary",
    ]:
        assert tool in text


def test_floor_plan_records_the_conventions():
    text = _render("floor_plan")
    assert "closed=True" in text  # the envelope is one closed run, never four runs
    assert "centreline" in text.lower()  # room boundaries are wall centrelines


def test_floor_plan_arguments_have_defaults_and_render():
    assert "Tower B" in _render("floor_plan", {"building_name": "Tower B"})


def test_headless_to_live_names_replay_and_the_skip_rule():
    text = _render("headless_to_live")
    assert "drawing_replay" in text
    assert "skipped" in text  # unplaceable steps are reported, never silently dropped


def test_drawing_review_forbids_an_unchecked_all_clear():
    text = _render("drawing_review")
    assert "drawing_check" in text
    assert "never" in text.lower()  # never claim "no issues" without running all three checks
