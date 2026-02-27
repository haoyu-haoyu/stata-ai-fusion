"""Stata session management — interactive (pexpect) and batch fallback.

Provides :class:`StataSession` for interactive Stata communication via
``pexpect``, :class:`BatchSession` as a fallback when ``pexpect`` is not
available, :class:`SessionManager` for managing multiple named sessions, and
the :class:`ExecutionResult` dataclass that all execution methods return.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

try:
    import pexpect

    HAS_PEXPECT = True
except ImportError:  # pragma: no cover
    pexpect = None  # type: ignore[assignment]
    HAS_PEXPECT = False

from .graph_cache import GraphArtifact, GraphCache, maybe_inject_graph_export
from .stata_discovery import StataInstallation

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Stata's interactive prompt: ". " (dot-space) at start of a line.
# We also handle the continuation prompt "> " and the MATA prompt ": ".
_PROMPT_PATTERN = r"\r?\n\. $"
_CONTINUATION_PATTERN = r"\r?\n> $"

# Timeout defaults (seconds)
_DEFAULT_TIMEOUT = 120
_START_TIMEOUT = 30

# ---------------------------------------------------------------------------
# SMCL tag stripping
# ---------------------------------------------------------------------------

_SMCL_TAG_RE = re.compile(
    r"\{(?:"
    r"res(?:ult)?|txt|text|err(?:or)?|cmd|inp(?:ut)?|bf|it|sf|"
    r"com|hline(?:\s+\d+)?|dup\s+\d+:[^}]*|space\s+\d+|col\s+\d+|"
    r"ralign\s+\d+:[^}]*|lalign\s+\d+:[^}]*|center\s+\d+:[^}]*|"
    r"right|reset|smcl|p_end|p |pstd|phang|pmore|p2colset[^}]*|"
    r"p2col[^}]*|p2line[^}]*|marker[^}]*|dlgtab[^}]*|title[^}]*|"
    r"hi(?:lite)?|ul\s+(?:on|off)|bind\s+[^}]*|char\s+[^}]*|break"
    r")\}"
)

# Lines consisting solely of a horizontal SMCL rule: {hline} or  {hline N}
_SMCL_HLINE_ONLY_RE = re.compile(r"^\s*\{hline(?:\s+\d+)?\}\s*$")

# Numeric SMCL escapes like {c |}
_SMCL_CHAR_RE = re.compile(r"\{c\s+([^}]+)\}")

# SMCL char mappings
_SMCL_CHAR_MAP: dict[str, str] = {
    "|": "|",
    "-": "-",
    "+": "+",
    "TT": "+",
    "BT": "+",
    "TLC": "+",
    "TRC": "+",
    "BLC": "+",
    "BRC": "+",
    "LT": "+",
    "RT": "+",
}


def strip_smcl(text: str) -> str:
    """Remove Stata SMCL markup tags from *text* and return plain text."""

    # Replace {c ...} character escapes first.
    def _replace_char(m: re.Match[str]) -> str:
        key = m.group(1).strip()
        return _SMCL_CHAR_MAP.get(key, "")

    text = _SMCL_CHAR_RE.sub(_replace_char, text)
    # Strip all remaining SMCL tags.
    text = _SMCL_TAG_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Error detection
# ---------------------------------------------------------------------------

ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"r\((\d+)\)"),  # standard error code r(111), r(198), etc.
    re.compile(r"no observations", re.IGNORECASE),
    re.compile(r"variable\s+.+\s+not found", re.IGNORECASE),
    re.compile(r"type mismatch", re.IGNORECASE),
    re.compile(r"conformability error", re.IGNORECASE),
    re.compile(r"op\.sys refuses to", re.IGNORECASE),
    re.compile(r"could not find file", re.IGNORECASE),
    re.compile(r"no room to add more", re.IGNORECASE),
]

# Specific pattern for extracting numeric error codes: r(NNN)
_ERROR_CODE_RE = re.compile(r"r\((\d+)\)")


def _detect_error(output: str) -> tuple[str | None, int | None]:
    """Scan *output* for Stata error indicators.

    Returns
    -------
    tuple[str | None, int | None]
        ``(error_message, error_code)`` if an error is found, or
        ``(None, None)`` if the output looks clean.
    """
    # First check for the standard r(NNN) error code.
    m = _ERROR_CODE_RE.search(output)
    if m:
        code = int(m.group(1))
        # Try to extract the line preceding the error code as a message.
        # Stata typically prints the error text on the line(s) before r(NNN).
        lines = output.splitlines()
        error_msg_parts: list[str] = []
        for line in lines:
            stripped = line.strip()
            if _ERROR_CODE_RE.search(stripped):
                break
            if stripped:
                error_msg_parts.append(stripped)
        error_msg = error_msg_parts[-1] if error_msg_parts else f"Stata error r({code})"
        return error_msg, code

    # Check for other error patterns (no numeric code).
    for pattern in ERROR_PATTERNS[1:]:
        pm = pattern.search(output)
        if pm:
            return pm.group(0), None

    return None, None


# ---------------------------------------------------------------------------
# Temp directory management
# ---------------------------------------------------------------------------


def _make_temp_dir() -> Path:
    """Create a temporary directory for a session.

    Respects the ``MCP_STATA_TEMP`` environment variable when set.
    """
    base = os.environ.get("MCP_STATA_TEMP")
    if base:
        base_path = Path(base)
        base_path.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="stata_session_", dir=str(base_path)))
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix="stata_session_"))
    log.debug("Created session temp dir: %s", tmpdir)
    return tmpdir


def _cleanup_temp_dir(tmpdir: Path) -> None:
    """Remove *tmpdir* and all its contents, ignoring errors."""
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
        log.debug("Cleaned up temp dir: %s", tmpdir)
    except Exception:
        log.warning("Failed to clean up temp dir: %s", tmpdir, exc_info=True)


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """The result of executing Stata code."""

    output: str  # Stata output text (SMCL-stripped)
    return_code: int  # 0 = success, non-0 = error
    error_message: str | None = field(default=None)  # error message if any
    error_code: int | None = field(default=None)  # Stata error code (e.g. 111, 198)
    graphs: list[GraphArtifact] = field(default_factory=list)  # produced graphs
    execution_time: float = field(default=0.0)  # execution time in seconds
    log_path: Path | None = field(default=None)  # log file path

    @property
    def success(self) -> bool:
        """Return ``True`` when the command succeeded."""
        return self.return_code == 0


# ---------------------------------------------------------------------------
# StataSession (interactive, pexpect-based)
# ---------------------------------------------------------------------------


class StataSession:
    """Interactive Stata session managed via ``pexpect``.

    Parameters
    ----------
    installation:
        A :class:`StataInstallation` describing which Stata binary to use.
    session_id:
        A human-readable identifier for this session.
    """

    def __init__(
        self,
        installation: StataInstallation,
        session_id: str = "default",
    ) -> None:
        self.installation = installation
        self.session_id = session_id
        self._process: pexpect.spawn | None = None  # type: ignore[union-attr]
        self._lock: anyio.Lock = anyio.Lock()
        self._log_buffer: list[str] = []
        self._tmpdir: Path = _make_temp_dir()
        self._graph_cache: GraphCache = GraphCache(self._tmpdir)
        self._started: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Stata process using ``pexpect``.

        The process is launched inside the session temp directory so that
        graph files written to the current directory are captured by the
        :class:`GraphCache`.
        """
        if not HAS_PEXPECT:
            msg = (
                "pexpect is required for StataSession but is not installed. "
                "Install it with: pip install pexpect"
            )
            raise RuntimeError(msg)

        if self._process is not None and self._process.isalive():
            log.debug("Session %s already running", self.session_id)
            return

        log.info(
            "Starting Stata session %s with %s",
            self.session_id,
            self.installation,
        )

        # Run pexpect spawn in a thread because it blocks.
        self._process = await anyio.to_thread.run_sync(
            self._spawn_process, abandon_on_cancel=True,
        )
        self._started = True
        log.info("Session %s started successfully", self.session_id)

    def _spawn_process(self) -> pexpect.spawn:  # type: ignore[name-defined]
        """Synchronous helper to spawn the Stata process."""
        stata_path = str(self.installation.path)

        # Start Stata in interactive (console) mode.
        # -q suppresses the startup banner for cleaner output parsing.
        child = pexpect.spawn(
            stata_path,
            args=["-q"],
            cwd=str(self._tmpdir),
            encoding="utf-8",
            timeout=_START_TIMEOUT,
            env={**os.environ, "TERM": "dumb"},
            preexec_fn=os.setsid,
        )

        # Wait for the initial prompt.
        child.expect(r"\. $", timeout=_START_TIMEOUT)

        # Disable GUI graph windows so Stata never blocks waiting for
        # user interaction in the background.
        child.sendline("set graphics off")
        child.expect(r"\. $", timeout=_START_TIMEOUT)

        return child

    async def close(self) -> None:
        """Close the Stata process and clean up resources."""
        if self._process is not None:
            log.info("Closing session %s", self.session_id)
            try:
                if self._process.isalive():
                    # Send the exit command gracefully.
                    self._process.sendline("exit, clear")
                    try:
                        self._process.expect(pexpect.EOF, timeout=10)
                    except (pexpect.TIMEOUT, pexpect.EOF):
                        pass
                    if self._process.isalive():
                        # Kill the entire process group, same as _kill_process().
                        try:
                            os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            self._process.terminate(force=True)
            except Exception:
                log.warning(
                    "Error closing session %s",
                    self.session_id,
                    exc_info=True,
                )
            finally:
                self._process = None
                self._started = False

        _cleanup_temp_dir(self._tmpdir)

    @property
    def is_alive(self) -> bool:
        """Check whether the Stata process is still running."""
        if self._process is None:
            return False
        try:
            return self._process.isalive()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _kill_process(self) -> None:
        """Forcefully kill the Stata process *and all its children*, then
        mark the session for restart.

        Stata-MP spawns helper worker processes.  A plain ``terminate()``
        only signals the lead process; the workers can linger and hold the
        licence.  By sending SIGKILL to the entire process **group**
        (created via ``os.setsid`` in :meth:`_spawn_process`) we guarantee
        a clean slate.
        """
        if self._process is not None:
            try:
                pid = self._process.pid
                if pid and self._process.isalive():
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                        log.debug(
                            "Killed process group for session %s (pid %d)",
                            self.session_id,
                            pid,
                        )
                    except (ProcessLookupError, PermissionError):
                        # Race: process already exited — fall back.
                        self._process.terminate(force=True)
            except Exception:
                log.warning(
                    "Error killing session %s process",
                    self.session_id,
                    exc_info=True,
                )
            self._process = None
        self._started = False

    def send_interrupt(self) -> bool:
        """Send SIGINT (Ctrl-C) to the running Stata process.

        Returns ``True`` if the signal was sent, ``False`` if the process
        is not alive.  This is a *gentle* cancellation — Stata will abort
        the current command but the session stays usable.
        """
        if self._process is not None and self._process.isalive():
            self._process.sendintr()
            log.info("Sent interrupt to session %s", self.session_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Auto-restart
    # ------------------------------------------------------------------

    async def _ensure_alive(self) -> None:
        """Restart the process if it has died unexpectedly."""
        if not self.is_alive:
            if self._started:
                log.warning(
                    "Session %s process died; restarting",
                    self.session_id,
                )
            # Re-create temp dir if it was cleaned up.
            if not self._tmpdir.exists():
                self._tmpdir = _make_temp_dir()
                self._graph_cache = GraphCache(self._tmpdir)
            await self.start()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, code: str, timeout: int = _DEFAULT_TIMEOUT) -> ExecutionResult:
        """Execute Stata code and return the result.

        The implementation writes multi-line code to a temporary ``.do`` file
        and executes it with Stata's ``do`` command, then captures all output
        until the prompt returns.

        A per-session async lock ensures that only one command runs at a time,
        preventing concurrent access to the shared ``pexpect`` process.

        Parameters
        ----------
        code:
            One or more Stata commands, possibly spanning multiple lines.
        timeout:
            Maximum seconds to wait for the command to finish.

        Returns
        -------
        ExecutionResult
        """
        async with self._lock:
            await self._ensure_alive()
            assert self._process is not None  # guaranteed by _ensure_alive

            # Inject graph export if the code draws a graph but has no export.
            code = maybe_inject_graph_export(code, self._tmpdir)

            # Snapshot graph files before execution.
            self._graph_cache.take_snapshot()

            start_time = time.monotonic()

            # Write code to a temp .do file.
            do_file = self._tmpdir / f"_cmd_{uuid.uuid4().hex[:12]}.do"
            do_file.write_text(code, encoding="utf-8")

            try:
                raw_output = await anyio.to_thread.run_sync(
                    lambda: self._run_do_file(do_file, timeout),
                    # NOTE: Do NOT use abandon_on_cancel here.  The lock
                    # protects self._process; if we abandon the thread the
                    # lock is released while the thread is still inside
                    # pexpect.expect(), and the next caller would corrupt
                    # the pexpect state.  The deadline-based timeout in
                    # _run_do_file guarantees bounded execution.
                )
            except pexpect.TIMEOUT:
                elapsed = time.monotonic() - start_time
                # Capture whatever partial output Stata produced before
                # the timeout — this often contains useful progress info.
                partial = ""
                try:
                    if self._process is not None and self._process.before:
                        partial = strip_smcl(self._process.before).strip()
                except Exception:
                    pass

                # After a timeout the pexpect buffer is in an unknown
                # state (partial output, Stata may still be running the
                # timed-out command).  Kill the process so _ensure_alive()
                # spawns a clean one on the next call.
                self._kill_process()

                hint = (
                    f"Command timed out after {timeout}s. "
                    "The session has been reset and will auto-restart on "
                    "the next command.  Tips:\n"
                    "  • Increase the timeout parameter.\n"
                    "  • For long-running commands (bootstrap, mixed models) "
                    "use the run_do_file tool which runs in batch mode.\n"
                    "  • Use stata_cancel_command to abort a running command "
                    "without losing the session."
                )
                output_text = f"{hint}\n\n--- partial output ---\n{partial}" if partial else hint

                return ExecutionResult(
                    output=output_text,
                    return_code=1,
                    error_message=hint,
                    error_code=None,
                    graphs=[],
                    execution_time=elapsed,
                    log_path=None,
                )
            except pexpect.EOF:
                elapsed = time.monotonic() - start_time
                self._process = None
                self._started = False
                return ExecutionResult(
                    output="",
                    return_code=1,
                    error_message="Stata process terminated unexpectedly",
                    error_code=None,
                    graphs=[],
                    execution_time=elapsed,
                    log_path=None,
                )
            finally:
                # Clean up the temp .do file.
                try:
                    do_file.unlink(missing_ok=True)
                except OSError:
                    pass

            elapsed = time.monotonic() - start_time

            # Strip SMCL markup.
            cleaned = strip_smcl(raw_output)

            # Remove the echoed "do" command line and trailing prompt noise.
            cleaned = self._clean_do_output(cleaned, do_file)

            # Detect errors.
            error_message, error_code = _detect_error(cleaned)
            return_code = 0 if error_code is None and error_message is None else 1

            # Detect new graph files.
            graphs = self._graph_cache.detect_changes()

            # Append to log buffer.
            self._log_buffer.append(cleaned)

            log.debug(
                "Session %s execute completed in %.2fs (rc=%d, graphs=%d)",
                self.session_id,
                elapsed,
                return_code,
                len(graphs),
            )

            return ExecutionResult(
                output=cleaned,
                return_code=return_code,
                error_message=error_message,
                error_code=error_code,
                graphs=graphs,
                execution_time=elapsed,
                log_path=None,
            )

    def _run_do_file(self, do_file: Path, timeout: int) -> str:
        """Synchronous helper: send ``do "file"`` and collect output until prompt.

        Uses a **deadline-based** total timeout so that continuation prompts
        cannot reset the clock and cause unbounded waiting.
        """
        assert self._process is not None

        cmd = f'do "{do_file}"'
        self._process.sendline(cmd)

        # Use an absolute deadline so the total wait never exceeds *timeout*
        # seconds, even when Stata produces many continuation prompts.
        deadline = time.monotonic() + timeout
        output_parts: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise pexpect.TIMEOUT(
                    f"Total execution timeout exceeded ({timeout}s)"
                )
            idx = self._process.expect(
                [
                    _PROMPT_PATTERN,  # 0 — primary prompt
                    _CONTINUATION_PATTERN,  # 1 — continuation prompt
                ],
                timeout=remaining,
            )
            # Capture everything printed before the matched pattern.
            before = self._process.before or ""
            output_parts.append(before)

            if idx == 0:
                # Reached the primary prompt — command finished.
                break
            # idx == 1: continuation prompt — keep reading.

        return "".join(output_parts)

    @staticmethod
    def _clean_do_output(output: str, do_file: Path) -> str:
        """Remove the echoed ``do`` command and surrounding noise."""
        do_file_stem = do_file.stem
        lines = output.splitlines()
        cleaned: list[str] = []
        skip_do_echo = True
        for line in lines:
            stripped = line.strip()
            # Skip the echoed "do ..." line and the "end of do-file" marker.
            if skip_do_echo and (
                stripped.startswith(f'do "{do_file}')
                or stripped.startswith("do ")
                or stripped == ""
            ):
                if stripped.startswith("do "):
                    skip_do_echo = False
                continue
            if stripped == "end of do-file":
                continue
            # Skip continuation-prompt residue that references the .do file.
            if stripped.startswith("> ") and do_file_stem in stripped:
                continue
            # Skip echoed commands from the .do file (Stata echoes each
            # command with a leading ". " when running a .do file).
            if stripped.startswith(". "):
                continue
            cleaned.append(line)

        # Strip trailing prompt residue: standalone "." lines at the end.
        while cleaned and cleaned[-1].strip() == ".":
            cleaned.pop()

        return "\n".join(cleaned).strip()

    # ------------------------------------------------------------------
    # Log access
    # ------------------------------------------------------------------

    def get_log(self) -> str:
        """Return the accumulated session log."""
        return "\n".join(self._log_buffer)


