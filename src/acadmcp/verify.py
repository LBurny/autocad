"""The drawing reconciled against the journal's own account of it.

The journal is a claim: every applied step says how many entities it drew. This module
compares that claim with what the drawing actually holds, which is the one check the
journal cannot make about itself. It takes plain rows rather than a backend, so it works
the same on all three — `backends/recording.py` and the plugin report the shape it reads —
and it imports nothing from this package and nothing CAD-related.

What it cannot see: the rows carry an entity *count*, not handles, so it can say the total
is wrong without saying which entity is. It is a smoke alarm, not a locator.

It also cannot know a baseline. A drawing opened from a file already holds entities that no
step recorded, and `drawing_open` records nothing about how many, so a positive difference is
reported as `unaccounted` *with a warning* rather than as a fault; a drawing with no steps at
all is `no_baseline`, which is neither agreement nor disagreement.

Why it is worth having: measured on this tree, a mitred envelope with a door, a room label,
an axis grid, a dimension and a placed catalogue element totalled 38 entities, and the
drawing held exactly 38 — through drawing, saving, reopening, undoing and redoing. So a
disagreement is a real fault (a save that dropped entities, a rollback that erased too
much, something that drew outside a step), not an accident of counting.
"""

from __future__ import annotations

from typing import Any

#: The verdicts. A caller branches on this rather than on `ok`, because a verdict of
#: "walk_truncated" or "no_baseline" is neither agreement nor disagreement and `ok` is None.
MATCHES = "matches"
UNACCOUNTED = "unaccounted"
MISSING = "missing"
TRUNCATED = "walk_truncated"
NO_BASELINE = "no_baseline"

#: The state a row is read as when it does not say. Assuming the opposite would let a
#: rollback erase entities the drawing still needs.
APPLIED = "applied"

#: Step states that mean the drawing does not hold the step, so its entities must not be
#: expected. Two vocabularies arrive here: the Python journal writes "applied"/"undone", and
#: the plugin's in-DWG journal writes "done"/"planned"/"failed" (RpcServer.Row lowercases
#: StepState's name). Testing for "undone" alone counted a plugin's planned and failed steps
#: as applied — the same one-vocabulary mistake that made a plugin undo report ok while
#: erasing nothing, and the tables in `critique.py` and `suggest.py` already carry both
#: spellings. `tests/test_plugin_row_contract.py` pins all four against each other so they
#: cannot drift apart again.
INACTIVE_STATES = frozenset({"undone", "planned", "failed", "rejected"})


def reconcile(
    steps: list[dict[str, Any]],
    entities: list[dict],
    *,
    truncated: bool = False,
    reported: int | None = None,
) -> dict[str, Any]:
    """What the applied steps claim against what the drawing holds.

    `truncated` says the entity walk stopped at its limit, which makes the count a floor
    rather than a count — comparing against a floor would report a false shortfall, so the
    verdict says the walk was cut short instead of guessing.

    `reported` is the drawing's own entity count where the backend offers one
    (`Backend.info`). On the com backend it can exceed a listing, because it counts proxy
    entities a listing skips, so the two are reported side by side and never reconciled.

    A row that cannot be read is counted in `warnings` and treated as having drawn nothing,
    never raised: the rule the catalogue loader and the journal reader already follow.
    """
    warnings: list[str] = []
    applied = 0
    undone = 0
    expected = 0
    undone_entities = 0
    by_op: dict[str, int] = {}
    empty_steps: list[Any] = []

    for position, row in enumerate(steps, start=1):
        if not isinstance(row, dict):
            warnings.append(f"step {position} is not a row, so nothing was counted for it")
            continue
        try:
            count = int(row["entities"])
        except KeyError:
            # Absent is not zero. Counting it as zero would depress the expectation and
            # turn a correct drawing into a false "unaccounted", which for a check like
            # this is worse than saying nothing.
            warnings.append(f"step {position} reports no entity count, so none was counted")
            count = 0
        except (TypeError, ValueError):
            warnings.append(
                f"step {position} reports an unreadable entity count "
                f"{row.get('entities')!r}; it was counted as none"
            )
            count = 0
        if str(row.get("state", APPLIED)).strip().lower() in INACTIVE_STATES:
            undone += 1
            undone_entities += count
            continue
        applied += 1
        expected += count
        op = str(row.get("op", ""))
        by_op[op] = by_op.get(op, 0) + count
        if count == 0:
            # Informational only: a step that ensures a layer or saves a file legitimately
            # draws nothing, so this is a place to look rather than a fault.
            empty_steps.append(row.get("index", position))

    actual = len(entities)
    difference = actual - expected
    verdict: str
    ok: bool | None
    if truncated:
        verdict, ok = TRUNCATED, None
        warnings.append(
            f"the entity walk stopped at its limit, so the {actual} counted are a floor; "
            "call again with a larger limit"
        )
    elif applied == 0 and actual:
        # Nothing was drawn in this session's journal, so there is no claim to check. Calling
        # this "unaccounted" would cry wolf on every drawing opened from a file — the one case
        # where a read-back is most wanted — and `ok: false` would stop an automated loop.
        verdict, ok = NO_BASELINE, None
        warnings.append(
            f"this drawing holds {actual} entities and records no steps, so there is nothing "
            "to reconcile: it was drawn by another session, another tool, or before this "
            "journal existed"
        )
    elif difference == 0:
        verdict, ok = MATCHES, True
    elif difference > 0:
        verdict, ok = UNACCOUNTED, False
        warnings.append(
            f"{difference} entities are in the drawing and in no step — expected if the file "
            "was opened with entities already in it (drawing_open records no baseline), and a "
            "real drift if it was not"
        )
    else:
        verdict, ok = MISSING, False

    report: dict[str, Any] = {
        "ok": ok,
        "verdict": verdict,
        "steps": {"applied": applied, "undone": undone},
        "expected_entities": expected,
        "actual_entities": actual,
        "difference": difference,
        "undone_entities": undone_entities,
        "by_op": dict(sorted(by_op.items())),
        "empty_steps": empty_steps,
        "truncated": truncated,
        "warnings": warnings,
    }
    if reported is not None:
        report["reported_entities"] = reported
    return report
