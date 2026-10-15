"""Replaying a journal onto another backend: the document state it has to rebuild.

A journal records *primitives*, not document state. Two things a drawing depends on are
document state and are therefore never journalled — a sheet's page setup, and a captured
element's block definition — so a replay onto a fresh drawing has to rebuild them from what
the step does carry, or the frame has nowhere to land and the INSERT has no block to
reference. Both bugs below were exactly that: the step recorded the frame/insert, and the
half the applier needed was missing on the target.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import capture_ops, catalog, ops, server, settings  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402
from acadmcp.catalog import Catalog  # noqa: E402

KEY = "replay_door"
BLOCK = "ACADMCP_REPLAY_DOOR"


@pytest.fixture()
def session():
    """A fresh dxf drawing behind the tool surface, dropped afterwards."""
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


@pytest.fixture(autouse=True)
def _no_leaked_drawing():
    """The tool-surface tests here reset `_state` themselves; drop the drawing afterwards so
    a test that replaced it cannot leak into the next one."""
    yield
    server._state.backend = None


# ---------------------------------------------------------------------------
# F16: a sheet is a page setup *and* a frame; only the frame is journalled
# ---------------------------------------------------------------------------


def test_replaying_a_sheeted_drawing_rebuilds_the_page_setup(session, tmp_path):
    """Replay a sheeted drawing dxf->dxf: the page setup is document state, so it is not in
    the journal, and without rebuilding it first the frame had nowhere to land — the old
    reply was `replayed: 1` (the line only) and the sheet step in `failed`."""
    server.draw_line(p1=[0, 0], p2=[30000, 0])
    server.drawing_sheet(paper="A3", scale="1:100", title="图")

    target = str(tmp_path / "replayed.dxf")
    report = server.drawing_replay(backend="dxf", path=target)
    assert report["replayed"] == 2 and report["failed"] == [], "the sheet step must apply"

    import ezdxf

    layout = ezdxf.readfile(target).layouts.get("A3")
    assert layout is not None, "the page setup renamed the layout to its paper, as before"
    frame = [e for e in layout if e.dxf.get("layer") == "A-SHEET"]
    assert len(frame) == 12, "the frame and title block landed in paperspace"
    assert len([e for e in layout if e.dxftype() == "VIEWPORT"]) == 1


def test_a_replayed_sheet_reopens_with_its_scale_and_verifies(session, tmp_path):
    server.draw_line(p1=[0, 0], p2=[30000, 0])
    server.drawing_sheet(paper="A3", scale="1:200", title="图")
    target = str(tmp_path / "replayed.dxf")
    server.drawing_replay(backend="dxf", path=target)

    reopened = server.drawing_open(target)
    assert reopened["ok"] is True
    assert reopened["sheet"]["paper"] == "A3" and reopened["sheet"]["scale"] == "1:200"
    verdict = server.drawing_verify()
    assert verdict["verdict"] == "matches"
    assert verdict["expected_entities"] == verdict["actual_entities"] > 0


def test_a_replayed_sheet_plots_without_a_phantom(session, tmp_path):
    """The rebuilt page setup is a real one, so the plot works — and a drawing replayed with
    no sheet still refuses, which is the other half of the phantom-sheet fix."""
    server.drawing_sheet(paper="A3", scale="1:100")
    target = str(tmp_path / "replayed.dxf")
    server.drawing_replay(backend="dxf", path=target)

    report = server.drawing_plot_pdf(str(tmp_path / "replayed.pdf"))
    assert report["verified"] is True
    assert report["paper_mm"] == pytest.approx([420.0, 297.0], abs=0.05)


def test_replaying_a_sheetless_drawing_is_not_given_one(tmp_path):
    """The other direction: a plain drawing replays plain — no phantom A3 from the round trip."""
    source = make_backend("dxf")
    with source.step("draw_line"):
        source.line((0, 0), (1000, 0))

    target = make_backend("dxf")
    target.replay_step(source.journal.steps[0])
    target.save(str(tmp_path / "plain.dxf"))

    reopened = make_backend("dxf", path=str(tmp_path / "plain.dxf"))
    assert reopened.sheet == {} and reopened._sheet_layout is None


# ---------------------------------------------------------------------------
# F17: a captured definition is document state; an INSERT just references it
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """An empty working directory and a catalogue of its own, the test_publish pattern."""
    monkeypatch.chdir(tmp_path)
    settings.reset()
    catalog.use(Catalog())
    yield
    catalog.reset()
    settings.reset()


def _capture_door(source):
    """A wall run and a door in it, captured as one published element."""
    with source.step("draw_wall"):
        ops.draw_wall_run(source, [(0, 0), (6000, 0)], 200)
    with source.step("draw_door"):
        ops.draw_door(source, (1000, 0), 900, 200)
    with source.step("element_capture", params={"key": KEY}):
        return capture_ops.capture(source, steps=[1, 2], key=KEY, layer="A-DOOR")


def _block_geometry(block) -> Counter:
    """A block definition's entities as a Counter, as test_publish compares two drawings."""
    rows = []
    for entity in block:
        points = []
        for key in ("start", "end", "center", "insert"):
            if entity.dxf.hasattr(key):
                value = entity.dxf.get(key)
                points.append((round(float(value[0]), 1), round(float(value[1]), 1)))
        rows.append((entity.dxftype(), entity.dxf.get("layer", "0"), tuple(points)))
    return Counter(rows)


