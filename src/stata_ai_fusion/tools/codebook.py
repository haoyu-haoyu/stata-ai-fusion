"""Codebook tool.

Provides the ``stata_codebook`` MCP tool that generates a compact codebook
for one or more variables in the current dataset.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_codebook"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Generate a compact codebook for variables in the current dataset. "
        "Shows variable types, labels, unique values, missing counts, and "
        "example values. If no variables are specified, a codebook for the "
        "entire dataset is produced."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "variables": {
                "type": "string",
                "description": (
                    "Space-separated variable names, or omit for all variables. "
                    "Example: 'price mpg weight'."
                ),
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier. Default 'default'.",
                "default": "default",
            },
        },
    },
)


# Allowed characters in variable names: alphanumerics, underscores, spaces,
# wildcards (* ?), and hyphens (for variable ranges like var1-var10).
_VALID_VARNAMES_RE = re.compile(r"^[A-Za-z0-9_ *?\-]+$")


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Generate and return a compact codebook."""
    variables: str | None = arguments.get("variables")
    session_id: str = arguments.get("session_id", "default")

    try:
        session = await session_manager.get_or_create(session_id)
    except Exception as exc:
        log.error("Failed to get/create session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Error creating session: {exc}")]

    if variables and variables.strip():
        if not _VALID_VARNAMES_RE.fullmatch(variables.strip()):
            return [
                TextContent(
                    type="text",
                    text="Error: variable names must contain only letters, digits, "
                    "underscores, spaces, hyphens, and wildcards (* ?).",
                )
            ]
        code = f"codebook {variables.strip()}, compact"
    else:
        code = "codebook, compact"

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
        output_text = "(no data in memory or no matching variables)"

    return [TextContent(type="text", text=output_text.strip())]
