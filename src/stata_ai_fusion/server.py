"""MCP Server for Stata AI Fusion.

This module is the entry-point for the Stata AI Fusion MCP server.  It
discovers the local Stata installation, creates a
:class:`~stata_ai_fusion.stata_session.SessionManager`, registers all MCP
tools and resources, then runs the server over ``stdio``.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource

from .stata_discovery import StataNotFoundError, discover_stata
from .stata_session import SessionManager
from .tools import register_all_tools

log = logging.getLogger(__name__)

SKILL_DIR = Path(__file__).parent.parent.parent / "skill"


# ---------------------------------------------------------------------------
# Resource helpers
# ---------------------------------------------------------------------------


def _read_skill_main() -> str:
    """Read the SKILL.md knowledge base document.

    Returns the file's contents on success.  If the file is missing,
    logs a WARNING (so operators see the packaging/config problem in
    their server log) and returns a user-visible placeholder that keeps
    the MCP resource-read contract.
    """
    skill_path = SKILL_DIR / "SKILL.md"
    if skill_path.is_file():
        try:
            return skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to read %s: %s", skill_path, exc)
            return f"(SKILL.md unreadable: {exc.strerror or exc})"
    log.warning(
        "SKILL.md not found at %s — knowledge base resource will be empty. "
        "Check that the 'skill/' directory is present in your installation.",
        skill_path,
    )
    return "(SKILL.md not found)"


def _list_reference_files() -> list[Path]:
    """List all reference markdown files in ``skill/references/``.

    If the references directory is missing or unreadable, logs a
    WARNING and returns an empty list so the MCP server still starts
    cleanly.
    """
    refs_dir = SKILL_DIR / "references"
    if not refs_dir.is_dir():
        log.warning(
            "Reference directory missing: %s — no reference topics will be advertised. "
            "Check that the 'skill/references/' directory is present in your installation.",
            refs_dir,
        )
        return []
    try:
        return sorted(refs_dir.glob("*.md"))
    except OSError as exc:
        log.warning("Failed to scan reference directory %s: %s", refs_dir, exc)
        return []


def _read_reference(topic: str) -> str | None:
    """Read a single reference document by topic slug.

    Returns the document's text on success, or ``None`` if no matching
    file exists (case-insensitive).  A missing references directory
    surfaces as a WARNING (emitted by :func:`_list_reference_files`)
    so both the list and single-topic access paths expose the same
    operator-visible signal for that root cause.

    The exact-path read is attempted *before* listing so that a
    directory with execute-only permissions (listable=False but
    openable by known name) still succeeds for the happy path.
    """
    refs_dir = SKILL_DIR / "references"

    # Fast path: try the exact filename first.  This succeeds even on
    # directories that block ``glob()`` / ``readdir()`` but allow
    # ``open()`` of a known path (rare but real: POSIX execute-only).
    candidate = refs_dir / f"{topic}.md"
    if candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to read reference %s: %s", candidate, exc)
            return None

    # Fall back to listing for the case-insensitive match.  Calling
    # :func:`_list_reference_files` is also what surfaces a missing /
    # unreadable references directory to operators.
    ref_files = _list_reference_files()

    topic_lower = topic.lower()
    for p in ref_files:
        if p.stem.lower() == topic_lower:
            try:
                return p.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("Failed to read reference %s: %s", p, exc)
                return None

    log.debug("Reference topic not found: %s", topic)
    return None


# ---------------------------------------------------------------------------
# Resource registration
# ---------------------------------------------------------------------------


def register_resources(server: Server) -> None:
    """Register MCP resources for the Skill knowledge base.

    Resources:
    - ``stata://skill/main``  -- The primary SKILL.md document
    - ``stata://skill/references``  -- List all reference topic names
    - ``stata://skill/references/{topic}``  -- A single reference doc
    """

    @server.list_resources()
    async def handle_list_resources() -> list[Resource]:
        resources: list[Resource] = [
            Resource(
                uri="stata://skill/main",
                name="Stata Skill Guide",
                description="Primary knowledge base for Stata AI Fusion skill",
                mimeType="text/markdown",
            ),
            Resource(
                uri="stata://skill/references",
                name="Reference Topics",
                description="List of all available Stata reference topics",
                mimeType="text/plain",
            ),
        ]

        for ref_path in _list_reference_files():
            topic = ref_path.stem
            resources.append(
                Resource(
                    uri=f"stata://skill/references/{topic}",
                    name=f"Reference: {topic}",
                    description=f"Stata reference for {topic}",
                    mimeType="text/markdown",
                )
            )

        return resources

    @server.read_resource()
    async def handle_read_resource(uri) -> str:
        uri_str = str(uri)

        if uri_str == "stata://skill/main":
            return _read_skill_main()

        if uri_str == "stata://skill/references":
            ref_files = _list_reference_files()
            if not ref_files:
                return "(no reference files found)"
            return "\n".join(f"- {p.stem}" for p in ref_files)

        if uri_str.startswith("stata://skill/references/"):
            topic = uri_str.split("stata://skill/references/", 1)[1]
            content = _read_reference(topic)
            if content is not None:
                return content
            # _read_reference() already logs at DEBUG for an unknown topic
            # and at WARNING if the underlying references directory is
            # missing — don't add a duplicate INFO log here.
            return f"(reference not found: {topic})"

        log.error("Unknown MCP resource URI requested: %s", uri_str)
        return f"(unknown resource: {uri_str})"


# ---------------------------------------------------------------------------
# Server entry-point
# ---------------------------------------------------------------------------


async def serve() -> None:
    """Start the Stata AI Fusion MCP server.

    Configuration via environment variables:

    - ``MCP_STATA_LOGLEVEL`` -- logging level (default ``INFO``).
    - ``STATA_PATH`` -- override Stata binary path (see
      :func:`~stata_ai_fusion.stata_discovery.discover_stata`).
    - ``MCP_STATA_TEMP`` -- base directory for session temp files.
    """

    # -- Configure logging -------------------------------------------------
    log_level = os.environ.get("MCP_STATA_LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    log.info("Starting Stata AI Fusion MCP server")

    # -- Discover Stata ----------------------------------------------------
    try:
        installation = discover_stata()
        log.info("Stata discovered: %s", installation)
    except StataNotFoundError as exc:
        log.error("Stata not found: %s", exc)
        raise

    # -- Init SessionManager -----------------------------------------------
    session_manager = SessionManager(installation)

    # -- Create MCP Server -------------------------------------------------
    server = Server("stata-ai-fusion")

    # -- Register tools and resources --------------------------------------
    register_all_tools(server, session_manager)
    register_resources(server)

    # -- Graceful shutdown on SIGINT/SIGTERM --------------------------------
    async def _shutdown() -> None:
        log.info("Shutting down: closing all sessions")
        await session_manager.close_all()

    def _signal_handler(sig: int, _frame: object) -> None:
        log.info("Received signal %s", signal.Signals(sig).name)
        # The server will exit; rely on finally block for cleanup.

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_handler)
        except (OSError, ValueError) as exc:
            # Windows restricts which signals can be set from non-main threads
            # or in certain execution contexts (e.g. pytest runners).  Log at
            # DEBUG rather than swallowing silently so the cause is visible
            # when troubleshooting unexpected shutdown behavior.
            log.debug("Could not install handler for %s: %s", signal.Signals(sig).name, exc)

    # -- Run ---------------------------------------------------------------
    log.info("MCP server ready, waiting for connections via stdio")
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await _shutdown()
