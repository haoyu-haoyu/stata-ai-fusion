"""Unit tests for Stata error detection and the run_command echo fix.

Both are pure-logic tests that do not require a Stata installation.
"""

from __future__ import annotations

from stata_ai_fusion.stata_session import ExecutionResult, _detect_error
from stata_ai_fusion.tools.run_command import handle


# ---------------------------------------------------------------------------
# _detect_error: anchored r(NNN); detection (no false positives)
# ---------------------------------------------------------------------------


def test_no_false_positive_on_code_in_text():
    """A return code merely mentioned in output text is NOT an error."""
    msg, code = _detect_error("this mentions r(198) in text")
    assert (msg, code) == (None, None)


def test_no_false_positive_on_comment():
    msg, code = _detect_error("* remember to check r(198) for syntax\n  price | 74")
    assert (msg, code) == (None, None)


def test_real_error_detected():
    msg, code = _detect_error("command foo is unrecognized\nr(199);")
    assert code == 199
    assert msg == "command foo is unrecognized"


def test_real_error_with_message_line():
    msg, code = _detect_error("no variables defined\nr(111);")
    assert code == 111
    assert msg == "no variables defined"


def test_duplicate_rc_line_batch_artifact():
    """Batch output can repeat the r(NNN); line; detect the first cleanly."""
    msg, code = _detect_error("command foo is unrecognized\nr(199);\n\n\nr(199);")
    assert code == 199
    assert msg == "command foo is unrecognized"


def test_leading_whitespace_before_rc():
    msg, code = _detect_error("    bad thing happened\n    r(111);")
    assert code == 111


def test_message_skips_command_echo():
    out = ". summarize nope\nvariable nope not found\nr(111);"
    msg, code = _detect_error(out)
    assert code == 111
    assert msg == "variable nope not found"  # the ". summarize nope" echo is skipped


def test_clean_output_no_error():
    out = ". summarize price\n    Variable | Obs Mean\n       price | 74  6165"
    assert _detect_error(out) == (None, None)


# ---------------------------------------------------------------------------
# _detect_error: anchored text fallbacks
# ---------------------------------------------------------------------------


def test_text_fallback_at_line_start():
    msg, code = _detect_error("no observations")
    assert code is None
    assert msg == "no observations"


def test_text_fallback_not_midsentence():
    """The same words mid-sentence must not trigger a false error."""
    assert _detect_error("there were no observations dropped today") == (None, None)
    assert _detect_error('display "type mismatch is a common error"') == (None, None)


# ---------------------------------------------------------------------------
# run_command: the broken `set output inform` injection is gone
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, code: str, timeout: int = 120) -> ExecutionResult:
        self.calls.append(code)
        return ExecutionResult(output="2", return_code=0)


class _FakeManager:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def get_or_create(self, session_id: str) -> _FakeSession:
        return self._session


async def test_run_command_never_injects_set_output_inform():
    """Even if a client still passes the removed `echo` arg, the code must run
    verbatim — no `set output inform` that would suppress results / poison the
    session."""
    sess = _FakeSession()
    await handle(_FakeManager(sess), {"code": "display 1+1", "echo": False})
    assert sess.calls == ["display 1+1"]
    assert all("set output inform" not in c for c in sess.calls)
