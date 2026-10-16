"""The draw_room tool exposes `label_offset` — ops has had it, the tool did not.

A room label is pinned to the centroid, and a stair or a piece of equipment can sit
exactly there; the cc-office sandbox drew its stair label through the treads for want
of this parameter. What is tested here is the plumbing: the offset the caller gives
reaches the label, on both the walled path and the walls=False re-label path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import catalog, server  # noqa: E402

RING = [[0, 0], [5000, 0], [5000, 4000], [0, 4000]]


@pytest.fixture(autouse=True)
def fresh_session():
    catalog.reset()
    server._state.reset(server.make_backend("dxf"), "dxf")
    yield
    server._state.backend = None
    catalog.reset()


def _name_text_y() -> float:
    texts = [
        e
        for e in server._state.backend.msp
        if e.dxftype() == "TEXT" and e.dxf.layer == "A-ROOM-NAME"
    ]
    assert len(texts) == 1
    return texts[0].dxf.insert[1]


def test_the_room_label_moves_by_label_offset():
    server.draw_room(boundary=RING, name="楼梯间", label_offset=1500)
    # The centroid of the 4000-deep room sits at y=2000; the label rides above it.
    assert _name_text_y() == pytest.approx(3500.0)


def test_the_default_keeps_the_label_on_the_centroid():
    server.draw_room(boundary=RING, name="楼梯间")
    assert _name_text_y() == pytest.approx(2000.0)


def test_label_offset_reaches_the_walls_free_path():
    server.draw_room(boundary=RING, name="楼梯间", walls=False, label_offset=1500)
    assert _name_text_y() == pytest.approx(3500.0)
