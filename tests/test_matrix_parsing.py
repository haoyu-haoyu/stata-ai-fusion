"""Tests for _parse_matrix_output using real `matrix list` output (no Stata)."""

from __future__ import annotations

import json

import pytest

from stata_ai_fusion.result_extractor import _parse_matrix_output

# Captured verbatim from Stata 19 `matrix list`.

NARROW = """\
e(b)[1,3]
           mpg      weight       _cons
y1  -49.512221   1.7465592   1946.0687
"""

SYMMETRIC = """\
symmetric e(V)[3,3]
               mpg      weight       _cons
   mpg    7422.863
weight   44.601659   .41133468
 _cons  -292759.82  -2191.9032    12938766
"""

WRAPPED = """\
e(b)[1,9]
             mpg        weight        length          turn         trunk
y1    -106.52857     4.8527254     -80.74026    -307.76057     105.61283

        headroom  displacement    gear_ratio         _cons
y1    -791.71181     12.221453     2317.7772     12675.275
"""

WITH_MISSING = """\
r(C)[1,3]
        c1   c2   c3
r1    .   1.5   .a
"""


def test_narrow_matrix():
    m = _parse_matrix_output(NARROW)
    assert m is not None
    assert len(m) == 1 and len(m[0]) == 3
    assert m[0] == pytest.approx([-49.512221, 1.7465592, 1946.0687])


def test_symmetric_matrix_is_mirrored():
    m = _parse_matrix_output(SYMMETRIC)
    assert m is not None
    assert len(m) == 3 and all(len(r) == 3 for r in m)
    # Lower triangle reflected into the upper triangle.
    assert m[0] == pytest.approx([7422.863, 44.601659, -292759.82])
    assert m[1] == pytest.approx([44.601659, 0.41133468, -2191.9032])
    assert m[2] == pytest.approx([-292759.82, -2191.9032, 12938766])
    # Symmetry holds for every off-diagonal pair.
    for i in range(3):
        for j in range(3):
            assert m[i][j] == pytest.approx(m[j][i])


def test_wide_wrapped_row_vector_is_reassembled():
    m = _parse_matrix_output(WRAPPED)
    assert m is not None
    assert len(m) == 1 and len(m[0]) == 9  # 5 + 4 columns across two blocks
    assert m[0] == pytest.approx(
        [-106.52857, 4.8527254, -80.74026, -307.76057, 105.61283,
         -791.71181, 12.221453, 2317.7772, 12675.275]
    )


def test_missing_values_become_none_and_are_json_safe():
    m = _parse_matrix_output(WITH_MISSING)
    assert m == [[None, 1.5, None]]
    # Must be valid JSON (the old code used float('nan'), which is not).
    assert json.loads(json.dumps(m)) == [[None, 1.5, None]]


def test_unparseable_returns_none():
    assert _parse_matrix_output("no matrix here\njust text") is None
    assert _parse_matrix_output("") is None
