"""Auto-discover Stata installations on macOS, Linux, and Windows."""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class StataNotFoundError(FileNotFoundError):
    """Raised when no usable Stata installation can be found."""

    def __init__(self, message: str = "No Stata installation found") -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Edition helpers
# ---------------------------------------------------------------------------

_EDITION_MAP: dict[str, str] = {
    "stata-mp": "MP",
    "statamp": "MP",
    "statamp-64": "MP",
    "stata-se": "SE",
    "statase": "SE",
    "statase-64": "SE",
    "stata": "IC",
    "stata-64": "IC",
    "stata-be": "BE",
    "statabe": "BE",
    "statabe-64": "BE",
}


def _edition_from_name(name: str) -> str:
    """Return the edition string (MP/SE/IC/BE) from an executable name."""
    stem = Path(name).stem.lower()
    return _EDITION_MAP.get(stem, "IC")


# ---------------------------------------------------------------------------
# Platform-specific search paths
# ---------------------------------------------------------------------------

# Each value is an *ordered* list of glob patterns that should resolve to
# Stata executables.  The patterns are expanded with ``glob.glob`` so they
# may contain ``*`` and ``?`` wildcards.

SEARCH_PATHS: dict[str, list[str]] = {
    # -- macOS ---------------------------------------------------------------
    "darwin": [
        # StataNow series (highest priority — newest packaging)
        "/Applications/StataNow/StataMP.app/Contents/MacOS/stata-mp",
        "/Applications/StataNow/StataSE.app/Contents/MacOS/stata-se",
        "/Applications/StataNow/Stata.app/Contents/MacOS/stata",
        "/Applications/StataNow/StataBE.app/Contents/MacOS/stata-be",
        # Traditional versioned directories  (Stata18, Stata17, …)
        "/Applications/Stata*/StataMP.app/Contents/MacOS/stata-mp",
        "/Applications/Stata*/StataSE.app/Contents/MacOS/stata-se",
        "/Applications/Stata*/Stata.app/Contents/MacOS/stata",
        "/Applications/Stata*/StataBE.app/Contents/MacOS/stata-be",
        # Direct (un-versioned) .app in /Applications
        "/Applications/StataMP.app/Contents/MacOS/stata-mp",
        "/Applications/StataSE.app/Contents/MacOS/stata-se",
        "/Applications/Stata.app/Contents/MacOS/stata",
        "/Applications/StataBE.app/Contents/MacOS/stata-be",
    ],
    # -- Linux ---------------------------------------------------------------
    "linux": [
        "/usr/local/stata*/stata-mp",
        "/usr/local/stata*/stata-se",
        "/usr/local/stata*/stata",
        "/usr/local/stata*/stata-be",
        "/usr/local/bin/stata-mp",
        "/usr/local/bin/stata-se",
        "/usr/local/bin/stata",
        "/usr/local/bin/stata-be",
    ],
    # -- Windows -------------------------------------------------------------
    "win32": [
        "C:/Program Files/Stata*/StataMP-64.exe",
        "C:/Program Files/Stata*/StataSE-64.exe",
        "C:/Program Files/Stata*/Stata-64.exe",
        "C:/Program Files/Stata*/StataBE-64.exe",
        "C:/Program Files/Stata*/StataMP.exe",
        "C:/Program Files/Stata*/StataSE.exe",
        "C:/Program Files/Stata*/Stata.exe",
        "C:/Program Files/Stata*/StataBE.exe",
        "C:/Program Files (x86)/Stata*/StataMP.exe",
        "C:/Program Files (x86)/Stata*/StataSE.exe",
        "C:/Program Files (x86)/Stata*/Stata.exe",
        "C:/Program Files (x86)/Stata*/StataBE.exe",
    ],
}


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

_VERSION_DIR_RE = re.compile(r"[Ss]tata\s*(\d+)")


def _version_from_path(path: Path) -> int | None:
    """Try to extract a major version number from the *directory* path.

    Looks for patterns like ``Stata18``, ``stata17``, ``Stata 19`` in any
    component of the path.
    """
    for part in path.parts:
        m = _VERSION_DIR_RE.search(part)
        if m:
            return int(m.group(1))
    return None


def _version_from_executable(path: Path) -> int | None:
    """Detect Stata version by running a quick batch command.

    Tries two strategies:
    1. Run ``stata -q`` and parse the banner (e.g. ``Stata/MP 18.0 …``).
    2. Run ``stata -b -e`` with a tiny do-file that displays ``c(version)``
       and parse the resulting log.

    Returns *None* when the version cannot be determined.
    """
    import tempfile

    # --- Strategy 1: parse the banner from ``stata -q`` --------------------
    try:
        result = subprocess.run(
            [str(path), "-q"],
            capture_output=True,
            text=True,
            timeout=15,
            input="exit\n",
        )
        output = result.stdout + result.stderr
        m = re.search(r"Stata\S*\s+(\d+)\.", output)
        if m:
            return int(m.group(1))
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass

    # --- Strategy 2: batch-mode do-file with ``display c(version)`` -------
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            do_file = Path(tmpdir) / "_version_check.do"
            do_file.write_text("display c(version)\nexit, clear STATA\n")
            result = subprocess.run(
                [str(path), "-b", "do", str(do_file)],
                capture_output=True,
                text=True,
                timeout=20,
                cwd=tmpdir,
            )
            # Stata batch mode writes output to a .log file in cwd
            log_file = Path(tmpdir) / "_version_check.log"
            log_text = ""
            if log_file.exists():
                log_text = log_file.read_text()
            else:
                log_text = result.stdout + result.stderr
            # Look for a bare version number on its own line (e.g. "19" or "18.5")
            for line in log_text.splitlines():
                stripped = line.strip()
                m2 = re.match(r"^(\d+)(?:\.\d+)?$", stripped)
                if m2:
                    return int(m2.group(1))
            # Also try the banner pattern in the log
            m3 = re.search(r"Stata\S*\s+(\d+)\.", log_text)
            if m3:
                return int(m3.group(1))
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass

    return None


