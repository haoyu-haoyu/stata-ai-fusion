"""Security/validation tests for ResultExtractor (no Stata required).

Stored-result names reach get_scalar/get_matrix/get_macro and are interpolated
into executed Stata code, so a crafted name must be rejected before execution.
"""

from __future__ import annotations

import pytest

from stata_ai_fusion.result_extractor import ResultExtractor
from stata_ai_fusion.stata_session import ExecutionResult


def test_validate_result_name_accepts_identifiers():
    for name in ("N", "mean", "r2", "_cons", "depvar", "F", "df_r", "rmse"):
        assert ResultExtractor._validate_result_name(name) == name
    # surrounding whitespace is trimmed
    assert ResultExtractor._validate_result_name("  N  ") == "N"


@pytest.mark.parametrize(
    "bad",
    [
        "N)",
        'N)\nshell echo pwned > /tmp/x\n*',
        "N) ssc install foo",
        "a b",
        "",
        "1abc",
        "b[1,2]",
        "$macro",
        "`local'",
        'N";display "x',
    ],
)
def test_validate_result_name_rejects_injection(bad: str):
    with pytest.raises(ValueError):
        ResultExtractor._validate_result_name(bad)


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, code: str, timeout: int = 120) -> ExecutionResult:
        self.calls.append(code)
        return ExecutionResult(output="74", return_code=0)


async def test_get_scalar_rejects_bad_name_without_executing():
    sess = _RecordingSession()
    ext = ResultExtractor(sess)
    with pytest.raises(ValueError):
        await ext.get_scalar("N)\nshell evil", "e")
    assert sess.calls == []  # never reached Stata


async def test_get_matrix_rejects_bad_name_without_executing():
    sess = _RecordingSession()
    ext = ResultExtractor(sess)
    with pytest.raises(ValueError):
        await ext.get_matrix("b)\nshell evil", "e")
    assert sess.calls == []


async def test_get_macro_rejects_bad_name_without_executing():
    sess = _RecordingSession()
    ext = ResultExtractor(sess)
    with pytest.raises(ValueError):
        await ext.get_macro("depvar)\nshell evil", "e")
    assert sess.calls == []


async def test_get_scalar_valid_name_executes_expected_code():
    sess = _RecordingSession()
    ext = ResultExtractor(sess)
    await ext.get_scalar("N", "r")
    assert sess.calls == ["display r(N)"]
