"""Graph caching, detection, and auto-export injection for Stata output.

Watches a directory for new or modified graph files produced by Stata,
encodes them as base64 artifacts, and optionally injects ``graph export``
commands into user code so that graphical output is always captured.
"""

from __future__ import annotations

import base64
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported graph formats
# ---------------------------------------------------------------------------

_SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".gph": "application/x-stata-graph",
}

# ---------------------------------------------------------------------------
# GraphArtifact
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GraphArtifact:
    """A single graph file produced by Stata, base64-encoded for transport."""

    path: Path
    format: str  # "png", "pdf", "svg", "gph"
    base64: str  # base64-encoded file content
    width: int | None = field(default=None)
    height: int | None = field(default=None)


# ---------------------------------------------------------------------------
# PNG dimension helper
# ---------------------------------------------------------------------------


def _png_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read width and height from a PNG file's IHDR chunk.

    The PNG spec stores width (4 bytes, big-endian) at offset 16 and height at
    offset 20 inside the first 24 bytes of the file.

    Returns ``(None, None)`` on any failure so callers never need to worry
    about exceptions.
    """
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
        if len(header) < 24:
            return None, None
        # PNG magic: \x89PNG\r\n\x1a\n
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            return None, None
        width, height = struct.unpack(">II", header[16:24])
        return width, height
    except OSError:
        log.debug("Could not read PNG header from %s", path)
        return None, None


# ---------------------------------------------------------------------------
# GraphCache
# ---------------------------------------------------------------------------


class GraphCache:
    """Track graph files in *watch_dir* and detect new / modified outputs.

    Typical usage::

        cache = GraphCache(tmpdir)
        cache.take_snapshot()          # before Stata runs
        # ... run Stata command ...
        artifacts = cache.detect_changes()  # after Stata finishes
    """

    def __init__(self, watch_dir: Path) -> None:
        self.watch_dir = watch_dir
        self._snapshot: dict[Path, float] = {}  # path -> mtime

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def take_snapshot(self) -> None:
        """Record the current state of all graph files in *watch_dir*."""
        self._snapshot = self._scan()
        log.debug(
            "Snapshot taken: %d graph file(s) in %s",
            len(self._snapshot),
            self.watch_dir,
        )

    def detect_changes(self) -> list[GraphArtifact]:
        """Compare current directory state with the last snapshot.

        Returns a list of :class:`GraphArtifact` objects for every graph file
        that is **new** or whose mtime has changed since :meth:`take_snapshot`
        was last called.
        """
        current = self._scan()
        changed: list[GraphArtifact] = []

        for path, mtime in current.items():
            prev_mtime = self._snapshot.get(path)
            if prev_mtime is None or mtime > prev_mtime:
                log.info("Detected new/modified graph: %s", path)
                try:
                    artifact = self.encode_graph(path)
                    changed.append(artifact)
                except Exception:
                    log.warning("Failed to encode graph %s", path, exc_info=True)

        # Update the snapshot so the next call only sees further changes.
        self._snapshot = current
        return changed

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def encode_graph(path: Path) -> GraphArtifact:
        """Read *path* and return a base64-encoded :class:`GraphArtifact`.

        Raises :class:`FileNotFoundError` if the file does not exist, or
        :class:`ValueError` if the extension is not a supported graph format.
        """
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_EXTENSIONS:
            msg = f"Unsupported graph format: {suffix!r} (path={path})"
            raise ValueError(msg)

        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        fmt = suffix.lstrip(".")

        width: int | None = None
        height: int | None = None
        if suffix == ".png":
            width, height = _png_dimensions(path)

        log.debug(
            "Encoded %s (%s, %d bytes, %sx%s)",
            path.name,
            fmt,
            len(raw),
            width,
            height,
        )
        return GraphArtifact(
            path=path,
            format=fmt,
            base64=encoded,
            width=width,
            height=height,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _scan(self) -> dict[Path, float]:
        """Return ``{path: mtime}`` for every supported graph file in *watch_dir*."""
        results: dict[Path, float] = {}
        if not self.watch_dir.is_dir():
            log.debug("Watch directory does not exist yet: %s", self.watch_dir)
            return results

        for ext in _SUPPORTED_EXTENSIONS:
            for p in self.watch_dir.glob(f"*{ext}"):
                if p.is_file():
                    try:
                        results[p] = p.stat().st_mtime
                    except OSError:
                        log.debug("Could not stat %s", p)
        return results


# ---------------------------------------------------------------------------
# Auto-inject graph export
# ---------------------------------------------------------------------------

# Patterns that indicate the Stata code produces graphical output.
# We look for common graph commands at the start of a line (possibly after
# whitespace or a `quietly` prefix).
_GRAPH_CMD_RE = re.compile(
    r"""
    (?:^|\n)                     # start of string or new line
    [ \t]*                       # optional leading whitespace
    (?:qui(?:etly)?[ \t]+)?      # optional quietly prefix
    (?:
        graph\b                  # graph (draw / twoway / …)
      | tw(?:oway)?\b            # twoway shorthand
      | scatter\b                # scatter
      | line\b                   # line
      | histogram\b              # histogram
      | hist\b                   # hist (abbreviation)
      | kdensity\b               # kdensity
      | qnorm\b                  # qnorm
      | pnorm\b                  # pnorm
      | rvfplot\b                # residual-vs-fitted
      | avplot\b                 # added-variable plot
      | lvr2plot\b               # leverage-vs-residual plot
      | marginsplot\b            # marginsplot
      | coefplot\b               # coefplot (user-written but very common)
    )
    """,
    re.VERBOSE,
)

# Detects an existing graph export command anywhere in the code.
_HAS_EXPORT_RE = re.compile(
    r"""
    (?:^|\n)
    [ \t]*
    (?:qui(?:etly)?[ \t]+)?
    graph[ \t]+export\b
    """,
    re.VERBOSE,
)


def maybe_inject_graph_export(code: str, tmpdir: Path) -> str:
    """Append ``graph export`` after each graph command that lacks one.

    Parameters
    ----------
    code:
        One or more Stata commands (may span multiple lines).
    tmpdir:
        Temporary directory where the exported PNGs should be written.

    Returns
    -------
    str
        The original *code* with ``graph export`` lines injected after
        each graphing command that is not already followed by an export.
    """
    # If the code already contains a graph export, assume the user manages
    # exports manually and don't interfere.
    if _HAS_EXPORT_RE.search(code):
        log.debug("Code already contains `graph export`; skipping injection.")
        return code

    # Find all graph command positions.
    matches = list(_GRAPH_CMD_RE.finditer(code))
    if not matches:
        return code

    # Build the new code by inserting an export after each graph command.
    # Work backwards so that earlier insertion offsets stay valid.
    result = code
    for i, m in enumerate(reversed(matches)):
        # Find the end of the line containing the graph command.
        line_end = result.find("\n", m.end())
        if line_end == -1:
            line_end = len(result)

        # But the command may span multiple lines via /// continuation.
        # Walk forward past continuation lines.
        pos = line_end
        while pos < len(result):
            next_line_end = result.find("\n", pos + 1)
            if next_line_end == -1:
                next_line_end = len(result)
            next_line = result[pos + 1 : next_line_end].strip()
            # Check if the previous line ended with ///
            prev_content = result[:pos].rstrip()
            if prev_content.endswith("///") or next_line.startswith("///"):
                pos = next_line_end
            elif next_line.startswith(">"):
                # Continuation prompt residue
                pos = next_line_end
            else:
                break
        insert_pos = pos

        timestamp = int(time.time() * 1000) + i  # unique per graph
        export_path = tmpdir / f"stata_graph_{timestamp}.png"
        export_line = f'\nquietly graph export "{export_path}", width(2000) replace'
        log.info("Auto-injecting graph export: %s", export_path.name)
        result = result[:insert_pos] + export_line + result[insert_pos:]

    return result
