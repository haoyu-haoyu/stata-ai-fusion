"""Tests for the MCP resource helpers in ``server.py``.

These cover the skill knowledge-base accessors that previously returned
placeholder strings silently on missing config.  The fix is to emit
WARNING-level logs when files/directories are missing so operators see
the underlying cause in their server log, while keeping the user-facing
return strings backwards-compatible with the MCP resource-read contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from stata_ai_fusion import server as server_module


@pytest.fixture
def empty_skill_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect SKILL_DIR to an empty temp directory (no SKILL.md, no refs/)."""
    monkeypatch.setattr(server_module, "SKILL_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def skill_with_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """SKILL_DIR with a SKILL.md but no references directory."""
    (tmp_path / "SKILL.md").write_text("# Stata Skill\n\nHello.\n", encoding="utf-8")
    monkeypatch.setattr(server_module, "SKILL_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def skill_with_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """SKILL_DIR with SKILL.md and a references/ directory containing a doc."""
    (tmp_path / "SKILL.md").write_text("# Main\n", encoding="utf-8")
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "regression.md").write_text("# Regression\n\nUse ``regress``.\n", encoding="utf-8")
    (refs / "DataCleaning.md").write_text("# Data Cleaning\n", encoding="utf-8")
    monkeypatch.setattr(server_module, "SKILL_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# _read_skill_main
# ---------------------------------------------------------------------------


class TestReadSkillMain:
    """Behavior of :func:`server._read_skill_main`."""

    def test_returns_file_content_when_present(self, skill_with_main: Path) -> None:
        """Happy path: SKILL.md content is returned verbatim."""
        assert server_module._read_skill_main() == "# Stata Skill\n\nHello.\n"

    def test_logs_warning_when_missing(
        self, empty_skill_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing SKILL.md must emit a WARNING so operators see the config gap."""
        with caplog.at_level(logging.WARNING):
            result = server_module._read_skill_main()
        assert "(SKILL.md not found)" in result
        assert any("SKILL.md not found" in rec.message for rec in caplog.records)
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_logs_warning_on_ioerror(
        self, skill_with_main: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """If ``read_text`` raises, we log WARNING and return a usable fallback."""
        original_read = Path.read_text

        def failing_read(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "SKILL.md":
                raise PermissionError(13, "Permission denied")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", failing_read)

        with caplog.at_level(logging.WARNING):
            result = server_module._read_skill_main()
        assert "unreadable" in result
        assert any("Failed to read" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _list_reference_files
# ---------------------------------------------------------------------------


class TestListReferenceFiles:
    """Behavior of :func:`server._list_reference_files`."""

    def test_returns_sorted_list_when_present(self, skill_with_refs: Path) -> None:
        """Returns all .md files sorted alphabetically."""
        files = server_module._list_reference_files()
        assert [p.name for p in files] == ["DataCleaning.md", "regression.md"]

    def test_warns_when_refs_dir_missing(
        self, skill_with_main: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing references/ must emit a WARNING and return []."""
        with caplog.at_level(logging.WARNING):
            files = server_module._list_reference_files()
        assert files == []
        assert any("Reference directory missing" in rec.message for rec in caplog.records)

    def test_warns_on_glob_oserror(
        self, skill_with_refs: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """If scanning the references directory itself raises OSError
        (e.g. unreadable directory), log WARNING and return [] instead of
        letting the exception escape into the MCP protocol layer."""
        original_glob = Path.glob

        def failing_glob(self: Path, pattern: str, *args: object, **kwargs: object):
            if self.name == "references":
                raise PermissionError(13, "Permission denied")
            return original_glob(self, pattern, *args, **kwargs)

        monkeypatch.setattr(Path, "glob", failing_glob)

        with caplog.at_level(logging.WARNING):
            files = server_module._list_reference_files()
        assert files == []
        assert any(
            "Failed to scan reference directory" in rec.message for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# _read_reference
# ---------------------------------------------------------------------------


class TestReadReference:
    """Behavior of :func:`server._read_reference`."""

    def test_returns_content_for_exact_match(self, skill_with_refs: Path) -> None:
        content = server_module._read_reference("regression")
        assert content is not None
        assert "Use ``regress``" in content

    def test_returns_content_for_case_insensitive_match(self, skill_with_refs: Path) -> None:
        """Callers that pass a slightly different casing still resolve."""
        content = server_module._read_reference("datacleaning")
        assert content is not None
        assert "Data Cleaning" in content

    def test_returns_none_for_unknown_topic(self, skill_with_refs: Path) -> None:
        assert server_module._read_reference("no-such-topic") is None

    def test_returns_none_when_refs_dir_missing(
        self, skill_with_main: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No references/ dir → None AND a WARNING, so a direct client
        read of ``stata://skill/references/X`` also surfaces the config
        problem (not just a list-then-read sequence)."""
        with caplog.at_level(logging.WARNING):
            result = server_module._read_reference("whatever")
        assert result is None
        assert any(
            "Reference directory missing" in rec.message for rec in caplog.records
        )

    def test_succeeds_when_dir_not_listable_but_file_readable(
        self, skill_with_refs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Execute-only directories (listable=False, readable-by-known-name=True)
        must still resolve exact-topic reads.  Simulated by making glob() fail
        while leaving read_text() working."""
        def failing_glob(self: Path, pattern: str, *args: object, **kwargs: object):
            if self.name == "references":
                raise PermissionError(13, "Permission denied")
            return Path.__dict__["glob"].__wrapped__(self, pattern, *args, **kwargs) \
                if hasattr(Path.__dict__["glob"], "__wrapped__") else iter([])
        monkeypatch.setattr(Path, "glob", failing_glob)

        # Exact-file path still succeeds because we try the candidate
        # filename before invoking the list helper.
        content = server_module._read_reference("regression")
        assert content is not None
        assert "Use ``regress``" in content

    def test_empty_refs_dir_logs_debug_for_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """When references/ exists but is empty, a topic miss still
        emits a DEBUG log so operators who raise verbosity can see it,
        and no WARNING is emitted (empty != missing)."""
        (tmp_path / "SKILL.md").write_text("# Main\n", encoding="utf-8")
        (tmp_path / "references").mkdir()
        monkeypatch.setattr(server_module, "SKILL_DIR", tmp_path)

        with caplog.at_level(logging.DEBUG):
            result = server_module._read_reference("whatever")

        assert result is None
        assert any(
            "Reference topic not found" in rec.message and rec.levelno == logging.DEBUG
            for rec in caplog.records
        )
        # Empty directory is not the same as missing — must not emit WARNING.
        assert not any(
            "Reference directory missing" in rec.message for rec in caplog.records
        )

    def test_logs_warning_on_ioerror(
        self, skill_with_refs: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """File exists but read fails → WARNING log and None return."""
        def failing_read(self: Path, *args: object, **kwargs: object) -> str:
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr(Path, "read_text", failing_read)

        with caplog.at_level(logging.WARNING):
            result = server_module._read_reference("regression")
        assert result is None
        assert any("Failed to read reference" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# handle_read_resource (via register_resources)
# ---------------------------------------------------------------------------


class _StubServer:
    """Minimal stand-in for ``mcp.server.Server`` that just captures decorated handlers."""

    def __init__(self) -> None:
        self.list_handler = None
        self.read_handler = None

    def list_resources(self):
        def decorator(func):
            self.list_handler = func
            return func
        return decorator

    def read_resource(self):
        def decorator(func):
            self.read_handler = func
            return func
        return decorator


class TestHandleReadResource:
    """End-to-end behavior of the MCP resource read handler."""

    @pytest.fixture
    def handlers(self, skill_with_refs: Path) -> _StubServer:
        stub = _StubServer()
        server_module.register_resources(stub)  # type: ignore[arg-type]
        assert stub.read_handler is not None
        return stub

    async def test_main_resource_returns_skill_content(self, handlers: _StubServer) -> None:
        result = await handlers.read_handler("stata://skill/main")
        assert result.startswith("# Main")

    async def test_references_list_returns_topic_names(self, handlers: _StubServer) -> None:
        result = await handlers.read_handler("stata://skill/references")
        assert "regression" in result
        assert "DataCleaning" in result

    async def test_references_list_placeholder_when_empty(
        self, skill_with_main: Path
    ) -> None:
        stub = _StubServer()
        server_module.register_resources(stub)  # type: ignore[arg-type]
        result = await stub.read_handler("stata://skill/references")
        assert "(no reference files found)" in result

    async def test_specific_reference_found(self, handlers: _StubServer) -> None:
        result = await handlers.read_handler("stata://skill/references/regression")
        assert "Use ``regress``" in result

    async def test_specific_reference_not_found_returns_placeholder(
        self, handlers: _StubServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Topic miss → user-facing placeholder.  No WARNING-level noise for
        what is typically a user-input typo; _read_reference already records
        it at DEBUG level."""
        with caplog.at_level(logging.WARNING):
            result = await handlers.read_handler("stata://skill/references/no-such-topic")
        assert "(reference not found: no-such-topic)" in result
        # Must NOT escalate typical misses to WARNING — reserve that for
        # real packaging/config problems (missing directory, I/O errors).
        assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)

    async def test_unknown_uri_logs_error(
        self, handlers: _StubServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown URIs are protocol violations → ERROR log + descriptive string."""
        with caplog.at_level(logging.ERROR):
            result = await handlers.read_handler("stata://bogus/path")
        assert "(unknown resource: stata://bogus/path)" in result
        assert any(
            "Unknown MCP resource URI" in rec.message and rec.levelno == logging.ERROR
            for rec in caplog.records
        )
