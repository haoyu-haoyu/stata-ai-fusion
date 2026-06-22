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
            "from_url": {
                "type": "string",
                "description": (
                    "Custom source for `net install` (an http(s) URL or absolute "
                    "path). Required when from_ssc is false; if given, it takes "
                    "precedence over SSC."
                ),
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

# Marker used to surface `which`'s return code.  `capture` suppresses which's
# own output (including the "not found" message and r(111)), so the only
# reliable signal of whether a package exists is `_rc`, which we print and parse.
_RC_MARKER = "__stata_pkg_rc="
_RC_RE = re.compile(rf"{re.escape(_RC_MARKER)}(-?\d+)")

# A custom `net install` source: an http(s) URL or an absolute path, restricted
# to characters that cannot break out of the surrounding Stata `from("...")`
# string (no quotes, whitespace, backtick, $, ; or newlines).
_FROM_URL_RE = re.compile(r"^(?:https?://[A-Za-z0-9._~:/?=%\-]+|/[A-Za-z0-9._~/\-]+)$")


def _parse_check_rc(output: str | None) -> int | None:
    """Return the `_rc` from a ``which`` check, or ``None`` if not found.

    A non-zero (e.g. 111) value means the command is not installed; ``0`` means
    it is available; ``None`` means the marker was missing (treat as unknown).
    """
    if not output:
        return None
    m = _RC_RE.search(output)
    return int(m.group(1)) if m else None


async def handle(
    session_manager: SessionManager,
    arguments: dict,
) -> list[TextContent]:
    """Check for and install a Stata package."""
    package: str = arguments.get("package", "")
    from_ssc: bool = arguments.get("from_ssc", True)
    from_url: str = (arguments.get("from_url") or "").strip()
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

    # Step 1: Check whether the package is already installed.
    #
    # `which <pkg>` sets _rc=111 when the command is not found.  `capture`
    # suppresses which's output, so we print _rc and read it back rather than
    # scraping the (now-empty) text.  The previous implementation scraped the
    # suppressed output and concluded EVERY missing package was "already
    # installed", so it never actually installed anything.
    check_code = f'capture which {package}\ndisplay "{_RC_MARKER}" _rc'
    try:
        check_result = await session.execute(check_code, timeout=30)
    except Exception as exc:
        log.error("Error checking package %s: %s", package, exc)
        return [TextContent(type="text", text=f"Error checking package: {exc}")]

    check_rc = _parse_check_rc(check_result.output)
    if check_rc == 0:
        return [
            TextContent(
                type="text",
                text=f"Package '{package}' is already installed.",
            )
        ]
    # check_rc is non-zero (not installed) or None (marker missing / unknown);
    # fall through and attempt the install — `, replace` makes it idempotent.

    # Step 2: Install the package.  `net install` needs a source, so the
    # non-SSC path requires a from_url — the old `net install <pkg>` with no
    # source always errored.
    if from_url:
        if not _FROM_URL_RE.match(from_url):
            return [
                TextContent(
                    type="text",
                    text=(
                        "Error: from_url must be an http(s) URL or absolute path "
                        "(no quotes, spaces, or other special characters)."
                    ),
                )
            ]
        install_code = f'net install {package}, from("{from_url}") replace'
    elif from_ssc:
        install_code = f"ssc install {package}, replace"
    else:
        return [
            TextContent(
                type="text",
                text=(
                    "Error: set from_ssc=true to install from SSC, or provide "
                    "from_url for a custom `net install` source."
                ),
            )
        ]

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
