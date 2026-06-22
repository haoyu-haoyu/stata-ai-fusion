"""Tests for the search_log tool, incl. the ReDoS timeout guard (no Stata)."""

from __future__ import annotations

import time

from stata_ai_fusion.tools.search_log import handle


class _FakeSession:
    def __init__(self, log_text: str) -> None:
        self._log = log_text

    def get_log(self) -> str:
        return self._log


class _FakeManager:
    def __init__(self, session: _FakeSession | None) -> None:
        self._session = session

    async def get_session(self, session_id: str) -> _FakeSession | None:
        return self._session


def _text(result) -> str:
    return result[0].text


async def test_plain_text_search_finds_match():
    mgr = _FakeManager(_FakeSession("alpha\nbeta\ngamma\n"))
    out = _text(await handle(mgr, {"query": "beta"}))
    assert "1 match" in out
    assert "beta" in out


async def test_regex_search_finds_match():
    mgr = _FakeManager(_FakeSession("price = 6165\nmpg = 21\n"))
    out = _text(await handle(mgr, {"query": r"\w+ = \d+", "regex": True}))
    assert "match" in out.lower()
    assert "price = 6165" in out


async def test_query_too_long_rejected():
    mgr = _FakeManager(_FakeSession("anything\n"))
    out = _text(await handle(mgr, {"query": "a" * 1001}))
    assert "too long" in out.lower()


async def test_negative_context_lines_clamped():
    mgr = _FakeManager(_FakeSession("l1\nl2\nMATCH\nl4\nl5\n"))
    # A negative context must not produce an empty/invalid range or crash.
    out = _text(await handle(mgr, {"query": "MATCH", "context_lines": -5}))
    assert "1 match" in out
    assert "MATCH" in out


async def test_catastrophic_regex_times_out(monkeypatch):
    """A ReDoS pattern must time out gracefully instead of blocking forever."""
    monkeypatch.setattr(
        "stata_ai_fusion.tools.search_log._SEARCH_TIMEOUT", 0.2
    )
    # `(a+)+$` against a non-matching tail backtracks exponentially.
    mgr = _FakeManager(_FakeSession("a" * 24 + "!\n"))
    start = time.monotonic()
    out = _text(await handle(mgr, {"query": r"(a+)+$", "regex": True}))
    elapsed = time.monotonic() - start
    assert "timed out" in out.lower()
    # Returned at ~the timeout, NOT after the full (multi-second) backtrack.
    assert elapsed < 2.0


async def test_no_session():
    out = _text(await handle(_FakeManager(None), {"query": "x"}))
    assert "no active session" in out.lower()
