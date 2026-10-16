"""The tool vocabulary: every tool indexed, the index free of ghosts, and it looks up."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server, terms  # noqa: E402


def _registered() -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_every_registered_tool_has_a_term_record():
    """Adding a tool without a vocabulary entry must fail this gate."""
    missing = sorted(_registered() - set(terms.TOOL_TERMS))
    assert not missing, f"tools with no vocabulary record: {missing}"


def test_no_record_names_a_tool_that_does_not_exist():
    """A rename must fail this gate rather than leave a record pointing at nothing."""
    unknown = sorted(set(terms.TOOL_TERMS) - _registered())
    assert not unknown, f"vocabulary records for unknown tools: {unknown}"


def test_command_names_are_uppercase_and_whitespace_free():
    for name, record in terms.TOOL_TERMS.items():
        for command in record.cad:
            assert command == command.upper(), f"{name}: {command!r} must be uppercase"
            assert command.split() == [command], f"{name}: {command!r} has whitespace"


def test_a_command_name_has_one_owner_unless_it_is_declared_shared():
    owners: dict[str, list[str]] = {}
    for name, record in terms.TOOL_TERMS.items():
        for command in record.cad:
            owners.setdefault(command, []).append(name)
    collisions = {
        command: names
        for command, names in owners.items()
        if len(names) > 1 and command not in terms.SHARED_CAD
    }
    assert not collisions, f"one command, several tools: {collisions}"


def test_a_declared_shared_command_is_actually_shared():
    """A stale exemption would silently stop guarding the command it names."""
    owners: dict[str, int] = {}
    for record in terms.TOOL_TERMS.values():
        for command in record.cad:
            owners[command] = owners.get(command, 0) + 1
    idle = [command for command in terms.SHARED_CAD if owners.get(command, 0) < 2]
    assert not idle, f"declared shared but owned by one tool or none: {idle}"


def test_every_record_carries_three_phrases_including_chinese():
    """Chinese is not decoration: it is how the tool is found by a Chinese speaker."""
    for name, record in terms.TOOL_TERMS.items():
        assert len(record.phrases) >= 3, f"{name} has {len(record.phrases)} phrases"
        assert all(p.strip() for p in record.phrases), f"{name} has an empty phrase"
        assert len(set(record.phrases)) == len(record.phrases), f"{name} repeats a phrase"
        assert any(terms.CJK_PATTERN.search(p) for p in record.phrases), (
            f"{name} has no Chinese phrase"
        )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("承重柱", "draw_column"),
        ("INSERT", "place_element"),
        ("HATCH", "add_hatch"),
        ("轴网", "draw_axis_grid"),
        ("墙", "draw_wall"),
        ("_INSERT", "place_element"),
        ("zoom", "acad_zoom"),
    ],
)
def test_lookup_finds_the_tool_a_draughtsman_would_name(query, expected):
    found = [name for name, _ in terms.lookup(query)]
    assert expected in found, f"{query!r} found {found}"


def test_lookup_prefers_a_command_name_over_a_phrase_or_a_tool_name():
    """`INSERT` is both an AutoCAD command and a substring of nothing else here."""
    first = terms.lookup("INSERT")[0]
    assert first == ("place_element", "cad")


def test_lookup_is_deterministic_and_empty_for_nonsense():
    assert terms.lookup("墙") == terms.lookup("墙")
    assert terms.lookup("zzzz-nothing-here") == []
    assert terms.lookup("") == []
    assert terms.lookup("   ") == []


def test_lookup_says_what_it_matched_on():
    matched = {what for _, what in terms.lookup("INSERT")}
    assert matched <= {"cad", "phrase", "name"}


def test_every_tool_is_reachable_by_its_own_name():
    """The index must not be the only way in: a tool's name always finds it."""
    for name in _registered():
        assert name in {found for found, _ in terms.lookup(name)}


def test_a_chinese_question_reaches_the_tool_that_answers_it():
    for query, expected in [
        ("房间面积", "draw_room"),
        ("中文乱码", "set_text_style"),
        ("撤销", "drawing_undo"),
        ("阵列", "place_element"),
        ("图纸和记录对不对", "drawing_check"),
    ]:
        assert expected in [name for name, _ in terms.lookup(query)], query