def test_replaying_a_placed_published_element_brings_its_definition(tmp_path):
    """The fragment import never happened on the replay target: the step records an INSERT,
    but the block it references is document state, so `insert_captured`'s import was never
    reached and the replay refused with "no such block definition"."""
    source = make_backend("dxf")
    _capture_door(source)
    entry = catalog.current().get(KEY)
    fragment = {**source.find_block_primitives(BLOCK), "fingerprint": entry.origin["fingerprint"]}
    published = catalog.current().publish_to(str(tmp_path / "elements.json"), entry, fragment)

    # A fresh drawing places the published element by key: the definition is imported from
    # the fragment (document state), and only the INSERT is journalled.
    catalog.use(Catalog.load([published["file"]]))
    server._state.reset(make_backend("dxf"), "dxf")
    placed = server.place_element(type=KEY, at=[1000, 1000], tag=False)
    assert placed["ok"] is True

    target = str(tmp_path / "replayed.dxf")
    report = server.drawing_replay(backend="dxf", path=target)
    assert report["replayed"] == 1 and report["failed"] == [], "the INSERT must apply"

    reopened = make_backend("dxf", path=target)
    assert BLOCK in reopened.doc.blocks, "the replay target resolved the published fragment"
    inserts = [e for e in reopened.msp if e.dxftype() == "INSERT"]
    assert [e.dxf.name for e in inserts] == [BLOCK]
    assert _block_geometry(reopened.doc.blocks.get(BLOCK)) == _block_geometry(
        source.doc.blocks.get(BLOCK)
    ), "the geometry that landed on replay is the geometry drawing A captured"


def test_the_replayed_insert_reports_where_it_landed(tmp_path):
    """And the read-only surface can confirm it: an INSERT row now carries `at`."""
    source = make_backend("dxf")
    _capture_door(source)
    entry = catalog.current().get(KEY)
    fragment = {**source.find_block_primitives(BLOCK), "fingerprint": entry.origin["fingerprint"]}
    published = catalog.current().publish_to(str(tmp_path / "elements.json"), entry, fragment)

    catalog.use(Catalog.load([published["file"]]))
    server._state.reset(make_backend("dxf"), "dxf")
    server.place_element(type=KEY, at=[1234, 5678], rotation=15.0, tag=False)

    target = str(tmp_path / "replayed.dxf")
    server.drawing_replay(backend="dxf", path=target)
    server.drawing_open(target)

    rows = [r for r in server.listing_entities()["entities"] if r["type"] == "INSERT"]
    assert len(rows) == 1
    assert rows[0]["at"] == [1234.0, 5678.0]
    assert rows[0]["rotation"] == 15.0
    assert server.drawing_verify()["verdict"] == "matches"


def test_a_placement_the_catalogue_cannot_resolve_is_still_reported(tmp_path):
    """The fragment import is an attempt, not a licence to drop: an INSERT whose block the
    catalogue cannot resolve is refused and reported in `failed`, exactly as before."""
    source = make_backend("dxf")
    source.doc.blocks.new("NEVER_PUBLISHED")  # a definition local to the source drawing
    with source.step("place_element"):
        source.insert("NEVER_PUBLISHED", (0, 0))

    server._state.reset(source, "dxf")
    report = server.drawing_replay(backend="dxf", path=str(tmp_path / "replayed.dxf"))
    assert report["replayed"] == 0
    assert report["failed"][0]["op"] == "place_element"
    assert "no such block definition" in report["failed"][0]["reason"]
