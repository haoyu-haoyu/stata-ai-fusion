"""Stata session management — interactive (pexpect), persistent pipe, and batch.

Provides :class:`StataSession` for interactive Stata communication via
``pexpect``, :class:`PipeSession` as a *stateful* fallback for hosts that deny
PTY allocation (a single long-lived process driven over stdin, output read from
a log file — state persists between calls), :class:`BatchSession` as the
last-resort stateless fallback (a fresh process per call),
:class:`SessionManager` for managing multiple named sessions, and the
:class:`ExecutionResult` dataclass that all execution methods return.
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
import anyio

try:
    import pexpect

    HAS_PEXPECT = True
except ImportError:  # pragma: no cover
    pexpect = None  # type: ignore[assignment]
    HAS_PEXPECT = False

from .graph_cache import GraphArtifact, GraphCache, maybe_inject_graph_export
from .stata_discovery import StataInstallation

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

# Maximum number of entries kept in the per-session log buffer.
# Older entries are evicted in FIFO order to bound memory usage.
_MAX_LOG_BUFFER_ENTRIES = 1000

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
    # Strip all remaining SMCL tags — loop to handle nested tags.
    prev = None
    for _ in range(50):
        if prev == text:
            break
        prev = text
        text = _SMCL_TAG_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Error detection
# ---------------------------------------------------------------------------

# Stata prints an error's return code as ``r(NNN);`` at the START of a line.
# Anchoring (line start + trailing semicolon) is essential: the bare pattern
# ``r\(\d+\)`` matched a return code merely *mentioned* in output/help text or a
# comment — e.g. ``display "see r(198)"`` was wrongly reported as an error.
_ERROR_CODE_RE = re.compile(r"(?m)^[ \t]*r\((\d+)\);")

# Secondary patterns for the rare case where the ``r(NNN);`` line is absent from
# the captured output (e.g. it was truncated).  Anchored to the start of a line
# so the same words appearing mid-sentence in normal output do not misfire.
_TEXT_ERROR_RES: list[re.Pattern[str]] = [
    re.compile(r"^[ \t]*no observations\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*variable\s+\S+\s+not found\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*type mismatch\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*conformability error\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*op\.sys refuses to\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*could not find file\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*no room to add more\b", re.IGNORECASE | re.MULTILINE),
]


def _detect_error(output: str) -> tuple[str | None, int | None]:
    """Scan *output* for Stata error indicators.

    Returns
    -------
    tuple[str | None, int | None]
        ``(error_message, error_code)`` if an error is found, or
        ``(None, None)`` if the output looks clean.
    """
    # Primary, authoritative signal: an ``r(NNN);`` line that Stata emits when a
    # command aborts.
    m = _ERROR_CODE_RE.search(output)
    if m:
        code = int(m.group(1))
        # The error text is the non-empty, non-echo line(s) immediately before
        # the ``r(NNN);`` line.
        parts: list[str] = []
        for line in reversed(output[: m.start()].splitlines()):
            stripped = line.strip()
            if not stripped:
                if parts:
                    break  # blank line ends the message block
                continue
            # Stop at an echoed command (". cmd") or an earlier r(NNN); line.
            if stripped.startswith(". ") or _ERROR_CODE_RE.search(line):
                break
            parts.append(stripped)
            if len(parts) >= 2:
                break
        parts.reverse()
        error_msg = "\n".join(parts) if parts else f"Stata error r({code})"
        return error_msg, code

    # Fallback: a recognised error phrase at the start of a line.
    for pattern in _TEXT_ERROR_RES:
        pm = pattern.search(output)
        if pm:
            return pm.group(0).strip(), None

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

        # Run the blocking pexpect spawn in a worker thread.  Do NOT abandon on
        # cancel — that would leave the thread spawning Stata with no handle to
        # kill.  _spawn_process assigns self._process *before* waiting for the
        # prompt, so a process that launched but failed to initialise is still
        # reachable (and killable) here.
        try:
            await anyio.to_thread.run_sync(self._spawn_process)
        except BaseException:
            # Startup failed (PTY denied, Stata crashed/timed out at boot) or was
            # cancelled.  Kill whatever process did launch and remove the eagerly
            # created temp dir so neither leaks (incl. on CancelledError, which is
            # a BaseException — hence not 'except Exception').
            self._kill_process()
            _cleanup_temp_dir(self._tmpdir)
            raise
        self._started = True
        log.info("Session %s started successfully", self.session_id)

    def _spawn_process(self) -> None:
        """Spawn Stata and drive it to the first prompt.

        Assigns ``self._process`` to the spawned child *before* waiting for the
        prompt, so a process that launched but then failed (or was cancelled)
        mid-initialisation is still reachable for cleanup by :meth:`start`.
        """
        stata_path = str(self.installation.path)

        # Start Stata in interactive (console) mode.
        # -q suppresses the startup banner for cleaner output parsing.
        self._process = pexpect.spawn(
            stata_path,
            args=["-q"],
            cwd=str(self._tmpdir),
            encoding="utf-8",
            timeout=_START_TIMEOUT,
            env={**os.environ, "TERM": "dumb"},
            preexec_fn=os.setsid,
        )

        # Wait for the initial prompt.
        self._process.expect(r"\. $", timeout=_START_TIMEOUT)

        # Disable GUI graph windows so Stata never blocks waiting for
        # user interaction in the background.
        self._process.sendline("set graphics off")
        self._process.expect(r"\. $", timeout=_START_TIMEOUT)

    async def close(self) -> None:
        """Close the Stata process and clean up resources.

        The blocking pexpect calls (``sendline``, ``expect``) are run in a
        worker thread so the async event loop is never blocked — even if
        the Stata process is unresponsive.
        """
        if self._process is not None:
            log.info("Closing session %s", self.session_id)
            try:
                await anyio.to_thread.run_sync(self._close_sync)
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

    def _close_sync(self) -> None:
        """Synchronous helper that shuts down the Stata process.

        Called from :meth:`close` inside ``anyio.to_thread.run_sync`` so
        that the blocking pexpect I/O cannot stall the event loop.
        """
        proc = self._process
        if proc is None or not proc.isalive():
            return
        # Try a graceful exit first.
        try:
            proc.sendline("exit, clear")
            proc.expect(pexpect.EOF, timeout=10)
        except (pexpect.TIMEOUT, pexpect.EOF, OSError):
            pass
        # If still alive, kill the entire process group.
        if proc.isalive():
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate(force=True)

    @property
    def is_alive(self) -> bool:
        """Check whether the Stata process is still running."""
        if self._process is None:
            return False
        try:
            return self._process.isalive()
        except Exception:
            return False

    @property
    def tmpdir(self) -> Path:
        """Return the session's temporary directory."""
        return self._tmpdir

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
                    log.debug("Failed to clean up temp do-file: %s", do_file)

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

            # Append to log buffer (FIFO eviction to bound memory).
            self._log_buffer.append(cleaned)
            if len(self._log_buffer) > _MAX_LOG_BUFFER_ENTRIES:
                self._log_buffer = self._log_buffer[-_MAX_LOG_BUFFER_ENTRIES:]

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
        """Remove the echoed ``do`` command and surrounding noise.

        Stata echoes each command from a .do file with a leading ". ".
        We strip those echo lines but preserve output lines that happen
        to start with ". " by only removing lines that look like echoed
        Stata commands (". " followed by a known command token or blank).
        """
        do_file_stem = do_file.stem
        lines = output.splitlines()
        cleaned: list[str] = []
        skip_do_echo = True
        for line in lines:
            stripped = line.strip()
            # Skip the echoed "do ..." line and leading blank lines before it.
            if skip_do_echo:
                if stripped.startswith(f'do "{do_file}') or stripped.startswith("do "):
                    skip_do_echo = False
                    continue
                if stripped == "":
                    continue  # skip blank lines before the do-echo only
                # Non-blank, non-do line means Stata didn't echo; stop skipping.
                skip_do_echo = False
            if stripped == "end of do-file":
                continue
            # Skip continuation-prompt residue that references the .do file.
            if stripped.startswith("> ") and do_file_stem in stripped:
                continue
            # Skip echoed commands from the .do file.  Stata echoes each
            # command with a leading ". ".  Only strip lines that look like
            # actual command echoes (". " followed by non-numeric content)
            # to avoid eating output that starts with ". " (e.g. decimal
            # numbers or continuation lines).
            if stripped.startswith(". ") and len(stripped) > 2:
                after_dot = stripped[2:]
                # Echoed commands start with a letter, underscore, or known
                # Stata prefix (quietly, capture, noisily, etc.).  Numeric
                # output (like ".1234") or punctuation should be kept.
                if after_dot[:1].isalpha() or after_dot[:1] == "_":
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

    def send_interrupt(self) -> bool:
        """Batch sessions have no persistent process to interrupt.

        Returns ``False`` always.  Callers should check the return value
        and inform the user that cancellation is not supported in batch mode.
        """
        log.info("send_interrupt called on BatchSession %s (no-op)", self.session_id)
        return False

    @property
    def is_alive(self) -> bool:
        """Batch sessions are always 'alive' once started."""
        return self._started

    @property
    def tmpdir(self) -> Path:
        """Return the session's temporary directory."""
        return self._tmpdir

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

            # Append to log buffer (FIFO eviction to bound memory).
            self._log_buffer.append(cleaned)
            if len(self._log_buffer) > _MAX_LOG_BUFFER_ENTRIES:
                self._log_buffer = self._log_buffer[-_MAX_LOG_BUFFER_ENTRIES:]

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

        Uses Popen with start_new_session so the entire process group
        (including Stata-MP workers) can be killed on timeout.
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
# PipeSession (persistent fallback when a PTY cannot be allocated)
# ---------------------------------------------------------------------------


