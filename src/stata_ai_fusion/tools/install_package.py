"""Install package tool.

Provides the ``stata_install_package`` MCP tool that installs a Stata
community-contributed package from SSC or a custom source.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from ..stata_session import SessionManager

log = logging.getLogger(__name__)

TOOL_NAME = "stata_install_package"

TOOL_DEF = Tool(
    name=TOOL_NAME,
    description=(
        "Install a Stata community-contributed package. By default installs "
        "from SSC (Statistical Software Components). First checks whether the "
        "package is already installed. Use this when a command is not found and "
        "needs to be installed (e.g. estout, outreg2, coefplot)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "package": {
                "type": "string",
                "description": "Package name to install (e.g. 'estout', 'outreg2').",
            },
            "from_ssc": {
                "type": "boolean",
                "description": "Install from SSC. Default true.",
                "default": True,
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier. Default 'default'.",
                "default": "default",
            },
        },
        "required": ["package"],
    },
)


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Check for and install a Stata package."""
    package: str = arguments.get("package", "")
    from_ssc: bool = arguments.get("from_ssc", True)
    session_id: str = arguments.get("session_id", "default")

    if not package.strip():
        return [TextContent(type="text", text="Error: no package name provided.")]

    package = package.strip()

    # Validate package name to prevent Stata command injection.
    # Stata package names are alphanumeric with optional underscores/hyphens.
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", package):
        return [
            TextContent(
                type="text",
                text="Error: package name must contain only alphanumerics, underscores, and hyphens.",
            )
        ]

    try:
        session = await session_manager.get_or_create(session_id)
    except Exception as exc:
        log.error("Failed to get/create session %s: %s", session_id, exc)
        return [TextContent(type="text", text=f"Error creating session: {exc}")]

    # Step 1: Check if already installed
    check_code = f"capture which {package}"
    try:
        check_result = await session.execute(check_code, timeout=30)
    except Exception as exc:
        log.error("Error checking package %s: %s", package, exc)
        return [TextContent(type="text", text=f"Error checking package: {exc}")]

    # If `which` succeeds without error, the package is already installed
    if check_result.return_code == 0 and check_result.error_message is None:
        output = check_result.output or ""
        # `which` prints the path when found; check it does not contain "not found"
        if "not found" not in output.lower():
            return [
                TextContent(
                    type="text",
                    text=f"Package '{package}' is already installed.\n{output.strip()}",
                )
            ]

    # Step 2: Install the package
    if from_ssc:
        install_code = f"ssc install {package}, replace"
    else:
        install_code = f"net install {package}, replace"

    try:
        install_result = await session.execute(install_code, timeout=120)
    except Exception as exc:
        log.error("Error installing package %s: %s", package, exc)
        return [TextContent(type="text", text=f"Error installing package: {exc}")]

    output_text = install_result.output or ""
    if install_result.error_message:
        output_text += f"\n\n--- Stata Error ---\n{install_result.error_message}"
        if install_result.error_code is not None:
            output_text += f" [r({install_result.error_code})]"
        return [TextContent(type="text", text=f"Installation failed:\n{output_text.strip()}")]

    return [
        TextContent(
            type="text",
            text=f"Package '{package}' installed successfully.\n{output_text.strip()}",
        )
    ]
