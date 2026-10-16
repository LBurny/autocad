"""Build the shipped template artefacts, and gate them against what is committed.

The templates live in ``src/acadmcp/data/templates.json`` as *specifications*; the files in
``templates/`` are what those specifications build, drawn by this package's own ``dxf``
backend — no AutoCAD, and nothing hand-drawn. Two gates keep the two honest:

* **The standard gate (D4).** A template names the standard it encodes, so the build runs the
  Phase-B conformance checker over the artefact it just drew and *fails* on any error-severity
  issue. A template that does not conform to the standard it claims is not a template.
* **The fingerprint gate.** ``--check`` rebuilds every artefact into a temporary directory and
  compares a geometry fingerprint with the committed file — ``(dxftype, layer, text, height,
  points)`` per entity, as a :class:`collections.Counter`.

The fingerprint is the whole point, and it is why bytes are never compared. A DXF embeds a
timestamp, random GUIDs and — occasionally, with hash randomisation — a re-ordered
``BLOCK_RECORD``, so the committed file is different bytes every time it is regenerated even
when nothing moved (the evaluation-fixture lesson, recorded in AGENTS.md). ``git status`` is
the same trap: it is always dirty. Geometry is what a reader actually receives, so geometry is
what this compares.

Usage::

    python scripts/build_templates.py             # build templates/*.dxf in place
    python scripts/build_templates.py --out DIR   # build somewhere else
    python scripts/build_templates.py --check     # rebuild to a temp dir and compare
    python scripts/build_templates.py --dwt        # also write .dwt twins (needs AutoCAD)
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from acadmcp import ops, sheet, standards, templates  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402

#: The `AcSaveAsType` constant for a 2018 template, for the optional `.dwt` twin.
#:
#: Read out of ``Autodesk.AutoCAD.Interop.Common.dll`` with a reflection-only load of the
#: enum — ``ac2018_Template == 66`` (and ``acNative == ac2018_dwg == 64``, so 2018 *is* this
#: release's native format). AGENTS.md's ``25`` is a DXF constant (``acR18_dxf``), *not* a
#: template one: the enum runs 24/25/26 for ``acR18`` and 64/65/66 for ``ac2018``, so the
#: template constant is not simply "the neighbour of the DXF one". Verified, not guessed.
DWT_FORMAT = 66


# ---------------------------------------------------------------------------
# the fingerprint
# ---------------------------------------------------------------------------


def _vertex(value: Any) -> tuple[float, float]:
    return (round(float(value[0]), 4), round(float(value[1]), 4))


def _entity_key(entity: Any) -> tuple:
    """One entity as a hashable geometry key: ``(dxftype, layer, text, height, points)``.

    Coordinates are rounded to 0.0001 mm — far below any drafting tolerance, and enough to
    absorb the float noise a save/load round trip can introduce so the comparison is about
    geometry rather than about the last bit of an IEEE double.
    """
    dxftype = str(entity.dxftype())
    layer = str(entity.dxf.get("layer", "0"))
    text = ""
    height: float | None = None
    points: tuple = ()

    if dxftype == "LINE":
        points = (_vertex(entity.dxf.start), _vertex(entity.dxf.end))
    elif dxftype == "LWPOLYLINE":
        points = tuple(_vertex(p) for p in entity.get_points("xy"))
    elif dxftype == "POLYLINE":
        points = tuple(_vertex(v.dxf.location) for v in entity.vertices)
    elif dxftype == "CIRCLE":
        points = (_vertex(entity.dxf.center), (round(float(entity.dxf.radius), 4), 0.0))
    elif dxftype == "ARC":
        points = (
            _vertex(entity.dxf.center),
            (round(float(entity.dxf.radius), 4), round(float(entity.dxf.start_angle), 4)),
            (round(float(entity.dxf.end_angle), 4), 0.0),
        )
    elif dxftype == "VIEWPORT":
        points = (
            _vertex(entity.dxf.center),
            (round(float(entity.dxf.width), 4), round(float(entity.dxf.height), 4)),
        )
    elif dxftype == "TEXT":
        text = str(entity.dxf.get("text", ""))
        height = round(float(entity.dxf.get("height", 0.0)), 4)
        points = (_vertex(entity.dxf.insert),)
    elif dxftype == "MTEXT":
        text = entity.plain_text()
        height = round(float(entity.dxf.get("char_height", 0.0)), 4)
        points = (_vertex(entity.dxf.insert),)
    elif dxftype == "INSERT":
        text = str(entity.dxf.get("name", ""))
        points = (_vertex(entity.dxf.insert),)

    return (dxftype, layer, text, height, points)


def fingerprint(path: str | Path) -> Counter:
    """The geometry of every entity in a DXF, as a Counter of :func:`_entity_key` tuples.

    Model and paperspace alike, and de-duplicated by handle in case a layout is reachable by
    two routes — a sheet's frame is drawing content, so a listing that skipped the layout would
    miss exactly the entities a template is mostly made of.
    """
    import ezdxf

    doc = ezdxf.readfile(str(path))
    counter: Counter = Counter()
    seen: set[str] = set()
    for layout in doc.layouts:
        for entity in layout:
            handle = str(entity.dxf.get("handle", ""))
            if handle and handle in seen:
                continue
            seen.add(handle)
            counter[_entity_key(entity)] += 1
    return counter


def _describe_drift(key: str, want: Counter, got: Counter) -> list[str]:
    """What differs between the committed fingerprint and the rebuilt one, line by line."""
    lines: list[str] = []
    for item in sorted(set(want) | set(got), key=repr):
        committed, rebuilt = want.get(item, 0), got.get(item, 0)
        if committed != rebuilt:
            lines.append(f"  {key}: {item!r} committed x{committed}, rebuilt x{rebuilt}")
    if not lines:
        lines.append(
            f"  {key}: counters differ in total only ({sum(want.values())} vs {sum(got.values())})"
        )
    return lines


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def title_block_fields(spec: templates.TemplateSpec) -> dict[str, str]:
    """The title block's fields, with the scale written in last.

    The 比例 field is added here rather than stored in the JSON, so a sheet always states its
    scale (GB/T 10609.1) and cannot be built without it by leaving a field out of the file.
    """
    fields = dict(spec.title_fields)
    fields["比例"] = spec.scale
    return fields


def snapshot_of(be: Any) -> dict[str, Any]:
    """The drawing as the conformance checker reads it, gathered off the backend.

    ``be.entities()`` carries no text height, so the texts are read from the journal's applied
    steps instead — every recorded ``text`` primitive carries its height, layer and content.
    The sheet comes from ``be.info()["sheet"]`` and the layer names in use from the entities,
    which is the whole of what the checker's snapshot is.
    """
    info = be.info()
    texts: list[dict[str, Any]] = []
    for step in be.journal.steps:
        if str(getattr(step, "state", "")) != "applied":
            continue
        for primitive in step.primitives:
            if not isinstance(primitive, dict) or primitive.get("kind") != "text":
                continue
            texts.append(
                {
                    "height": primitive.get("height"),
                    "layer": primitive.get("layer", ""),
                    "content": primitive.get("text", ""),
                    "paper": bool(primitive.get("paper")),
                }
            )
    layers_in_use = sorted({str(row.get("layer", "")) for row in be.entities() if row.get("layer")})
    return {
        "units": info.get("units"),
        "layers_in_use": layers_in_use,
        "texts": texts,
        "sheet": info.get("sheet") or {},
    }


def build_one(spec: templates.TemplateSpec, out_dir: str | Path) -> tuple[Any, list[dict]]:
    """Draw one template into ``out_dir/<key>.dxf`` and return ``(backend, error findings)``.

    The order is the order ``drawing_new`` applies: the standard's layers, then the page setup,
    then the sheet as one step. The conformance checker runs over the result before it is
    returned, so a build that breaks its own standard is reported by the caller rather than
    written out silently.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{spec.key}.dxf"

    be = make_backend("dxf", path=str(target), create=True)
    ops.setup_layers(be, standard=spec.standard)

    standard = standards.in_force(spec.standard)
    if standard is None:
        raise SystemExit(
            f"template '{spec.key}' names standard '{spec.standard}', which is not loaded; "
            f"this package ships: {', '.join(standards.current().keys())}"
        )
    # The standard whose layer table the artefact was just built with, recorded inside it the
    # way `drawing_new(template=...)` records it. The artefact is a file a user opens, and
    # opening it directly has to be checked against the standard it holds, not against
    # whatever this process happens to have in force. Measured missing: the shipped 50104 plan
    # carried that standard's own line weights (A-WALL 0.70, A-DIM 0.25) with an empty journal,
    # so `drawing_open` adopted the package's gbt50001 for it and a save then recorded gbt50001
    # as the standard the drawing was drawn under.
    be.journal.note_meta(standard=standards.identity(standard))

    paper = sheet.resolve_paper(spec.paper, spec.orientation)
    scale = sheet.resolve_scale(spec.scale)
    be.set_page_setup(paper, scale)
    with be.step(
        "drawing_sheet", label=spec.label, params={"paper": spec.paper, "scale": spec.scale}
    ):
        ops.draw_sheet(be, paper, scale, title_block_fields(spec))
    be.save(str(target))

    errors = [
        issue
        for issue in standards.check(standard, snapshot_of(be))
        if issue["severity"] == "error"
    ]
    return be, errors