# ---------------------------------------------------------------------------
# StataInstallation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StataInstallation:
    """Represents a discovered Stata installation."""

    path: Path
    edition: str  # "MP", "SE", "IC", or "BE"
    version: int | None = field(default=None)
    platform: str = field(default_factory=lambda: sys.platform)

    # -- derived properties --------------------------------------------------

    @property
    def supports_unicode(self) -> bool:
        """Stata 14+ supports Unicode."""
        if self.version is None:
            return False
        return self.version >= 14

    @property
    def supports_frames(self) -> bool:
        """Stata 16+ supports frames (``frame`` commands)."""
        if self.version is None:
            return False
        return self.version >= 16

    def __str__(self) -> str:  # pragma: no cover – cosmetic
        ver = f" {self.version}" if self.version else ""
        return f"Stata/{self.edition}{ver} ({self.path})"


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _is_executable(path: Path) -> bool:
    """Return *True* if *path* exists and is executable."""
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_glob_paths(patterns: list[str]) -> list[Path]:
    """Expand a list of glob patterns into a de-duplicated, ordered list of
    existing executable paths.
    """
    seen: set[Path] = set()
    result: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        for match in matches:
            p = Path(match).resolve()
            if p not in seen and _is_executable(p):
                seen.add(p)
                result.append(p)
    return result


def _try_which() -> list[Path]:
    """Fall back to ``which`` / ``shutil.which`` for ``stata-mp``, ``stata-se``,
    ``stata``, and ``stata-be``.
    """
    names = ["stata-mp", "stata-se", "stata", "stata-be"]
    found: list[Path] = []
    for name in names:
        location = shutil.which(name)
        if location is not None:
            p = Path(location).resolve()
            if _is_executable(p):
                logger.debug("which('%s') → %s", name, p)
                found.append(p)
    return found


def _build_installation(path: Path) -> StataInstallation:
    """Build a ``StataInstallation`` from a validated executable *path*."""
    edition = _edition_from_name(path.name)

    # Attempt version detection: first from path, then from running the binary.
    version = _version_from_path(path)
    if version is None:
        version = _version_from_executable(path)

    return StataInstallation(
        path=path,
        edition=edition,
        version=version,
        platform=sys.platform,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_stata() -> StataInstallation:
    """Discover and return the best available Stata installation.

    Search priority:
    1. The ``STATA_PATH`` environment variable (must point to an executable).
    2. Common installation paths for the current platform (expanded via
       ``glob.glob``).
    3. The system ``PATH`` (via ``shutil.which``).

    Raises
    ------
    StataNotFoundError
        If no usable Stata installation is found.
    """

    # ----- 1. STATA_PATH environment variable ------------------------------
    env_path = os.environ.get("STATA_PATH")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        logger.debug("Checking STATA_PATH env var: %s", p)
        if _is_executable(p):
            installation = _build_installation(p)
            logger.info("Stata found via STATA_PATH: %s", installation)
            return installation
        logger.debug("STATA_PATH '%s' is not a valid executable", p)

    # ----- 2. Common install paths -----------------------------------------
    platform_key = sys.platform
    # Normalise platform key: anything starting with "linux" → "linux"
    if platform_key.startswith("linux"):
        platform_key = "linux"

    patterns = SEARCH_PATHS.get(platform_key, [])
    candidates = _resolve_glob_paths(patterns)

    for candidate in candidates:
        logger.debug("Checking common path: %s", candidate)
        if _is_executable(candidate):
            installation = _build_installation(candidate)
            logger.info("Stata found at common path: %s", installation)
            return installation

    # ----- 3. which / shutil.which -----------------------------------------
    which_candidates = _try_which()
    for candidate in which_candidates:
        logger.debug("Checking which result: %s", candidate)
        if _is_executable(candidate):
            installation = _build_installation(candidate)
            logger.info("Stata found via which: %s", installation)
            return installation

    # ----- Nothing found ---------------------------------------------------
    logger.info("No Stata installation discovered")
    raise StataNotFoundError(
        "No Stata installation found. Set the STATA_PATH environment variable "
        "to the full path of your Stata executable, or install Stata in one of "
        "the standard locations."
    )


def discover_stata_or_none() -> StataInstallation | None:
    """Like :func:`discover_stata`, but returns ``None`` instead of raising."""
    try:
        return discover_stata()
    except StataNotFoundError:
        return None
