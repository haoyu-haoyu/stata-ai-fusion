"""Inspect data tool.

Provides the ``stata_inspect_data`` MCP tool that returns a comprehensive
overview of the dataset currently loaded in a Stata session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_inspect_data"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Get an overview of the dataset currently in memory. Runs `describe`, "
        "`summarize`, and reports the observation count. Use this to understand "
        "the structure, variable types, labels, and basic summary statistics of "
        "your data before running analyses."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session identifier. Default 'default'.",
                "default": "default",
            },
        },
    },
)


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Inspect the currently loaded dataset and return an overview."""
    session_id: str = arguments.get("session_id", "default")

    try:
        session = await session_manager.get_or_create(session_id)
    except Exception as exc:
        log.error("Failed to get/create session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Error creating session: {exc}")]

    code = "\n".join(
        [
            "describe, short",
            "describe",
            "summarize",
            "display _N",
        ]
    )

    try:
        result = await session.execute(code, timeout=60)
    except Exception as exc:
        log.error("Execution error in session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Execution error: {exc}")]

    output_text = result.output or ""
    if result.error_message:
        output_text += f"\n\n--- Stata Error ---\n{result.error_message}"
        if result.error_code is not None:
            output_text += f" [r({result.error_code})]"

    if not output_text.strip():
        output_text = "(no data in memory)"

    return [TextContent(type="text", text=output_text.strip())]
