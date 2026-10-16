"""Output schemas for the high-frequency read-only tools.

The SDK derives each tool's published ``outputSchema`` from its return
annotation: a TypedDict is used directly as the schema, and the wire's
``structuredContent`` is the *validated* copy — pydantic drops keys a TypedDict
does not declare, silently. So every key a tool can return is declared here,
and ``tests/test_output_schemas.py`` gates the drift: it calls each tool
through the tool manager (the same path the wire takes) and fails on a returned
key no TypedDict names.

Typing rule: precise where the shape is stable across backends, ``Any`` where
it is not — the plugin's ``layers`` is an int where dxf's is a list of names,
and com reports ``entities: None`` on a lost link. Backend-leaky nested
containers are ``dict[str, Any]`` with their stable inner keys stated in a
comment.

Every class is ``total=False``: the read tools report unions of what the
active backend can tell, so no single key is guaranteed. Annotations only; no
CAD imports, and none from this package either.
"""

from typing import Any, TypedDict

__all__ = [
    "CheckReply",
    "CheckStandardReply",
    "CritiqueReply",
    "ElementTypesReply",
    "EntitiesReply",
    "HistoryReply",
    "StatusReply",
    "SuggestReply",
    "SummaryReply",
    "TemplateListReply",
    "VerifyReply",
]


class StatusReply(TypedDict, total=False):
    """``drawing_status``: the backend's own ``info()`` plus the tool's additions.

    The first block is the union of the three backends' ``info()`` dicts; which
    of them are present is a property of the backend, not of the call.
    """

    backend: str
    path: str | None  # dxf and plugin; None on a dxf drawing never saved
    units: str
    insunits: int  # plugin
    entities: int | None  # None on a lost com link, beside `problem`
    layers: Any  # list[str] on dxf, a count (int) on the plugin, absent on com
    ops: int  # dxf and com
    document: str | None  # com and plugin; None on a lost com link
    problem: str  # com, only when the link could not be read
    journal: dict[str, Any]  # {steps: int, applied: int}; the plugin's adds its own
    # Despite the name a message, not a count: "" when the journal read clean,
    # otherwise why it was ignored.
    journal_unreadable: str  # dxf
    sidecar: str | None  # com
    sidecar_unreadable: int  # com
    # Added by the tool itself, on every backend:
    catalog: dict[str, Any]  # {count, digest, library, skipped}
    sheet: dict[str, Any]  # {paper, scale, orientation, layout, ...}; {} until drawing_sheet
    standard: dict[str, Any]  # standards.describe(): {count, in_force, skipped, standards}
    drawing_catalog: dict[str, Any]  # recording backends: {catalog, loaded, warning?}


class HistoryReply(TypedDict, total=False):
    """``drawing_history``. Step rows carry index/op/label/params/state, plus
    entities/handles/primitives depending on the backend."""

    ok: bool
    backend: str
    rendered: str
    steps: list[dict[str, Any]]


class SummaryReply(TypedDict, total=False):
    """``drawing_summary`` — summary.summarise's shape, plus ok."""

    ok: bool
    steps: dict[str, Any]  # {total, applied, undone, next_index}
    rooms: list[dict[str, Any]]  # {name, area_m2, perimeter_m, step, ...}
    openings: list[dict[str, Any]]  # {kind, width, tag, step, ...}
    elements: list[dict[str, Any]]  # {type, variant, tag, step, ...}
    structure: dict[str, int]
    annotation: dict[str, int]
    layers: list[str]
    catalog: dict[str, Any] | None  # {count, digest, library}, or None unrecorded
    warnings: list[str]


class VerifyReply(TypedDict, total=False):
    """The ``verify`` lens of ``drawing_check`` — verify.reconcile's shape, plus backend.

    `ok` is None, not missing, for the two verdicts that are neither pass nor
    fail (walk_truncated, no_baseline): a bool there would force a false one.
    """

    ok: bool | None
    verdict: str  # matches | unaccounted | missing | walk_truncated | no_baseline
    steps: dict[str, Any]  # {applied, undone}
    expected_entities: int
    actual_entities: int
    difference: int
    undone_entities: int
    by_op: dict[str, int]
    empty_steps: list[Any]
    truncated: bool
    warnings: list[str]
    reported_entities: int  # the drawing's own count, where a backend offers one
    backend: str  # added by the tool


class CritiqueReply(TypedDict, total=False):
    """The ``critique`` lens of ``drawing_check`` — critique.run's shape, plus ok."""

    ok: bool
    issues: list[dict[str, Any]]  # {check, severity, message, step?, ...}
    counts: dict[str, int]  # {error, warning, info}
    rows_checked: int
    unreadable: int
    focus: str


class CheckStandardReply(TypedDict, total=False):
    """The ``standard`` lens of ``drawing_check``."""

    ok: bool
    standard: str
    label: str
    digest: str
    issues: list[dict[str, Any]]  # {rule, severity, detail, entities}
    counts: dict[str, int]  # {error, warning}


class CheckReply(TypedDict, total=False):
    """``drawing_check`` — one envelope; the checks that ran, keyed by name."""

    ok: bool
    check: str  # verify | critique | standard | all — what was asked for
    checks: dict[str, Any]  # "verify" -> VerifyReply, "critique" -> CritiqueReply,
    # "standard" -> CheckStandardReply
    counts: dict[str, int]  # error/warning/info totals across the checks that ran


class EntitiesReply(TypedDict, total=False):
    """``listing_entities``. No `ok`: the pagination fields are the answer."""

    count: int
    offset: int
    has_more: bool
    next_offset: int | None
    total_count: int | None  # None while a filtered page has not reached the end
    entities: list[dict[str, Any]]  # {type, layer, ...}; text/inserts carry more
    layer_known: bool  # only when a `layer` filter was passed


class ElementTypesReply(TypedDict, total=False):
    """``element_types``."""

    ok: bool
    count: int
    skipped: list[str]
    library: dict[str, str]  # catalogue file path -> library directory it declared
    categories: dict[str, list[dict[str, Any]]]  # category -> Element.describe() rows
    problems: list[str]  # with check_library: every entry whose block is missing
    matches_library: bool
    placement_note: str  # when the library could not be checked at all


class TemplateListReply(TypedDict, total=False):
    """``template_list``. Rows carry key/label/standard/paper/orientation/scale/
    title_fields/artefact."""

    ok: bool
    templates: list[dict[str, Any]]


class SuggestReply(TypedDict, total=False):
    """``catalog_suggest`` — suggest.suggestions' shape, plus ok."""

    ok: bool
    suggestions: list[dict[str, Any]]  # {op, count, params, steps, hint}
    skipped: int
