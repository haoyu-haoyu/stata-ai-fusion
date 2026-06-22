"""Unit tests for the install_package tool (no Stata required).

Uses a fake session that records the Stata code it is asked to run, so the
handler's check/install logic is verified deterministically.
"""

from __future__ import annotations

import pytest

from stata_ai_fusion.stata_session import ExecutionResult
from stata_ai_fusion.tools.install_package import _parse_check_rc, handle


def test_parse_check_rc():
    assert _parse_check_rc("__stata_pkg_rc=0") == 0
    assert _parse_check_rc("__stata_pkg_rc=111") == 111
    # Tolerates surrounding output / command echoes.
    assert _parse_check_rc('. display "__stata_pkg_rc=" _rc\n__stata_pkg_rc=0\n') == 0
    assert _parse_check_rc("no marker here") is None
    assert _parse_check_rc("") is None
    assert _parse_check_rc(None) is None


class _FakeSession:
    """Records executed code; returns the configured `which` rc, success otherwise."""

    def __init__(self, which_rc: int | None) -> None:
        self._which_rc = which_rc
        self.calls: list[str] = []

    async def execute(self, code: str, timeout: int = 120) -> ExecutionResult:
        self.calls.append(code)
        if "capture which" in code:
            out = "" if self._which_rc is None else f"__stata_pkg_rc={self._which_rc}"
            return ExecutionResult(output=out, return_code=0)
        # ssc/net install: pretend it succeeded.
        return ExecutionResult(output="installation complete", return_code=0)

    def ran_install(self) -> bool:
        return any("install" in c for c in self.calls)


class _FakeManager:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def get_or_create(self, session_id: str) -> _FakeSession:
        return self._session


async def test_already_installed_skips_install():
    sess = _FakeSession(which_rc=0)
    result = await handle(_FakeManager(sess), {"package": "estout"})
    assert "already installed" in result[0].text.lower()
    assert not sess.ran_install()  # must NOT attempt install when present


async def test_missing_package_triggers_install():
    """Regression: a missing package (rc=111) must actually be installed,
    not reported as 'already installed'."""
    sess = _FakeSession(which_rc=111)
    result = await handle(_FakeManager(sess), {"package": "estout"})
    assert "already installed" not in result[0].text.lower()
    assert sess.ran_install()
    assert any("ssc install estout" in c for c in sess.calls)


async def test_unknown_marker_falls_through_to_install():
    """If the rc marker is missing (unexpected), be safe and attempt install."""
    sess = _FakeSession(which_rc=None)
    await handle(_FakeManager(sess), {"package": "estout"})
    assert sess.ran_install()


async def test_invalid_package_name_rejected():
    sess = _FakeSession(which_rc=111)
    result = await handle(_FakeManager(sess), {"package": "evil; shell rm -rf /"})
    assert "only alphanumerics" in result[0].text.lower()
    assert sess.calls == []  # rejected before touching Stata


@pytest.mark.parametrize("empty", ["", "   "])
async def test_empty_package_name_rejected(empty: str):
    sess = _FakeSession(which_rc=0)
    result = await handle(_FakeManager(sess), {"package": empty})
    assert "no package name" in result[0].text.lower()
    assert sess.calls == []


async def test_from_url_builds_net_install():
    sess = _FakeSession(which_rc=111)
    await handle(
        _FakeManager(sess),
        {"package": "mypkg", "from_url": "https://example.com/repo/"},
    )
    assert any(
        'net install mypkg, from("https://example.com/repo/") replace' in c
        for c in sess.calls
    )


@pytest.mark.parametrize(
    "bad_url",
    ['http://x" ; shell evil', "http://x$(whoami)", "https://a b/c", "not_a_url"],
)
async def test_invalid_from_url_rejected(bad_url: str):
    sess = _FakeSession(which_rc=111)
    result = await handle(
        _FakeManager(sess), {"package": "mypkg", "from_url": bad_url}
    )
    assert "from_url must be" in result[0].text.lower()
    assert not sess.ran_install()  # never reached the install step


async def test_from_ssc_false_without_url_errors():
    sess = _FakeSession(which_rc=111)
    result = await handle(
        _FakeManager(sess), {"package": "mypkg", "from_ssc": False}
    )
    text = result[0].text.lower()
    assert "from_url" in text or "from_ssc" in text
    assert not sess.ran_install()