def _shipped() -> templates.Templates:
    """The package's own templates only — never a project's, which the working directory may
    name through ``acadmcp.json``: the build and its fingerprint gate cover exactly what ships.
    """
    return templates.load()


def build_all(out_dir: str | Path) -> list[dict[str, Any]]:
    """Build every shipped template into ``out_dir``, with its conformance errors."""
    report: list[dict[str, Any]] = []
    for spec in _shipped().specs():
        _be, errors = build_one(spec, out_dir)
        report.append(
            {"key": spec.key, "path": str(Path(out_dir) / f"{spec.key}.dxf"), "errors": errors}
        )
    return report


def _report_errors(report: list[dict[str, Any]]) -> bool:
    """Print any error findings; True when something failed the gate."""
    failed = False
    for row in report:
        for issue in row["errors"]:
            failed = True
            print(f"gate failed for {row['key']}: {issue['rule']} — {issue['detail']}")
    return failed


# ---------------------------------------------------------------------------
# the modes
# ---------------------------------------------------------------------------


def run_check() -> int:
    """Rebuild to a temporary directory and compare fingerprints with what is committed."""
    with tempfile.TemporaryDirectory(prefix="acadmcp-templates-") as tmp:
        report = build_all(tmp)
        if _report_errors(report):
            return 1

        drift: list[str] = []
        for row in report:
            spec = _shipped().resolve(row["key"])
            committed = ROOT / spec.artefact
            if not committed.is_file():
                drift.append(f"  {spec.key}: {spec.artefact} is not committed")
                continue
            want = fingerprint(committed)
            got = fingerprint(row["path"])
            if want != got:
                drift.extend(_describe_drift(spec.key, want, got))

        if drift:
            print("templates have drifted from their specification:")
            print("\n".join(drift))
            return 1

        print(f"ok: {len(report)} templates rebuild identically")
        return 0


