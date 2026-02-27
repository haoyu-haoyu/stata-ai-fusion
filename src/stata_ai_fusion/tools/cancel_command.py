"""Cancel command tool.

Provides the ``stata_cancel_command`` MCP tool that sends SIGINT (Ctrl-C) to
the running Stata process, aborting the current command without destroying the
session.  This is the preferred way to stop a long-running interactive command
(e.g. bootstrap, simulate) while keeping the data in memory.
"""

from __future__ import annotations

import logging

from mcp.types import TextContent, Tool

log = logging.getLogger(__name__)

TOOL_NAME = "stata_cancel_command"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Send Ctrl-C (SIGINT) to cancel the currently running Stata command "
        "without killing the session. The dataset in memory is preserved. "
        "Only works for interactive sessions."
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
    session_manager,  # noqa: ANN001
    arguments: dict,
) -> list[TextContent]:
    """Send an interrupt (SIGINT) to the Stata session."""
    session_id: str = arguments.get("session_id", "default")

    # Look up the session *without* creating a new one or blocking on the
    # session lock (get_or_create would block if a command is running).
    session = await session_manager.get_session(session_id)

    if session is None:
        return [
            TextContent(
                type="text",
                text=f"No active session with id '{session_id}'.",
            )
        ]

    # Both session types now have send_interrupt, but BatchSession always
    # returns False.  Check the type explicitly for a clearer message.
    from ..stata_session import BatchSession

    if isinstance(session, BatchSession):
        return [
            TextContent(
                type="text",
                text="Cancel is only supported for interactive sessions, "
                "not batch sessions.",
            )
        ]

    sent = session.send_interrupt()
    if sent:
        return [
            TextContent(
                type="text",
                text=f"Interrupt sent to session '{session_id}'. "
                "Stata will abort the current command. The session and "
                "data in memory are preserved.",
            )
        ]
    return [
        TextContent(
            type="text",
            text=f"Session '{session_id}' has no running process to interrupt.",
        )
    ]
