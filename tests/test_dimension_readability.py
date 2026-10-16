"""A dimension's text always reads left-to-right.

An aligned dimension's text runs along p1 -> p2, so a span given right-to-left
(or top-to-bottom) renders the value mirrored — silently: no error, and critique
sees a perfectly ordinary dimension. A sandbox agent only caught it in a
screenshot. ops normalises the span before the backend sees it, negating the
offset with the swap so the dimension line lands exactly where the caller put it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import ops  # noqa: E402
from acadmcp.backends import make_backend  # noqa: E402


def test_a_reversed_span_is_normalised_and_the_side_preserved():
    p1, p2, offset = ops._readable_span((6000.0, 0.0), (0.0, 0.0), 1200.0)
    assert (p1, p2) == ((0.0, 0.0), (6000.0, 0.0))
    assert offset == -1200.0


def test_a_forward_span_is_untouched():
    assert ops._readable_span((0.0, 0.0), (6000.0, 0.0), 1200.0) == (
        (0.0, 0.0),
        (6000.0, 0.0),
        1200.0,
    )


def test_a_downward_span_flips_upward():
    p1, p2, offset = ops._readable_span((0.0, 6000.0), (0.0, 0.0), 800.0)
    assert (p1, p2) == ((0.0, 0.0), (0.0, 6000.0))
    assert offset == -800.0


def test_a_reversed_dimension_lands_where_the_caller_put_it():
    """east->west with +offset and west->east with -offset are the same dimension."""
    be = make_backend("dxf")
    ops.dimension_between(be, (6000.0, 0.0), (0.0, 0.0), 1200.0)
    ops.dimension_between(be, (0.0, 3000.0), (6000.0, 3000.0), -1200.0)

    dims = [e for e in be.msp if e.dxftype() == "DIMENSION"]
    assert len(dims) == 2
    # Both spans are normalised to left-to-right, so the extension points agree...
    for dim in dims:
        assert dim.dxf.defpoint2.x == pytest.approx(0.0)
        assert dim.dxf.defpoint3.x == pytest.approx(6000.0)
    # ...and the dimension lines sit the same side of their spans: below them.
    assert dims[0].dxf.defpoint.y == pytest.approx(-1200.0)
    assert dims[1].dxf.defpoint.y == pytest.approx(3000.0 - 1200.0)
