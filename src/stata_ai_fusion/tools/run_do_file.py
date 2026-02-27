"""Run .do file tool — batch-mode implementation.

Provides the ``stata_run_do_file`` MCP tool that executes an existing Stata
do-file in **batch mode** (``subprocess.Popen``) rather than through the
interactive pexpect session.  Batch mode is more reliable for long-running
scripts (bootstrap, mixed models, simulations) because it avoids the
prompt-matching heuristics of pexpect.

Text output is read from the ``.log`` file Stata produces; graphs are
detected via the :class:`GraphCache` snapshot-diff mechanism.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import anyio
from mcp.types import ImageContent, TextContent, Tool

from ..graph_cache import GraphCache
from ..stata_session import _detect_error, strip_smcl

log = logging.getLogger(__name__)

TOOL_NAME = "stata_run_do_file"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Execute a Stata .do file by its full path. The file must exist and "
        "have a .do extension. Returns the text output and any graphs produced."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the .do file.",
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier. Default 'default'.",
                "default": "default",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum seconds to wait. Default 300.",
                "default": 300,
            },
        },
        "required": ["path"],
    },
)

# Maximum characters of Stata log output to return to the AI.
_MAX_OUTPUT_CHARS = 30_000


async def handle(
    session_manager,  # noqa: ANN001
    arguments: dict,
) -> list[TextContent | ImageContent]:
    """Execute a Stata .do file in batch mode and return content blocks."""
    raw_path: str = arguments.get("path", "")
    session_id: str = arguments.get("session_id", "default")
    timeout: int = arguments.get("timeout", 300)

    # ---- Input validation ------------------------------------------------

    if not raw_path.strip():
        return [TextContent(type="text", text="Error: no file path provided.")]

    do_path = Path(raw_path).expanduser().resolve()

    if do_path.suffix.lower() != ".do":
        return [
            TextContent(
                type="text",
                text=f"Error: file must have a .do extension, got '{do_path.suffix}'.",
            )
        ]

    if not do_path.is_file():
        return [
            TextContent(
                type="text",
                text=f"Error: file not found: {do_path}",
            )
        ]

    # ---- Resolve Stata path ---------------------------------------------

    stata_path = str(session_manager.installation.path)

    # ---- Prepare working directory & graph cache -------------------------

    working_directory = do_path.parent
    graph_cache = GraphCache(working_directory)
    graph_cache.take_snapshot()

    # The .log file Stata creates has the same stem as the .do file.
    log_file = working_directory / f"{do_path.stem}.log"

    # Remove stale log from a previous run to avoid reading old output.
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass

    # ---- Run in batch mode -----------------------------------------------

    start_time = time.monotonic()

    try:
        def _run_batch() -> int:
            proc = subprocess.Popen(
                [stata_path, "-b", "do", str(do_path)],
                cwd=str(working_directory),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the entire process group.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
                raise
            return proc.returncode

        await anyio.to_thread.run_sync(_run_batch)

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start_time
        # Try to read partial log output even after timeout.
        partial = ""
        if log_file.exists():
            try:
                partial = log_file.read_text(encoding="utf-8", errors="replace")
                partial = strip_smcl(partial)
                if len(partial) > _MAX_OUTPUT_CHARS:
                    partial = partial[:_MAX_OUTPUT_CHARS] + "\n\n... (truncated)"
            except Exception:
                pass
        msg = f"Do-file timed out after {timeout}s."
        output = f"{msg}\n\n--- partial output ---\n{partial}" if partial else msg
        return [TextContent(type="text", text=output)]

    except Exception as exc:
        log.error("Batch execution error for %s: %s", do_path, exc)
        return [TextContent(type="text", text=f"Execution error: {exc}")]

    elapsed = time.monotonic() - start_time

    # ---- Read log file ---------------------------------------------------

    raw_output = ""
    if log_file.exists():
        try:
            raw_output = log_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            log.warning("Could not read log file %s: %s", log_file, exc)

    cleaned = strip_smcl(raw_output)

    # Truncate very large output.
    if len(cleaned) > _MAX_OUTPUT_CHARS:
        cleaned = cleaned[:_MAX_OUTPUT_CHARS] + "\n\n... (output truncated at 30,000 chars)"

    # ---- Error detection -------------------------------------------------

    error_message, error_code = _detect_error(cleaned)

    # ---- Graph detection -------------------------------------------------

    graphs = graph_cache.detect_changes()

    # ---- Build response --------------------------------------------------

    contents: list[TextContent | ImageContent] = []

    # Metadata header
    meta_line = f"[batch mode | {elapsed:.1f}s | session_id={session_id}]"
    output_text = f"{meta_line}\n\n{cleaned}" if cleaned.strip() else meta_line

    if error_message:
        output_text += f"\n\n--- Stata Error ---\n{error_message}"
        if error_code is not None:
            output_text += f" [r({error_code})]"

    if output_text.strip():
        contents.append(TextContent(type="text", text=output_text.strip()))

    # Graph images
    for graph in graphs:
        mime = f"image/{graph.format}" if graph.format != "pdf" else "application/pdf"
        contents.append(
            ImageContent(
                type="image",
                data=graph.base64,
                mimeType=mime,
            )
        )

    if not contents:
        contents.append(TextContent(type="text", text="(no output)"))

    return contents
