"""Integration tests for Stata AI Fusion.

Tests in this module interact with a real Stata installation.  They are
automatically skipped when Stata is not available on the machine.
"""

from __future__ import annotations

import pytest

from stata_ai_fusion.stata_discovery import discover_stata_or_none
from stata_ai_fusion.stata_session import (
    BatchSession,
    ExecutionResult,
    PipeSession,
    StataSession,
    strip_smcl,
)

# ---------------------------------------------------------------------------
# Skip marker
# ---------------------------------------------------------------------------

requires_stata = pytest.mark.skipif(
    discover_stata_or_none() is None,
    reason="Stata not installed",
)


# ---------------------------------------------------------------------------
# PipeSession (persistent, no-PTY fallback)
# ---------------------------------------------------------------------------


@requires_stata
class TestPipeSessionPersistence:
    """The persistent pipe fallback must keep in-memory state between calls.

    Unlike the interactive :class:`StataSession`, :class:`PipeSession` needs
    no PTY, so these tests run even in sandboxed environments that deny
    ``openpty`` (where the interactive fixtures are skipped/errored).
    """

    async def _new(self) -> PipeSession:
        installation = discover_stata_or_none()
        assert installation is not None
        s = PipeSession(installation)
        await s.start()
        return s

    async def test_data_persists_between_calls(self):
        """Data loaded in one execute() must survive into the next call."""
        s = await self._new()
        try:
            await s.execute("sysuse auto, clear")
            result = await s.execute("count")  # separate call, same process
            assert result.return_code == 0
            assert "74" in result.output
        finally:
            await s.close()

    async def test_stored_results_persist_across_calls(self):
        """e()-class results from a regression must be readable in a later call."""
        s = await self._new()
        try:
            await s.execute("sysuse auto, clear")
            await s.execute("regress price mpg")
            result = await s.execute("display e(N)")
            assert "74" in result.output
        finally:
            await s.close()

    async def test_session_survives_error(self):
        """An error in one call must not kill the persistent session."""
        s = await self._new()
        try:
            bad = await s.execute("this_is_not_a_command")
            assert bad.return_code == 1
            assert s.is_alive is True
            ok = await s.execute("display 1+1")
            assert "2" in ok.output
        finally:
            await s.close()

    async def test_close_releases_process(self):
        """close() must terminate the process and report not-alive."""
        s = await self._new()
        assert s.is_alive is True
        await s.close()
        assert s.is_alive is False


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


@requires_stata
class TestBasicExecution:
    """Core execute-and-inspect workflow."""

    async def test_simple_display(self, session: StataSession):
        """``display 1+1`` should output '2' with rc=0."""
        result = await session.execute("display 1+1")
        assert isinstance(result, ExecutionResult)
        assert result.return_code == 0
        assert result.success is True
        assert "2" in result.output

    async def test_multi_line_code(self, session: StataSession):
        """Multiple statements separated by newlines should all run."""
        code = "display 10\ndisplay 20\ndisplay 30"
        result = await session.execute(code)
        assert result.return_code == 0
        assert "10" in result.output
        assert "20" in result.output
        assert "30" in result.output

    async def test_load_sysuse_auto(self, session: StataSession):
        """Loading the built-in 'auto' dataset should succeed."""
        result = await session.execute("sysuse auto, clear")
        assert result.return_code == 0

        result = await session.execute("describe, short")
        assert result.return_code == 0
        assert "74" in result.output  # 74 observations
        assert "price" in result.output.lower() or "12" in result.output  # 12 vars or 'price'

    async def test_summarize(self, session: StataSession):
        """``summarize price`` on the auto dataset should return statistics."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("summarize price")
        assert result.return_code == 0
        # The mean of price in the auto dataset is approximately 6165
        assert "6165" in result.output

    async def test_regression(self, session: StataSession):
        """A simple OLS regression should run and mention the regressors."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("regress price mpg weight")
        assert result.return_code == 0
        assert "mpg" in result.output.lower()
        assert "weight" in result.output.lower()

    async def test_tabulate(self, session: StataSession):
        """``tabulate foreign`` on auto should show 'Domestic' and 'Foreign'."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("tabulate foreign")
        assert result.return_code == 0
        assert "Domestic" in result.output or "domestic" in result.output.lower()

    async def test_execution_time_recorded(self, session: StataSession):
        """ExecutionResult.execution_time should be a positive float."""
        result = await session.execute("display 42")
        assert result.execution_time > 0.0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@requires_stata
class TestErrorHandling:
    """Verify that errors are properly detected and reported."""

    async def test_unrecognised_command(self, session: StataSession):
        """A completely invalid command should produce a non-zero rc."""
        result = await session.execute("this_is_not_a_command")
        assert result.return_code != 0
        assert result.error_code is not None
        assert result.success is False

    async def test_variable_not_found(self, session: StataSession):
        """Referencing a non-existent variable should error."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("summarize nonexistent_variable")
        assert result.return_code != 0

    async def test_error_message_populated(self, session: StataSession):
        """The error_message field should contain descriptive text."""
        result = await session.execute("this_is_bogus_cmd")
        assert result.error_message is not None
        assert len(result.error_message) > 0

    async def test_session_survives_error(self, session: StataSession):
        """The session should remain usable after a command error."""
        await session.execute("this_is_not_a_command")
        result = await session.execute("display 42")
        assert result.return_code == 0
        assert "42" in result.output


