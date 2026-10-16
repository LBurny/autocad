"""A placement step records what it drew, so a replay can reconstruct it.

`place_element` (single, run and grid modes) records its step params for the journal, and
rotation, scale and tag were dropped from that record — so `catalog_suggest` would group two
placements differing only in rotation, and a replay could not rebuild the placement. This
pins the recorded params; the behaviour is otherwise unchanged.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_dxf():
    server._state.reset(make_backend("dxf"), "dxf")
    yield
    server._state.backend = None


def _params(index=0):
    return server._state.backend.step_rows()[index]["params"]


def test_place_element_records_rotation_scale_and_tag():
    """A door's parametric twin draws at real size (a non-1.0 scale is refused), so `scale`
    is recorded at its default — the regression being pinned is that the *keys* travel at all,
    so a replay can rebuild the placement and suggest can tell two rotations apart."""
    server.place_element(type="door", at=[0, 0], rotation=90.0, tag=False)

    params = _params()
    assert params["rotation"] == 90.0
    assert params["scale"] == 1.0
    assert params["tag"] is False


def test_place_element_records_the_defaults_too():
    server.place_element(type="door", at=[0, 0], tag=False)

    params = _params()
    assert params["rotation"] == 0.0
    assert params["scale"] == 1.0
    assert params["tag"] is False


def test_place_along_records_rotation_and_tag():
    server.place_element("column", start=[0, 0], end=[6000, 0], count=3, rotation=45.0, tag=False)

    params = _params()
    assert params["rotation"] == 45.0
    assert params["tag"] is False


def test_place_along_records_a_derived_rotation_as_none():
    """`rotation=None` means "derive from the run direction", which is a different fact from
    an explicit 0.0 — so the record keeps the None rather than a number nothing measured."""
    server.place_element("column", start=[0, 0], end=[6000, 0], count=3, tag=False)

    assert _params()["rotation"] is None


def test_place_grid_records_rotation_and_tag():
    server.place_element("column", xs=[0, 6000], ys=[0], rotation=30.0, tag=False)

    params = _params()
    assert params["rotation"] == 30.0
    assert params["tag"] is False
