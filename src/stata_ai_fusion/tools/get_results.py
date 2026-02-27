"""Get results tool.

Provides the ``stata_get_results`` MCP tool that extracts stored results
(scalars, macros, matrices) from the r(), e(), or c() namespaces after
running a Stata command.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mcp.types import TextContent, Tool

from ..result_extractor import ResultExtractor

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_get_results"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Extract stored results from Stata's r(), e(), or c() namespaces. "
        "After running a command like `regress` or `summarize`, use this tool "
        "to retrieve coefficients, standard errors, R-squared, observation "
        "counts, and other stored results as structured JSON."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "result_class": {
                "type": "string",
                "enum": ["r", "e", "c"],
                "description": (
                    "Result namespace: 'r' for r-class commands (summarize, etc.), "
                    "'e' for estimation results (regress, etc.), "
                    "'c' for system constants."
                ),
                "default": "e",
            },
            "keys": {
                "type": "string",
                "description": (
                    "Comma-separated result names to retrieve (e.g. 'r2,N,rmse'). "
                    "If omitted, returns all available results."
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


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Extract and return Stata stored results as JSON."""
    result_class: str = arguments.get("result_class", "e")
    keys: str | None = arguments.get("keys")
    session_id: str = arguments.get("session_id", "default")

    try:
        session = await session_manager.get_or_create(session_id)
    except Exception as exc:
        log.error("Failed to get/create session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Error creating session: {exc}")]

    extractor = ResultExtractor(session)

    try:
        if keys and keys.strip():
            # Retrieve specific keys
            key_list = [k.strip() for k in keys.split(",") if k.strip()]
            results: dict[str, float | str | None] = {}
            for key in key_list:
                value = await extractor.get_scalar(key, result_class=result_class)
                results[key] = value
        else:
            # Retrieve all results
            results = await extractor.get_all(result_class=result_class)
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]
    except Exception as exc:
        log.error("Error extracting results: %s", exc)
        return [TextContent(type="text", text=f"Error extracting results: {exc}")]

    formatted = json.dumps(results, indent=2, default=str)
    return [TextContent(type="text", text=formatted)]
