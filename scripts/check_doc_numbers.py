"""Hold the numbers in the documents equal to the numbers in the tree.

Every figure here has been wrong at least once. The expensive one was silent:
`evaluation.xml` asks how many element types the catalogue offers in the `structure`
category and answered 4, while the tree offers 5 — the catalogue grew a fifth element in
5234722 and the evaluation was last touched one commit earlier, so nothing connected the
two and nothing would ever have noticed. `AGENTS.md` argued the point by hand instead:
"the answers in evaluation.xml ... still hold", which was true of the entity counts and
not of this one.

The rule is the same one the release snapshot follows: a number a human types is a number
that rots, so derive it and fail when the prose disagrees.

    python scripts/check_doc_numbers.py          # report drift and exit non-zero
    python scripts/check_doc_numbers.py --list   # show every derived value
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = "evaluations/fixtures/floor_plan.dxf"
EVALUATION = "evaluations/evaluation.xml"

#: The limit the evaluation asks about when it asks whether listing is cut short.
EVAL_LISTING_LIMIT = 200

#: The page size the evaluation's paging question walks the fixture with.
EVAL_PAGE_LIMIT = 50

#: A pair with no derivation is reported rather than skipped: an answer nothing checks is
#: exactly the kind that went stale here.
NO_DERIVATION = "<no derivation>"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _collected_tests() -> int:
    """How many tests pytest actually collects.

    Collection only; running the suite here would make the gate cost a minute. The number
    is derived rather than counted from source because `tests/test_catalog.py` parametrises,
    so a `def test_` walk would be wrong by however many cases that expands to.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if not match:
        raise RuntimeError(f"could not count tests; pytest said:\n{result.stdout[-800:]}")
    return int(match.group(1))


def _tool_count() -> int:
    from acadmcp import server

    return len(server.mcp._tool_manager.list_tools())


def _journal_version() -> int:
    from acadmcp.journal import Journal

    return int(Journal().to_dict()["version"])


def _fixture_entities() -> list[tuple[str, str]]:
    """(layer, dxftype) per entity. The fixture is modelspace-only, so this is the whole walk."""
    import ezdxf

    doc = ezdxf.readfile(ROOT / FIXTURE)
    return [(str(e.dxf.get("layer", "0")), e.dxftype()) for e in doc.modelspace()]


def _fixture_layers() -> list[str]:
    return [layer for layer, _ in _fixture_entities()]


def _fixture_texts(layer: str) -> list[str]:
    """The string content of every TEXT entity on a layer."""
    import ezdxf

    doc = ezdxf.readfile(ROOT / FIXTURE)
    return [
        str(e.dxf.get("text", ""))
        for e in doc.modelspace()
        if e.dxftype() == "TEXT" and str(e.dxf.get("layer", "")) == layer
    ]


def _default_layer_for(term: str) -> str:
    """The layer the term's one tool draws onto when the caller does not say.

    The lookup must return exactly one tool — the evaluation's question says "the tool" —
    so a second match is the gate failing loudly, not something to pick from.
    """
    import inspect

    from acadmcp import server, terms

    ((name, _),) = terms.lookup(term)
    return inspect.signature(getattr(server, name)).parameters["layer"].default


def _elements() -> list:
    from acadmcp import catalog

    return list(catalog.current().elements.values())


def _by_key(key: str):
    return next(e for e in _elements() if e.key == key)


def derivations() -> dict[str, object]:
    """Every quotable number, derived. Values are what the tree says right now."""
    layers = Counter(_fixture_layers())
    return {
        "tools": _tool_count(),
        "tests": _collected_tests(),
        "journal_version": _journal_version(),
        "fixture_entities": len(_fixture_layers()),
        "fixture_busiest_layer": layers.most_common(1)[0][0],
        "fixture_window_entities": layers["A-WINDOW"],
        "fixture_layers_used": len(layers),
    }


