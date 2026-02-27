"""Export graph tool.

Provides the ``stata_export_graph`` MCP tool that exports the current
in-memory Stata graph to a file in the specified format.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from typing import TYPE_CHECKING

from mcp.types import ImageContent, TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_export_graph"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Export the current Stata graph to a file. Supports png, pdf, and svg "
        "formats. Returns the graph as an inline image and reports the file path."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["png", "pdf", "svg"],
                "description": "Output format. Default 'png'.",
                "default": "png",
            },
            "width": {
                "type": "integer",
                "description": "Width in pixels (png) or points (pdf/svg). Default 2000.",
                "default": 2000,
            },
            "filename": {
                "type": "string",
                "description": (
                    "Output filename (without extension). If omitted, an "
                    "auto-generated timestamped name is used."
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
) -> list[TextContent | ImageContent]:
    """Export the current Stata graph and return it as an image."""
    fmt: str = arguments.get("format", "png")
    width: int = arguments.get("width", 2000)
    filename: str | None = arguments.get("filename")
    session_id: str = arguments.get("session_id", "default")

    if fmt not in ("png", "pdf", "svg"):
        return [TextContent(type="text", text=f"Error: unsupported format '{fmt}'.")]

    if not (100 <= width <= 10000):
        return [
            TextContent(
                type="text",
                text="Error: width must be between 100 and 10000.",
            )
        ]

    try:
        session = await session_manager.get_or_create(session_id)
    except Exception as exc:
        log.error("Failed to get/create session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Error creating session: {exc}")]

    # Build the output path — sanitize the filename to prevent path
    # traversal and Stata command injection via embedded quotes.
    if filename and filename.strip():
        stem = filename.strip()
        # Allow only alphanumerics, underscores, hyphens, and dots (no slashes or quotes).
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", stem):
            return [
                TextContent(
                    type="text",
                    text="Error: filename must contain only alphanumerics, underscores, hyphens, and dots.",
                )
            ]
    else:
        stem = f"stata_graph_{uuid.uuid4().hex[:12]}"
    out_name = f"{stem}.{fmt}"

    # Use the session tmpdir for output — the tmpdir is created by the
    # session itself and contains only safe path characters, so embedding
    # it in the Stata command is safe.  The filename is validated above.
    tmpdir = session.tmpdir
    out_path = tmpdir / out_name

    # Use Stata's `cd` to avoid embedding the full path (which could
    # contain spaces or platform-specific characters) in the export command.
    code = (
        f'cd "{tmpdir}"\n'
        f'graph export "{out_name}", width({width}) replace'
    )
    try:
        result = await session.execute(code, timeout=30)
    except Exception as exc:
        log.error("Graph export error: %s", exc)
        return [TextContent(type="text", text=f"Graph export error: {exc}")]

    if result.error_message:
        return [
            TextContent(
                type="text",
                text=f"Stata error during graph export: {result.error_message}",
            )
        ]

    contents: list[TextContent | ImageContent] = []

    # Read the exported file
    if out_path.is_file():
        raw = out_path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        mime_map = {
            "png": "image/png",
            "pdf": "application/pdf",
            "svg": "image/svg+xml",
        }
        contents.append(
            ImageContent(
                type="image",
                data=b64,
                mimeType=mime_map[fmt],
            )
        )
        contents.append(TextContent(type="text", text=f"Graph exported to: {out_path}"))
    else:
        contents.append(
            TextContent(
                type="text",
                text=(
                    f"Graph export command ran but file not found at {out_path}. "
                    "Is there a graph in memory?"
                ),
            )
        )

    return contents