def run_dwt() -> int:
    """Write a ``.dwt`` twin beside each artefact, or skip cleanly when AutoCAD is not there.

    The optional half of D4. It needs live AutoCAD; without it the flag prints ``skipped`` and
    exits 0, so the same command works on a machine that has none.
    """
    try:
        be = make_backend("com", new_document=True)
    except Exception as exc:  # noqa: BLE001 - any reason AutoCAD is not there is "skipped"
        print(f"skipped: no AutoCAD ({exc})")
        return 0

    from acadmcp.backends.com import _call

    written: list[str] = []
    for spec in _shipped().specs():
        source = ROOT / spec.artefact
        if not source.is_file():
            print(f"skipped {spec.key}: {spec.artefact} is not built yet")
            continue
        target = source.with_suffix(".dwt")
        # A SaveAs over an existing file can raise a modal overwrite prompt, which no API can
        # dismiss off-thread — so the old twin goes first.
        target.unlink(missing_ok=True)
        # Each lambda binds its argument by default: `_call` runs it on the COM thread, and a
        # closure reading the loop variables would otherwise be the classic late-binding bug.
        doc = _call(lambda source=source: be.app.Documents.Open(str(source)))
        try:
            _call(lambda doc=doc, target=target: doc.SaveAs(str(target), DWT_FORMAT))
        finally:
            _call(lambda doc=doc: doc.Close(False))
        written.append(str(target))
        print(f"wrote {target}")

    if not written:
        print("skipped: no artefact to convert")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and gate the shipped templates.")
    parser.add_argument(
        "--out", default="", help="where to write the artefacts (default templates/)"
    )
    parser.add_argument("--check", action="store_true", help="rebuild and compare fingerprints")
    parser.add_argument("--dwt", action="store_true", help="also write .dwt twins (needs AutoCAD)")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()

    out_dir = Path(args.out) if args.out else ROOT / "templates"
    report = build_all(out_dir)
    if _report_errors(report):
        return 1
    for row in report:
        print(f"built {row['path']}")

    if args.dwt:
        return run_dwt()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