def eval_answers() -> list[tuple[str, str, str]]:
    """Every evaluation pair as (question, the answer written down, the answer derived).

    The question texts are matched to a derivation by a distinctive fragment rather than by
    position, so inserting a pair cannot silently shift every answer by one.
    """
    rows = _fixture_layers()
    layers = Counter(rows)
    entities = _fixture_entities()
    from pathlib import PureWindowsPath

    derived = {
        "How many entities does drawing_status report": str(len(rows)),
        "which layer holds the most": layers.most_common(1)[0][0],
        "How many entities sit on it": str(layers["A-WINDOW"]),
        "How many distinct layers contain at least one entity": str(len(layers)),
        "Does the response's has_more flag come back": str(len(rows) > EVAL_LISTING_LIMIT),
        "how many element types are offered in the": str(
            len([e for e in _elements() if e.category == "structure"])
        ),
        "what is the default variant of the light-gauge steel stud": str(
            _by_key("stud").default_variant
        ),
        "dynamic block parameter whose name is misleading": str(_by_key("window").width_param),
        "exactly one element type carries an empty tag prefix": _by_key("weld_symbol").key,
        # The catalogue writes the library path the way AutoCAD does, backslashes and all,
        # so it is basenamed the way a path is rather than by splitting on "/".
        "imported from a block in which library drawing file": PureWindowsPath(
            _by_key("light").source
        ).name,
        # The paging question's answer is the page walk, not the count: ceil at the page
        # size the question names, over the same universe listing_entities walks (the
        # fixture carries no paper-space entities, so the modelspace walk is the walk).
        "How many listing_entities calls does the full walk take": str(
            -(-len(rows) // EVAL_PAGE_LIMIT)
        ),
        # The largest area label, by the number the label reads — the original string is
        # the answer, so "40.60m²" survives with its padding rather than becoming "40.6".
        "largest area figure labelled": max(
            _fixture_texts("A-ROOM-AREA"), key=lambda t: float(t[: -len("m²")])
        ),
        # A door swing is an arc on the door layer; the leaves are lines.
        "How many door swings does the plan show": str(
            sum(1 for layer, kind in entities if layer == "A-DOOR" and kind == "ARC")
        ),
        "hatch onto when the caller does not say": _default_layer_for("HATCH"),
    }
    out: list[tuple[str, str, str]] = []
    for pair in ET.parse(ROOT / EVALUATION).getroot().findall("qa_pair"):
        question = (pair.findtext("question") or "").strip()
        said = (pair.findtext("answer") or "").strip()
        hit = next((v for k, v in derived.items() if k in question), None)
        out.append((question, said, hit if hit is not None else NO_DERIVATION))
    return out


def check() -> list[str]:
    """Every disagreement as a line a person can act on. Empty when the tree and the docs agree."""
    drift: list[str] = []
    numbers = derivations()

    for key, pattern, name in [
        ("tools", r"\((\d+) tools", "AGENTS.md"),
        ("tests", r"# (\d+) tests", "AGENTS.md"),
        ("tools", r"the (\d+)-tool MCP surface", "README.md"),
        # The source states the count too, in a sentence explaining a design choice, and a
        # docstring is nobody's idea of a document — which is how one of them said 44 for
        # five tools' worth of drift. Anything stating the count is checked, code or prose.
        ("tools", r"At (\d+) tools", "src/acadmcp/terms.py"),
        ("tools", r"vocabulary of the (\d+) tools", "src/acadmcp/server.py"),
    ]:
        found = re.search(pattern, _read(name))
        if found is None:
            drift.append(f"{name}: nothing matching {pattern!r} to compare with {key}")
        elif int(found.group(1)) != numbers[key]:
            drift.append(f"{name}: says {found.group(1)}, the tree has {numbers[key]} ({key})")

    found = re.search(r"writes \((\d+)\)", _read("AGENTS.md"))
    if found is None:
        drift.append("AGENTS.md: no journal payload version claim to compare")
    elif int(found.group(1)) != numbers["journal_version"]:
        drift.append(
            f"AGENTS.md: says journal version {found.group(1)}, "
            f"journal.py writes {numbers['journal_version']}"
        )

    for question, said, want in eval_answers():
        if want == NO_DERIVATION:
            drift.append(f"{EVALUATION}: nothing derives this pair: {question}")
        elif said != want:
            drift.append(f"{EVALUATION}: says {said!r}, the tree derives {want!r} — {question}")
    return drift


def main() -> None:
    parser = argparse.ArgumentParser(description="The docs against the tree.")
    parser.add_argument("--list", action="store_true", help="show every derived value")
    args = parser.parse_args()
    if args.list:
        for key, value in derivations().items():
            print(f"{key:26} {value}")
        return
    drift = check()
    if not drift:
        print(f"ok: {json.dumps(derivations())}")
        return
    for line in drift:
        print(line)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