# ---------------------------------------------------------------------------
# BatchSession (fallback when pexpect is unavailable)
# ---------------------------------------------------------------------------


class BatchSession:
    """Fallback session that runs Stata in batch mode.

    Each :meth:`execute` call:

    1. Writes the code to a temporary ``.do`` file.
    2. Runs ``stata -b do <file>`` as a subprocess.
    3. Reads the generated ``.log`` file.
    4. Parses the output and returns an :class:`ExecutionResult`.

    This is less efficient than :class:`StataSession` because every
    invocation starts a new Stata process, but it works everywhere
    (including platforms where ``pexpect`` is not available).
    """

    def __init__(
        self,
        installation: StataInstallation,
        session_id: str = "default",
    ) -> None:
        self.installation = installation
        self.session_id = session_id
        self._lock: anyio.Lock = anyio.Lock()
        self._tmpdir: Path = _make_temp_dir()
        self._graph_cache: GraphCache = GraphCache(self._tmpdir)
        self._log_buffer: list[str] = []
        self._started: bool = False

    # ------------------------------------------------------------------
    # Lifecycle (thin — no persistent process)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """No-op for batch mode; just mark as started."""
        self._started = True
        log.info("BatchSession %s ready (batch mode)", self.session_id)

    async def close(self) -> None:
        """Clean up the temporary directory."""
        self._started = False
        _cleanup_temp_dir(self._tmpdir)
        log.info("BatchSession %s closed", self.session_id)

    @property
    def is_alive(self) -> bool:
        """Batch sessions are always 'alive' once started."""
        return self._started

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, code: str, timeout: int = _DEFAULT_TIMEOUT) -> ExecutionResult:
        """Execute Stata code in batch mode.

        A per-session async lock serializes concurrent calls.
        """
        async with self._lock:
            if not self._started:
                await self.start()

            # Inject graph export if needed.
            code = maybe_inject_graph_export(code, self._tmpdir)

            # Snapshot graph files.
            self._graph_cache.take_snapshot()

            start_time = time.monotonic()

            # Write code to a temp .do file.
            do_name = f"_batch_{uuid.uuid4().hex[:12]}"
            do_file = self._tmpdir / f"{do_name}.do"
            log_file = self._tmpdir / f"{do_name}.log"
            do_file.write_text(code, encoding="utf-8")

            try:
                raw_output = await anyio.to_thread.run_sync(
                    lambda: self._run_batch(do_file, log_file, timeout),
                    # NOTE: Do NOT use abandon_on_cancel here.  The
                    # subprocess.run timeout guarantees bounded execution,
                    # and keeping the lock held prevents concurrent access.
                )
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start_time
                return ExecutionResult(
                    output="",
                    return_code=1,
                    error_message=f"Batch command timed out after {timeout}s",
                    error_code=None,
                    graphs=[],
                    execution_time=elapsed,
                    log_path=log_file if log_file.exists() else None,
                )

            elapsed = time.monotonic() - start_time

            # Strip SMCL.
            cleaned = strip_smcl(raw_output)

            # Detect errors.
            error_message, error_code = _detect_error(cleaned)
            return_code = 0 if error_code is None and error_message is None else 1

            # Detect new graph files.
            graphs = self._graph_cache.detect_changes()

            # Append to log buffer.
            self._log_buffer.append(cleaned)

            log.debug(
                "BatchSession %s execute completed in %.2fs (rc=%d, graphs=%d)",
                self.session_id,
                elapsed,
                return_code,
                len(graphs),
            )

            return ExecutionResult(
                output=cleaned,
                return_code=return_code,
                error_message=error_message,
                error_code=error_code,
                graphs=graphs,
                execution_time=elapsed,
                log_path=log_file if log_file.exists() else None,
            )

    def _run_batch(self, do_file: Path, log_file: Path, timeout: int) -> str:
        """Synchronous helper: run Stata in batch mode and read the log.

        Uses ``start_new_session=True`` so that on timeout the entire
        process group (including Stata-MP worker processes) can be killed
        cleanly via ``os.killpg``.
        """
        stata_path = str(self.installation.path)

        proc = subprocess.Popen(
            [stata_path, "-b", "do", str(do_file)],
            cwd=str(self._tmpdir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the entire process group.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait()
            raise

        # Stata writes output to a .log file with the same stem.
        if log_file.exists():
            return log_file.read_text(encoding="utf-8", errors="replace")
        return ""

    # ------------------------------------------------------------------
    # Log access
    # ------------------------------------------------------------------

    def get_log(self) -> str:
        """Return the accumulated session log."""
        return "\n".join(self._log_buffer)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manage multiple named Stata sessions.

    Sessions are created on demand via :meth:`get_or_create` and can be
    individually or collectively closed.

    Parameters
    ----------
    installation:
        The Stata installation to use for new sessions.
    use_batch:
        Force batch mode even when ``pexpect`` is available.  When ``None``
        (the default), batch mode is used only when ``pexpect`` is not
        installed.
    """

    def __init__(
        self,
        installation: StataInstallation,
        *,
        use_batch: bool | None = None,
        session_timeout: int = 3600,
    ) -> None:
        self._sessions: dict[str, StataSession | BatchSession] = {}
        self._lock: anyio.Lock = anyio.Lock()
        self.installation = installation
        self._session_timeout = session_timeout
        self._last_activity: dict[str, float] = {}
        if use_batch is None:
            self._use_batch = not HAS_PEXPECT
        else:
            self._use_batch = use_batch

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_create(
        self,
        session_id: str = "default",
    ) -> StataSession | BatchSession:
        """Return an existing session or create and start a new one.

        A manager-level lock prevents TOCTOU races where concurrent
        callers with the same *session_id* could both create sessions.

        Parameters
        ----------
        session_id:
            A name that uniquely identifies the session.

        Returns
        -------
        StataSession | BatchSession
        """
        async with self._lock:
            # Expire idle sessions.
            now = time.monotonic()
            expired = [
                sid for sid, last in self._last_activity.items()
                if now - last > self._session_timeout
                and sid in self._sessions
            ]
            for sid in expired:
                sess = self._sessions.pop(sid)
                self._last_activity.pop(sid, None)
                log.info("Session %s expired after %ds idle", sid, self._session_timeout)
                # Close in background to avoid blocking the lock.
                try:
                    await sess.close()
                except Exception:
                    log.warning("Error closing expired session %s", sid, exc_info=True)

            if session_id in self._sessions:
                session = self._sessions[session_id]
                # Auto-restart if dead.
                if not session.is_alive:
                    log.warning(
                        "Session %s is dead; removing and re-creating",
                        session_id,
                    )
                    del self._sessions[session_id]
                    self._last_activity.pop(session_id, None)
                else:
                    self._last_activity[session_id] = time.monotonic()
                    return session

            log.info(
                "Creating new %s session: %s",
                "batch" if self._use_batch else "interactive",
                session_id,
            )

            session: StataSession | BatchSession
            if self._use_batch:
                session = BatchSession(self.installation, session_id=session_id)
            else:
                session = StataSession(self.installation, session_id=session_id)

            await session.start()
            self._sessions[session_id] = session
            self._last_activity[session_id] = time.monotonic()
            return session

    async def close_session(self, session_id: str) -> None:
        """Close and remove a single session by ID.

        Silently ignores unknown session IDs.
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._last_activity.pop(session_id, None)
        if session is not None:
            await session.close()
            log.info("Session %s closed and removed", session_id)

    async def list_sessions(self) -> list[dict]:
        """Return metadata for every tracked session.

        Each entry is a dict with keys ``"session_id"``, ``"alive"``,
        and ``"type"``.
        """
        async with self._lock:
            snapshot = list(self._sessions.items())
        result: list[dict] = []
        for sid, session in snapshot:
            result.append(
                {
                    "session_id": sid,
                    "alive": session.is_alive,
                    "type": "batch" if isinstance(session, BatchSession) else "interactive",
                }
            )
        return result

    async def close_all(self) -> None:
        """Close every tracked session."""
        async with self._lock:
            session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.close_session(sid)
        log.info("All sessions closed")