class PipeSession:
    """Persistent Stata session driven over stdin, with output read from a log.

    This is the preferred fallback when an interactive :class:`StataSession`
    cannot be created because the host denies PTY allocation (e.g. sandboxed
    launches where ``openpty`` raises ``PermissionError``), but a long-running
    child process is still allowed.

    Unlike :class:`BatchSession` — which starts a fresh ``stata -b`` process
    per call and therefore loses all in-memory state between calls — this keeps
    a *single* Stata process alive, so data, locals, scalars, matrices,
    programs and ``e()``/``r()`` results persist between tool calls, matching
    the behaviour users expect from an interactive session.

    How it works:

    1. ``stata -q`` is spawned with stdin connected to a pipe and stdout/stderr
       discarded.
    2. A named text *log* is opened in the session temp dir.  Stata
       block-buffers a piped stdout, but it flushes its log file promptly, so
       output is read from the **log file**, never the pipe.
    3. Each :meth:`execute` writes the code to a temporary ``.do`` file and
       sends ``do "<file>"`` bracketed by unique sentinel ``display`` commands.
       The call is complete once the closing sentinel appears in the log; the
       captured region is everything between the sentinels.  Running via ``do``
       (rather than feeding lines interactively) preserves do-file semantics:
       ``#delimit``, ``///`` continuation, comments, and error-aborts-the-file.
    """

    # Polling interval (seconds) when tailing the log file for completion.
    _POLL_INTERVAL = 0.05

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
        self._process: subprocess.Popen | None = None
        # Process-group id, captured at spawn while the child is guaranteed
        # alive.  Signalling this cached value avoids calling os.getpgid()
        # after the lead process may have been reaped (which would race with
        # the worker thread's proc.poll()).
        self._pgid: int | None = None
        self._log_file: Path = self._tmpdir / "_session.log"

    @staticmethod
    def _stata_quote(path: Path) -> str:
        """Wrap *path* in Stata compound double quotes (`"..."').

        Compound quotes let the path contain a literal ``"`` without breaking
        the surrounding string.  (They do NOT suppress ``$``/backtick macro
        expansion — but session temp dirs come from ``tempfile.mkdtemp`` and do
        not contain those.)
        """
        return '`"' + str(path) + '"\''

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the persistent Stata process and wait until it is ready."""
        try:
            await anyio.to_thread.run_sync(self._start_sync)
        except BaseException:
            # Startup failed, or was cancelled AFTER the worker thread already
            # spawned Stata (to_thread does not abandon the thread, so the
            # process can be live even though we were cancelled).  Kill it and
            # remove the eagerly-created temp dir so neither leaks, regardless
            # of which caller invoked start().
            self._kill_process()
            _cleanup_temp_dir(self._tmpdir)
            raise
        self._started = True
        log.info("PipeSession %s started (persistent pipe mode)", self.session_id)

    def _start_sync(self) -> None:
        stata_path = str(self.installation.path)
        try:
            self._process = subprocess.Popen(
                [stata_path, "-q"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(self._tmpdir),
                start_new_session=True,
                text=True,
                env={**os.environ, "TERM": "dumb"},
            )
            # start_new_session=True makes the child its own process-group
            # leader, so pgid == pid.  Capture it now while the child is alive.
            self._pgid = os.getpgid(self._process.pid)
            # Disable pagination (otherwise long output blocks forever waiting
            # on a "--more--" prompt nobody can answer), suppress GUI graph
            # windows, and open the capture log under a *named* log so it does
            # not collide with the user's own (unnamed) `log using` / `log
            # close` / `capture log close` usage inside their do-files.
            self._write_lines(
                "set more off",
                "set graphics off",
                f"log using {self._stata_quote(self._log_file)}, replace text name(_saf_capture)",
            )
            # Confirm the process booted and the log is live before returning.
            self._write_lines('display "<<<SAF_READY>>>"')
            deadline = time.monotonic() + _START_TIMEOUT
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError(
                        f"Stata exited during startup (code {self._process.returncode})"
                    )
                if "<<<SAF_READY>>>" in self._read_log_text():
                    return
                time.sleep(self._POLL_INTERVAL)
            raise TimeoutError("Stata pipe session did not become ready in time")
        except Exception:
            self._kill_process()
            raise

    def _write_lines(self, *lines: str) -> None:
        """Write one or more command lines to the child's stdin."""
        proc = self._process
        if proc is None or proc.stdin is None:
            raise BrokenPipeError("Stata process stdin is not available")
        proc.stdin.write("".join(f"{line}\n" for line in lines))
        proc.stdin.flush()

    def _read_log_text(self) -> str:
        """Read the entire capture log as decoded text (best effort)."""
        try:
            with open(self._log_file, "rb") as fh:
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _log_size(self) -> int:
        """Return the current byte size of the capture log (0 if missing)."""
        try:
            return self._log_file.stat().st_size
        except OSError:
            return 0

    @property
    def is_alive(self) -> bool:
        return (
            self._started
            and self._process is not None
            and self._process.poll() is None
        )

    @property
    def tmpdir(self) -> Path:
        """Return the session's temporary directory."""
        return self._tmpdir

    async def close(self) -> None:
        """Shut down the Stata process and clean up the temporary directory."""
        self._started = False
        await anyio.to_thread.run_sync(self._close_sync)
        _cleanup_temp_dir(self._tmpdir)
        log.info("PipeSession %s closed", self.session_id)

    def _close_sync(self) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.write("log close _all\nexit, clear\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                pass
        except Exception:
            log.debug("Error during PipeSession graceful close", exc_info=True)
        self._kill_group(proc)

    def _kill_process(self) -> None:
        """Force-kill the process group and reset state."""
        proc = self._process
        self._process = None
        self._started = False
        if proc is not None:
            self._kill_group(proc)
        self._pgid = None

    def _kill_group(self, proc: subprocess.Popen) -> None:
        """SIGKILL the whole process group (incl. Stata-MP workers), then reap.

        Uses the pgid captured at spawn rather than ``os.getpgid(proc.pid)``,
        which would fail (or race) once the lead process has been reaped.
        """
        pgid = self._pgid
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def send_interrupt(self) -> bool:
        """Send SIGINT to the Stata process group to abort the running command.

        Unlike :class:`BatchSession`, a persistent process *can* be
        interrupted, so this returns ``True`` when the signal is delivered.
        Stata aborts the current command and returns to its prompt; the
        session and the data in memory are preserved.

        Signals the pgid captured at spawn (never ``os.getpgid`` here), so it
        is safe to call from the event loop while a worker thread tails the
        log — it does not touch the (non-thread-safe) ``proc`` object.
        """
        pgid = self._pgid
        if self._process is not None and pgid is not None:
            try:
                os.killpg(pgid, signal.SIGINT)
                log.info("Sent interrupt to PipeSession %s", self.session_id)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                log.warning(
                    "Failed to interrupt PipeSession %s", self.session_id, exc_info=True
                )
        return False

    async def _ensure_alive(self) -> None:
        """Restart the process if it has died (in-memory state is then lost)."""
        if not self.is_alive:
            if self._started:
                log.warning(
                    "PipeSession %s process died; restarting (in-memory state lost)",
                    self.session_id,
                )
            self._kill_process()
            if not self._tmpdir.exists():
                self._tmpdir = _make_temp_dir()
                self._graph_cache = GraphCache(self._tmpdir)
            self._log_file = self._tmpdir / "_session.log"
            await self.start()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, code: str, timeout: int = _DEFAULT_TIMEOUT) -> ExecutionResult:
        """Execute Stata code in the persistent session and return the result.

        A per-session async lock serializes concurrent calls so only one
        command runs against the shared process at a time.
        """
        async with self._lock:
            await self._ensure_alive()

            # Inject graph export if the code draws a graph but has no export.
            code = maybe_inject_graph_export(code, self._tmpdir)

            # Snapshot graph files before execution.
            self._graph_cache.take_snapshot()

            start_time = time.monotonic()

            marker = uuid.uuid4().hex[:12]
            start_s = f"<<<SAF_S_{marker}>>>"
            end_s = f"<<<SAF_E_{marker}>>>"
            do_file = self._tmpdir / f"_cmd_{marker}.do"
            do_file.write_text(code, encoding="utf-8")

            try:
                raw = await anyio.to_thread.run_sync(
                    lambda: self._run_and_capture(do_file, start_s, end_s, timeout),
                    # Do NOT abandon on cancel: the lock protects the shared
                    # process, and the deadline in _run_and_capture bounds the
                    # wait.  Abandoning would release the lock with a thread
                    # still tailing the log.
                )
            except TimeoutError:
                elapsed = time.monotonic() - start_time
                # Abort the stuck command but keep the session (and its data).
                self.send_interrupt()
                hint = (
                    f"Command timed out after {timeout}s. The running command was "
                    "interrupted; the session and data in memory are preserved.\n"
                    "  • Increase the timeout parameter, or\n"
                    "  • use the run_do_file tool for long-running models."
                )
                return ExecutionResult(
                    output=hint,
                    return_code=1,
                    error_message=hint,
                    error_code=None,
                    graphs=[],
                    execution_time=elapsed,
                    log_path=self._log_file if self._log_file.exists() else None,
                )
            except (BrokenPipeError, OSError, ValueError) as exc:
                elapsed = time.monotonic() - start_time
                self._kill_process()
                return ExecutionResult(
                    output="",
                    return_code=1,
                    error_message=f"Stata process terminated unexpectedly: {exc}",
                    error_code=None,
                    graphs=[],
                    execution_time=elapsed,
                    log_path=None,
                )
            finally:
                try:
                    do_file.unlink(missing_ok=True)
                except OSError:
                    log.debug("Failed to remove temp do-file %s", do_file)

            elapsed = time.monotonic() - start_time

            # Strip SMCL, then reuse StataSession's do-output cleaner (same
            # echo/`end of do-file` stripping the interactive path uses).
            cleaned = strip_smcl(raw)
            cleaned = StataSession._clean_do_output(cleaned, do_file)

            error_message, error_code = _detect_error(cleaned)
            return_code = 0 if error_code is None and error_message is None else 1

            graphs = self._graph_cache.detect_changes()

            self._log_buffer.append(cleaned)
            if len(self._log_buffer) > _MAX_LOG_BUFFER_ENTRIES:
                self._log_buffer = self._log_buffer[-_MAX_LOG_BUFFER_ENTRIES:]

            log.debug(
                "PipeSession %s execute completed in %.2fs (rc=%d, graphs=%d)",
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
                log_path=self._log_file if self._log_file.exists() else None,
            )

    def _run_and_capture(
        self, do_file: Path, start_s: str, end_s: str, timeout: int
    ) -> str:
        """Send the bracketed ``do`` and tail the log until the end sentinel.

        Runs in a worker thread.  Raises :class:`TimeoutError` if the closing
        sentinel does not appear within *timeout* seconds, or
        :class:`BrokenPipeError` if the process exits first.
        """
        proc = self._process
        if proc is None or proc.stdin is None:
            raise BrokenPipeError("Stata process is not running")

        offset = self._log_size()
        # Re-assert `set more off` before each command (it is session-global
        # mutable state a prior command could have flipped back on, which would
        # otherwise block on a "--more--" prompt).  It precedes the opening
        # sentinel, so it never appears in the captured region.
        self._write_lines(
            "set more off",
            f'display "{start_s}"',
            f"do {self._stata_quote(do_file)}",
            f'display "{end_s}"',
        )

        end_bytes = end_s.encode("utf-8")
        deadline = time.monotonic() + timeout
        buf = b""
        with open(self._log_file, "rb") as fh:
            fh.seek(offset)
            while True:
                chunk = fh.read()
                if chunk:
                    buf += chunk
                    if end_bytes in buf:
                        break
                    continue
                if proc.poll() is not None:
                    raise BrokenPipeError(
                        f"Stata exited unexpectedly (code {proc.returncode})"
                    )
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Total execution timeout exceeded ({timeout}s)")
                time.sleep(self._POLL_INTERVAL)

        return self._extract_region(buf.decode("utf-8", errors="replace"), start_s, end_s)

    @staticmethod
    def _extract_region(text: str, start_s: str, end_s: str) -> str:
        """Return the log text strictly between the two sentinels.

        Boundaries are the opening sentinel's *value* line (its last
        occurrence) and the closing sentinel's *command-echo* line (its first
        occurrence), so the sentinel lines themselves are excluded.
        """
        s = text.rfind(start_s)
        if s == -1:
            region_start = 0
        else:
            nl = text.find("\n", s)
            region_start = nl + 1 if nl != -1 else len(text)
        e = text.find(end_s)
        if e == -1:
            region_end = len(text)
        else:
            ls = text.rfind("\n", 0, e)
            region_end = ls if ls != -1 else 0
        if region_end < region_start:
            return ""
        return text[region_start:region_end]

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
        self._sessions: dict[str, StataSession | BatchSession | PipeSession] = {}
        self._lock: anyio.Lock = anyio.Lock()
        self.installation = installation
        self._session_timeout = session_timeout
        self._last_activity: dict[str, float] = {}
        if use_batch is None:
            self._use_batch = not HAS_PEXPECT
        else:
            self._use_batch = use_batch
        # Set once an interactive PTY is found to be unavailable but a
        # persistent pipe session works — avoids retrying pexpect every time.
        self._use_pipe = False
        # session_id -> Event, marking a creation in progress.  Lets concurrent
        # callers for the same id wait instead of double-creating, while the
        # (slow) start() runs OUTSIDE the manager lock.
        self._creating: dict[str, anyio.Event] = {}
        # Bumped by close_all()/close_session().  A creator that ran start()
        # outside the lock checks this before publishing: if it changed, a
        # concurrent close raced the creation, so the new session is closed
        # instead of stored (otherwise a live Stata process would be orphaned).
        self._generation = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_create(
        self,
        session_id: str = "default",
    ) -> StataSession | BatchSession | PipeSession:
        """Return an existing session or create and start a new one.

        A manager-level lock prevents TOCTOU races where concurrent
        callers with the same *session_id* could both create sessions.

        Parameters
        ----------
        session_id:
            A name that uniquely identifies the session.

        Returns
        -------
        StataSession | BatchSession | PipeSession
        """
        while True:
            to_close: list[tuple[str, StataSession | BatchSession | PipeSession]] = []
            wait_event: anyio.Event | None = None
            creator = False
            gen_at_start = 0
            result: StataSession | BatchSession | PipeSession | None = None

            async with self._lock:
                # Collect idle/dead sessions to close *after* releasing the lock
                # (closing can block for seconds; holding the lock would stall
                # every other manager operation, incl. the cancel tool).
                now = time.monotonic()
                for sid in [
                    s
                    for s, last in self._last_activity.items()
                    if now - last > self._session_timeout and s in self._sessions
                ]:
                    to_close.append((sid, self._sessions.pop(sid)))
                    self._last_activity.pop(sid, None)
                    log.info("Session %s expired after %ds idle", sid, self._session_timeout)

                existing = self._sessions.get(session_id)
                if existing is not None and not existing.is_alive:
                    log.warning("Session %s is dead; removing and re-creating", session_id)
                    to_close.append((session_id, self._sessions.pop(session_id)))
                    self._last_activity.pop(session_id, None)
                    existing = None

                if existing is not None:
                    self._last_activity[session_id] = time.monotonic()
                    result = existing
                elif session_id in self._creating:
                    # Another caller is already starting this id — wait for it.
                    wait_event = self._creating[session_id]
                else:
                    # We will create it; publish an event so concurrent callers
                    # wait rather than double-create.
                    self._creating[session_id] = anyio.Event()
                    creator = True
                    gen_at_start = self._generation

            # ---- outside the manager lock ----
            if not creator:
                # Cache hit, or another caller is creating this id.
                await self._close_sessions(to_close)
                if result is not None:
                    return result
                assert wait_event is not None
                await wait_event.wait()
                continue

            # We are the creator.  From here self._creating[session_id] is
            # registered: its Event MUST be popped+set on EVERY exit (return,
            # exception, cancellation) or waiters block forever.  The bookkeeping
            # runs in a cancellation-shielded scope because acquiring the lock is
            # itself a cancellation checkpoint.
            session: StataSession | BatchSession | PipeSession | None = None
            raced_close = False
            try:
                await self._close_sessions(to_close)
                session = await self._create_started_session(session_id)
            finally:
                with anyio.CancelScope(shield=True):
                    async with self._lock:
                        ev = self._creating.pop(session_id, None)
                        if self._generation != gen_at_start:
                            # A close_all()/close_session() ran while we were
                            # creating outside the lock.  Do NOT store, or a live
                            # Stata process the close() never saw would orphan.
                            raced_close = True
                        elif session is not None:
                            self._sessions[session_id] = session
                            self._last_activity[session_id] = time.monotonic()
                    if ev is not None:
                        ev.set()
                    if raced_close and session is not None:
                        try:
                            await session.close()
                        except Exception:
                            log.warning(
                                "Error closing raced session %s", session_id, exc_info=True
                            )
                        session = None

            if raced_close:
                # A concurrent close won the race; re-evaluate from scratch.
                continue
            return session

    async def _close_sessions(
        self, sessions: list[tuple[str, StataSession | BatchSession | PipeSession]]
    ) -> None:
        """Close the given (id, session) pairs, logging but not raising."""
        for sid, session in sessions:
            try:
                await session.close()
            except Exception:
                log.warning("Error closing session %s", sid, exc_info=True)

    async def _create_started_session(
        self, session_id: str
    ) -> StataSession | BatchSession | PipeSession:
        """Create and start a session in the active mode, with fallback.

        Runs *outside* the manager lock so a slow Stata boot never blocks other
        manager operations (``get_session`` — used by the cancel tool —
        ``close_session``, ``list_sessions``, and other creations).
        """
        mode = "batch" if self._use_batch else "pipe" if self._use_pipe else "interactive"
        log.info("Creating new %s session: %s", mode, session_id)

        if self._use_batch:
            session: StataSession | BatchSession | PipeSession = BatchSession(
                self.installation, session_id=session_id
            )
            await session.start()
            return session
        if self._use_pipe:
            session = PipeSession(self.installation, session_id=session_id)
            await session.start()
            return session

        session = StataSession(self.installation, session_id=session_id)
        try:
            await session.start()
            return session
        except PermissionError:
            # macOS / sandboxed launches may deny PTY creation.  StataSession.start()
            # has already killed any process and removed its own temp dir, so we just
            # fall back to a *persistent pipe* session (state still persists between
            # calls); only if that also fails do we drop to stateless batch.
            log.warning(
                "Interactive PTY denied for session %s; trying persistent pipe mode",
                session_id,
            )

        try:
            session = PipeSession(self.installation, session_id=session_id)
            await session.start()
            self._use_pipe = True  # avoid retrying pexpect for later sessions
            log.info(
                "Using persistent pipe-mode sessions: state persists between "
                "calls without a PTY"
            )
            return session
        except Exception as exc:
            # PipeSession.start() already removed its own temp dir on failure.
            log.warning(
                "Pipe mode failed (%s); falling back to stateless batch mode for "
                "session %s",
                exc,
                session_id,
            )
            session = BatchSession(self.installation, session_id=session_id)
            await session.start()
            self._use_batch = True
            return session

    async def get_session(
        self, session_id: str
    ) -> StataSession | BatchSession | PipeSession | None:
        """Return an existing session without creating a new one.

        Returns ``None`` if the session does not exist.  This is safe to
        call even while another command is running (it only acquires the
        manager lock briefly to read the dict).
        """
        async with self._lock:
            return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> None:
        """Close and remove a single session by ID.

        Silently ignores unknown session IDs.
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._last_activity.pop(session_id, None)
            # Signal any in-flight creator (running start() outside the lock)
            # that a close raced it, so it discards rather than orphans.
            self._generation += 1
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
            if isinstance(session, BatchSession):
                session_type = "batch"
            elif isinstance(session, PipeSession):
                session_type = "pipe"
            else:
                session_type = "interactive"
            result.append(
                {
                    "session_id": sid,
                    "alive": session.is_alive,
                    "type": session_type,
                }
            )
        return result

    async def close_all(self) -> None:
        """Close every tracked session."""
        async with self._lock:
            sessions_to_close = dict(self._sessions)
            self._sessions.clear()
            self._last_activity.clear()
            # Signal in-flight creators (running start() outside the lock) that
            # a close raced them, so the new session is discarded, not orphaned.
            self._generation += 1

        for sid, session in sessions_to_close.items():
            try:
                await session.close()
                log.info("Session %s closed during close_all", sid)
            except Exception:
                log.warning("Error closing session %s during close_all", sid, exc_info=True)
        log.info("All sessions closed")
