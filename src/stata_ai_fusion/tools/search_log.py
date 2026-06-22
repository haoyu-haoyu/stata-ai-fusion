"""Search log tool.

Provides the ``stata_search_log`` MCP tool that searches through the
accumulated session log for matching lines.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import anyio
from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_search_log"

# Guards against a user-supplied regex causing catastrophic backtracking
# (ReDoS): the match loop runs in a worker thread bounded by this timeout, so a
# pathological pattern can never block the event loop and stall other sessions.
_MAX_QUERY_LEN = 1000
_SEARCH_TIMEOUT = 5.0  # seconds
_MAX_CONTEXT_LINES = 50

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Search through the session's accumulated output log for matching lines. "
        "Useful for finding specific results, error messages, or command output "
        "from earlier in the session. Supports plain text and regex search."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search string or regex pattern.",
            },
            "regex": {
                "type": "boolean",
                "description": "Treat query as a regex pattern. Default false.",
                "default": False,
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search. Default false.",
                "default": False,
            },
            "context_lines": {
                "type": "integer",
                "description": "Lines of context before and after each match. Default 2.",
                "default": 2,
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier. Default 'default'.",
                "default": "default",
            },
        },
        "required": ["query"],
    },
)


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Search the session log for matching lines and return results."""
    query: str = arguments.get("query", "")
    use_regex: bool = arguments.get("regex", False)
    case_sensitive: bool = arguments.get("case_sensitive", False)
    context_lines: int = max(0, min(int(arguments.get("context_lines", 2)), _MAX_CONTEXT_LINES))
    session_id: str = arguments.get("session_id", "default")

    if not query.strip():
        return [TextContent(type="text", text="Error: no search query provided.")]

    if len(query) > _MAX_QUERY_LEN:
        return [
            TextContent(
                type="text",
                text=f"Error: query too long (max {_MAX_QUERY_LEN} characters).",
            )
        ]

    session = await session_manager.get_session(session_id)
    if session is None:
        return [TextContent(type="text", text=f"No active session '{session_id}'.")]

    full_log = session.get_log()
    if not full_log.strip():
        return [TextContent(type="text", text="(session log is empty)")]

    lines = full_log.splitlines()

    # Build the search function
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        if use_regex:
            pattern = re.compile(query, flags)
        else:
            pattern = re.compile(re.escape(query), flags)
    except re.error as exc:
        return [TextContent(type="text", text=f"Invalid regex pattern: {exc}")]

    # Find matching line indices in a worker thread bounded by a timeout, so a
    # pathological regex (catastrophic backtracking) frees the event loop and
    # never stalls other sessions.  (On timeout the worker is abandoned and may
    # keep running until the regex completes — an accepted, low-severity CPU
    # residual for this local single-client server; a thread cannot be killed.)
    def _find_matches() -> list[int]:
        return [i for i, line in enumerate(lines) if pattern.search(line)]

    try:
        with anyio.fail_after(_SEARCH_TIMEOUT):
            match_indices = await anyio.to_thread.run_sync(
                _find_matches, abandon_on_cancel=True
            )
    except TimeoutError:
        return [
            TextContent(
                type="text",
                text=(
                    f"Error: search timed out after {_SEARCH_TIMEOUT:.0f}s — the "
                    "regex may be too complex (catastrophic backtracking). "
                    "Simplify the pattern."
                ),
            )
        ]

    if not match_indices:
        return [TextContent(type="text", text=f"No matches found for: {query}")]

    # Build output with context
    output_parts: list[str] = []
    shown: set[int] = set()

    for idx in match_indices:
        start = max(0, idx - context_lines)
        end = min(len(lines) - 1, idx + context_lines)

        if shown and start > max(shown) + 1:
            output_parts.append("---")

        for i in range(start, end + 1):
            if i not in shown:
                marker = ">>>" if i == idx else "   "
                output_parts.append(f"{marker} {i + 1:>5}: {lines[i]}")
                shown.add(i)

    header = f"Found {len(match_indices)} match(es) for: {query}\n"
    return [TextContent(type="text", text=header + "\n".join(output_parts))]
