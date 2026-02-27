"""Run Stata commands tool.

Provides the ``stata_run_command`` MCP tool that executes arbitrary Stata code
in a managed session, returning text output and any generated graph images.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.types import ImageContent, TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_run_command"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Execute one or more Stata commands interactively. Returns text output "
        "(SMCL stripped) and any graphs as inline images. Best for short ad-hoc "
        "commands: loading data, running regressions, generating tables, etc. "
        "For complete .do files or long-running models (mixed, bootstrap, "
        "simulate), use stata_run_do_file instead — it runs in batch mode "
        "and handles long execution times reliably without timeout risk."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Stata commands to execute (may span multiple lines).",
            },
            "echo": {
                "type": "boolean",
                "description": "Whether to echo commands in output. Default true.",
                "default": True,
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier. Default 'default'.",
                "default": "default",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum seconds to wait. Default 120.",
                "default": 120,
            },
        },
        "required": ["code"],
    },
)


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent | ImageContent]:
    """Execute Stata commands and return content blocks."""
    code: str = arguments.get("code", "")
    echo: bool = arguments.get("echo", True)
    session_id: str = arguments.get("session_id", "default")
    timeout: int = max(1, min(int(arguments.get("timeout", 120)), 3600))

    if not code.strip():
        return [TextContent(type="text", text="Error: no code provided.")]

    try:
        session = await session_manager.get_or_create(session_id)
    except Exception as exc:
        log.error("Failed to get/create session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Error creating session: {exc}")]

    if not echo:
        code = f"set output inform\n{code}"

    try:
        result = await session.execute(code, timeout=timeout)
    except Exception as exc:
        log.error("Execution error in session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Execution error: {exc}")]

    contents: list[TextContent | ImageContent] = []

    # Text output
    output_text = result.output or ""
    if result.error_message:
        output_text += f"\n\n--- Stata Error ---\n{result.error_message}"
        if result.error_code is not None:
            output_text += f" [r({result.error_code})]"
    if output_text.strip():
        contents.append(TextContent(type="text", text=output_text.strip()))

    # Graph images
    for graph in result.graphs:
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
