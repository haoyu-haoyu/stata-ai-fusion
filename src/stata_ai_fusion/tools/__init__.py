"""MCP tools for Stata AI Fusion.

This package exposes all Stata MCP tools and provides
:func:`register_all_tools` to wire them into the MCP server.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.types import ImageContent, TextContent, Tool

from . import (
    cancel_command,
    codebook,
    export_graph,
    get_results,
    inspect_data,
    install_package,
    run_command,
    run_do_file,
    search_log,
)

if TYPE_CHECKING:
    from mcp.server import Server

    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool registry — maps tool name to (TOOL_DEF, handle function)
# ---------------------------------------------------------------------------

_TOOL_MODULES = [
    run_command,
    run_do_file,
    inspect_data,
    codebook,
    get_results,
    export_graph,
    search_log,
    install_package,
    cancel_command,
]

_TOOL_REGISTRY: dict[str, tuple[Tool, object]] = {}
for _mod in _TOOL_MODULES:
    _TOOL_REGISTRY[_mod.TOOL_NAME] = (_mod.TOOL_DEF, _mod.handle)


def get_all_tool_definitions() -> list[Tool]:
    """Return MCP Tool definitions for all registered tools."""
    return [defn for defn, _ in _TOOL_REGISTRY.values()]


async def dispatch_tool(
    tool_name: str,
    arguments: dict,
    session_manager: SessionManager,
) -> list[TextContent | ImageContent]:
    """Dispatch a tool call to the appropriate handler.

    Returns a list of MCP content blocks. If the tool name is unknown,
    returns an error TextContent.
    """
    entry = _TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return [TextContent(type="text", text=f"Unknown tool: {tool_name}")]

    _, handler = entry
    try:
        return await handler(session_manager, arguments)
    except Exception as exc:
        log.error("Unhandled error in tool %s: %s", tool_name, exc, exc_info=True)
        return [TextContent(type="text", text=f"Internal error in {tool_name}: {exc}")]


def register_all_tools(server: Server, session_manager: SessionManager) -> None:
    """Register all Stata tools with the MCP *server*.

    This sets up the ``list_tools`` and ``call_tool`` handlers. Two
    additional lightweight tools (``stata_list_sessions`` and
    ``stata_close_session``) are also registered here.
    """

    # ----- Extra session-management tool definitions ----------------------

    list_sessions_def = Tool(
        name="stata_list_sessions",
        description=(
            "List all active Stata sessions with their IDs, types "
            "(interactive vs batch), and alive status."
        ),
        inputSchema={"type": "object", "properties": {}},
    )

    close_session_def = Tool(
        name="stata_close_session",
        description=(
            "Close a Stata session by its ID and release resources. "
            "Use 'default' to close the default session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to close.",
                },
            },
            "required": ["session_id"],
        },
    )

    # ----- list_tools handler ---------------------------------------------

    all_tool_defs = get_all_tool_definitions() + [list_sessions_def, close_session_def]

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return all_tool_defs

    # ----- call_tool handler ----------------------------------------------

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
        # Handle the two session-management tools directly
        if name == "stata_list_sessions":
            return await _handle_list_sessions(session_manager)
        if name == "stata_close_session":
            return await _handle_close_session(session_manager, arguments)

        # Dispatch to the tool registry
        return await dispatch_tool(name, arguments, session_manager)


async def _handle_list_sessions(
    session_manager: SessionManager,
) -> list[TextContent]:
    """Handle the ``stata_list_sessions`` tool call."""
    try:
        sessions = await session_manager.list_sessions()
    except Exception as exc:
        return [TextContent(type="text", text=f"Error listing sessions: {exc}")]

    if not sessions:
        return [TextContent(type="text", text="No active sessions.")]

    lines: list[str] = ["Active Stata sessions:", ""]
    for s in sessions:
        status = "alive" if s["alive"] else "dead"
        lines.append(f"  - {s['session_id']} ({s['type']}, {status})")
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_close_session(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Handle the ``stata_close_session`` tool call."""
    session_id: str = arguments.get("session_id", "")
    if not session_id.strip():
        return [TextContent(type="text", text="Error: no session_id provided.")]

    try:
        await session_manager.close_session(session_id.strip())
    except Exception as exc:
        return [TextContent(type="text", text=f"Error closing session: {exc}")]

    return [TextContent(type="text", text=f"Session '{session_id}' closed.")]
