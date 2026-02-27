"""Search log tool.

Provides the ``stata_search_log`` MCP tool that searches through the
accumulated session log for matching lines.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_search_log"

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
    context_lines: int = arguments.get("context_lines", 2)
    session_id: str = arguments.get("session_id", "default")

    if not query.strip():
        return [TextContent(type="text", text="Error: no search query provided.")]

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

    # Find matching line indices
    match_indices: list[int] = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            match_indices.append(i)

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