# ---------------------------------------------------------------------------
# Graph capture
# ---------------------------------------------------------------------------


@requires_stata
class TestGraphCapture:
    """Verify automatic graph export and capture."""

    async def test_scatter_graph(self, session: StataSession):
        """A scatter plot should produce at least one graph artifact."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("scatter price mpg")
        assert result.return_code == 0
        assert len(result.graphs) > 0

    async def test_graph_artifact_has_base64(self, session: StataSession):
        """Captured graph artifacts should contain non-empty base64 data."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("scatter price mpg")
        assert len(result.graphs) > 0
        artifact = result.graphs[0]
        assert artifact.format in ("png", "pdf", "svg", "gph")
        assert len(artifact.base64) > 0

    async def test_histogram_graph(self, session: StataSession):
        """A histogram should also be captured."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("histogram price")
        assert result.return_code == 0
        assert len(result.graphs) > 0

    async def test_no_graph_for_non_graph_command(self, session: StataSession):
        """A plain computation should not generate graph artifacts."""
        await session.execute("sysuse auto, clear")
        result = await session.execute("display 1+1")
        assert result.return_code == 0
        assert len(result.graphs) == 0


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@requires_stata
class TestSessionLifecycle:
    """Test session start, close, and is_alive behaviour."""

    async def test_session_is_alive(self, session: StataSession):
        """A started session should report is_alive=True."""
        assert session.is_alive is True

    async def test_close_and_dead(self):
        """After close() the session should report is_alive=False."""
        installation = discover_stata_or_none()
        assert installation is not None
        s = StataSession(installation)
        await s.start()
        assert s.is_alive is True
        await s.close()
        assert s.is_alive is False

    async def test_auto_restart(self):
        """Executing on a closed session should auto-restart it."""
        installation = discover_stata_or_none()
        assert installation is not None
        s = StataSession(installation)
        await s.start()
        # Forcefully kill the underlying process.
        if s._process is not None:
            s._process.terminate(force=True)
        assert s.is_alive is False
        # The next execute should transparently restart.
        result = await s.execute("display 99")
        assert result.return_code == 0
        assert "99" in result.output
        await s.close()

    async def test_get_log(self, session: StataSession):
        """get_log() should accumulate output from executed commands."""
        await session.execute("display 111")
        await session.execute("display 222")
        log_text = session.get_log()
        assert "111" in log_text
        assert "222" in log_text


# ---------------------------------------------------------------------------
# Multi-session (SessionManager)
# ---------------------------------------------------------------------------


@requires_stata
class TestMultiSession:
    """Test SessionManager with multiple named sessions."""

    async def test_create_session(self, session_manager):
        """get_or_create should return a started session."""
        s = await session_manager.get_or_create("alpha")
        assert s.is_alive is True

    async def test_same_id_returns_same_session(self, session_manager):
        """Requesting the same ID twice should return the same object."""
        s1 = await session_manager.get_or_create("beta")
        s2 = await session_manager.get_or_create("beta")
        assert s1 is s2

    async def test_different_ids_return_different_sessions(self, session_manager):
        """Different session IDs should produce distinct sessions."""
        s1 = await session_manager.get_or_create("gamma")
        s2 = await session_manager.get_or_create("delta")
        assert s1 is not s2

    async def test_session_isolation(self, session_manager):
        """Data loaded in one session must not leak into another."""
        s1 = await session_manager.get_or_create("session1")
        s2 = await session_manager.get_or_create("session2")

        await s1.execute("sysuse auto, clear")
        await s2.execute("sysuse nlsw88, clear")

        r1 = await s1.execute("display _N")
        r2 = await s2.execute("display _N")

        assert "74" in r1.output  # auto has 74 obs
        assert "2246" in r2.output  # nlsw88 has 2246 obs

    async def test_list_sessions(self, session_manager):
        """list_sessions() should reflect created sessions."""
        await session_manager.get_or_create("first")
        await session_manager.get_or_create("second")
        listing = await session_manager.list_sessions()
        ids = {entry["session_id"] for entry in listing}
        assert "first" in ids
        assert "second" in ids
        for entry in listing:
            assert entry["alive"] is True
            assert entry["type"] in ("interactive", "batch", "pipe")

    async def test_concurrent_same_id_dedup(self, session_manager):
        """Concurrent get_or_create for one id must return one shared session.

        Exercises the reserve-then-start-outside-the-lock path: only one caller
        should create the session; the others wait and receive the same object.
        """
        import anyio

        results: list = []

        async def grab() -> None:
            results.append(await session_manager.get_or_create("concurrent"))

        async with anyio.create_task_group() as tg:
            for _ in range(4):
                tg.start_soon(grab)

        assert len(results) == 4
        assert all(s is results[0] for s in results)
        assert results[0].is_alive is True

    async def test_cancel_during_create_does_not_deadlock(self, session_manager):
        """Cancelling a creation mid-boot must not leak the _creating Event.

        Regression guard: if the per-id creation Event were left unset, every
        later get_or_create for that id would block forever on wait_event.
        """
        import anyio

        # Cancel the first creation while Stata is still booting.
        with anyio.move_on_after(0.3):
            await session_manager.get_or_create("cancel_race")

        # The Event must have been cleaned up: a fresh call must NOT hang and
        # must yield a working session.
        with anyio.fail_after(90):
            s = await session_manager.get_or_create("cancel_race")
        assert s.is_alive is True
        # No half-created session should linger under another id.
        assert "cancel_race" in {e["session_id"] for e in await session_manager.list_sessions()}

    async def test_close_single_session(self, session_manager):
        """close_session() should remove exactly one session."""
        s1 = await session_manager.get_or_create("to_keep")
        await session_manager.get_or_create("to_close")
        await session_manager.close_session("to_close")

        listing = await session_manager.list_sessions()
        ids = {entry["session_id"] for entry in listing}
        assert "to_close" not in ids
        assert "to_keep" in ids
        assert s1.is_alive is True


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------


@requires_stata
class TestResultExtraction:
    """Test the ResultExtractor against live Stata sessions."""

    async def test_scalar_extraction_r_class(self, session, extractor):
        """Extracting r(mean) after ``summarize price`` should yield a float."""
        await session.execute("sysuse auto, clear")
        await session.execute("summarize price")
        mean = await extractor.get_scalar("mean", "r")
        assert isinstance(mean, float)
        # The mean of price in the auto dataset is ~6165.26
        assert 6000 < mean < 6500

    async def test_scalar_extraction_n(self, session, extractor):
        """r(N) after summarize should be 74 for the auto dataset."""
        await session.execute("sysuse auto, clear")
        await session.execute("summarize price")
        n = await extractor.get_scalar("N", "r")
        assert isinstance(n, float)
        assert n == 74.0

    async def test_scalar_extraction_e_class(self, session, extractor):
        """e(r2) after regression should be a float between 0 and 1."""
        await session.execute("sysuse auto, clear")
        await session.execute("regress price mpg weight")
        r2 = await extractor.get_scalar("r2", "e")
        assert isinstance(r2, float)
        assert 0.0 < r2 < 1.0

    async def test_get_macro(self, session, extractor):
        """e(depvar) after regression should be 'price'."""
        await session.execute("sysuse auto, clear")
        await session.execute("regress price mpg weight")
        depvar = await extractor.get_macro("depvar", "e")
        assert depvar is not None
        assert "price" in depvar.lower()

    async def test_get_matrix(self, session, extractor):
        """e(b) after regression should be a 1xK matrix of coefficients."""
        await session.execute("sysuse auto, clear")
        await session.execute("regress price mpg weight")
        b = await extractor.get_matrix("b", "e")
        assert b is not None
        assert len(b) >= 1  # at least 1 row
        assert len(b[0]) >= 3  # mpg, weight, _cons

    async def test_get_all(self, session, extractor):
        """get_all('r') after summarize should include scalars like N and mean."""
        await session.execute("sysuse auto, clear")
        await session.execute("summarize price")
        results = await extractor.get_all("r")
        assert "scalars" in results
        assert "macros" in results
        assert "matrices" in results
        assert "N" in results["scalars"]
        assert "mean" in results["scalars"]

    async def test_invalid_result_class(self, extractor):
        """An invalid result_class should raise ValueError."""
        with pytest.raises(ValueError, match="result_class must be"):
            await extractor.get_scalar("N", "x")

    async def test_nonexistent_scalar_returns_none(self, session, extractor):
        """Asking for a scalar that does not exist should return None."""
        await session.execute("sysuse auto, clear")
        await session.execute("summarize price")
        result = await extractor.get_scalar("totally_fake_scalar_name", "r")
        assert result is None


# ---------------------------------------------------------------------------
# SMCL stripping (unit-level, no Stata needed)
# ---------------------------------------------------------------------------


class TestSmclStripping:
    """Test strip_smcl() on synthetic SMCL fragments."""

    def test_strip_result_tag(self):
        assert strip_smcl("{result}hello{txt}") == "hello"

    def test_strip_error_tag(self):
        assert strip_smcl("{error}bad command{txt}") == "bad command"

    def test_strip_hline(self):
        """An hline tag should be removed."""
        cleaned = strip_smcl("{hline 13}")
        assert "hline" not in cleaned

    def test_strip_char_pipe(self):
        """SMCL {c |} should become a literal pipe."""
        assert strip_smcl("{c |}") == "|"

    def test_plain_text_unmodified(self):
        """Text without SMCL tags should pass through unchanged."""
        text = "The mean is 6165.26"
        assert strip_smcl(text) == text

    def test_nested_tags(self):
        """Multiple tags in one string should all be stripped."""
        raw = "{res}  price {txt}{hline 10}{result}6165.256{txt}"
        cleaned = strip_smcl(raw)
        assert "{" not in cleaned
        assert "}" not in cleaned
        assert "6165.256" in cleaned


# ---------------------------------------------------------------------------
# ExecutionResult dataclass (unit-level, no Stata needed)
# ---------------------------------------------------------------------------


class TestExecutionResult:
    """Test the ExecutionResult dataclass."""

    def test_success_property(self):
        r = ExecutionResult(output="ok", return_code=0)
        assert r.success is True

    def test_failure_property(self):
        r = ExecutionResult(output="err", return_code=1, error_code=198)
        assert r.success is False

    def test_default_fields(self):
        r = ExecutionResult(output="", return_code=0)
        assert r.error_message is None
        assert r.error_code is None
        assert r.graphs == []
        assert r.execution_time == 0.0
        assert r.log_path is None


# ---------------------------------------------------------------------------
# Batch session
# ---------------------------------------------------------------------------


@requires_stata
class TestBatchSession:
    """Test the BatchSession fallback path."""

    async def test_batch_execute(self):
        """Batch mode should execute simple commands."""
        installation = discover_stata_or_none()
        assert installation is not None
        bs = BatchSession(installation, session_id="batch_test")
        await bs.start()
        try:
            result = await bs.execute("display 42")
            assert result.return_code == 0
            assert "42" in result.output
        finally:
            await bs.close()

    async def test_batch_is_alive(self):
        """BatchSession.is_alive should reflect started state."""
        installation = discover_stata_or_none()
        assert installation is not None
        bs = BatchSession(installation, session_id="alive_test")
        assert bs.is_alive is False
        await bs.start()
        assert bs.is_alive is True
        await bs.close()
        assert bs.is_alive is False

    async def test_batch_error_detection(self):
        """Batch mode should detect errors in Stata output."""
        installation = discover_stata_or_none()
        assert installation is not None
        bs = BatchSession(installation, session_id="error_test")
        await bs.start()
        try:
            result = await bs.execute("this_is_not_a_command")
            assert result.return_code != 0
        finally:
            await bs.close()

    async def test_batch_log_file(self):
        """Batch mode should produce a log_path when output is available."""
        installation = discover_stata_or_none()
        assert installation is not None
        bs = BatchSession(installation, session_id="log_test")
        await bs.start()
        try:
            result = await bs.execute("display 77")
            # log_path may or may not exist depending on Stata's behaviour,
            # but if it does, it should be a Path.
            if result.log_path is not None:
                assert result.log_path.exists()
        finally:
            await bs.close()
