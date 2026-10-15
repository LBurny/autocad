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


def _fixture_layers() -> list[str]:
    import ezdxf

    doc = ezdxf.readfile(ROOT / FIXTURE)
    return [str(e.dxf.get("layer", "0")) for e in doc.modelspace()]


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
