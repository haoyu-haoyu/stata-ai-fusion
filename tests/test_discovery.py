"""Tests for the Stata discovery module.

These tests exercise discovery helpers, path parsing, edition detection, and
the SEARCH_PATHS data without requiring a Stata installation on the machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stata_ai_fusion.stata_discovery import (
    SEARCH_PATHS,
    StataInstallation,
    StataNotFoundError,
    _edition_from_name,
    _version_from_path,
    discover_stata,
    discover_stata_or_none,
)


# ---------------------------------------------------------------------------
# SEARCH_PATHS structure
# ---------------------------------------------------------------------------


class TestSearchPaths:
    """Validate the SEARCH_PATHS constant."""

    def test_search_paths_not_empty(self):
        """At least one platform entry must exist."""
        assert len(SEARCH_PATHS) > 0

    def test_all_platforms_present(self):
        """darwin, linux, and win32 should be covered."""
        assert "darwin" in SEARCH_PATHS
        assert "linux" in SEARCH_PATHS
        assert "win32" in SEARCH_PATHS

    def test_current_platform_has_paths(self):
        """The running platform (or its normalised key) should have entries."""
        key = sys.platform
        if key.startswith("linux"):
            key = "linux"
        # We only assert if the platform is one we explicitly map.
        if key in SEARCH_PATHS:
            assert len(SEARCH_PATHS[key]) > 0

    def test_paths_are_strings(self):
        """Every element in every platform list must be a string."""
        for platform, patterns in SEARCH_PATHS.items():
            for pattern in patterns:
                assert isinstance(pattern, str), (
                    f"Non-string pattern for {platform}: {pattern!r}"
                )


# ---------------------------------------------------------------------------
# Edition detection
# ---------------------------------------------------------------------------


class TestEditionDetection:
    """Test _edition_from_name for various executable stems."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("stata-mp", "MP"),
            ("statamp", "MP"),
            ("statamp-64", "MP"),
            ("stata-se", "SE"),
            ("statase", "SE"),
            ("statase-64", "SE"),
            ("stata", "IC"),
            ("stata-64", "IC"),
            ("stata-be", "BE"),
            ("statabe", "BE"),
            # With extensions (Windows) — note: Path.stem strips ".exe"
            ("StataMP-64.exe", "MP"),  # stem "StataMP-64" -> "statamp-64" -> MP
            ("StataSE-64.exe", "SE"),  # stem "StataSE-64" -> "statase-64" -> SE
            ("Stata.exe", "IC"),  # stem "Stata" -> "stata" -> IC
            ("StataBE-64.exe", "BE"),  # stem "StataBE-64" -> "statabe-64" -> BE
        ],
    )
    def test_known_editions(self, name: str, expected: str):
        assert _edition_from_name(name) == expected

    def test_unknown_name_defaults_to_ic(self):
        """An unrecognised executable name should default to IC."""
        assert _edition_from_name("some_random_binary") == "IC"

    def test_case_insensitive(self):
        """Detection lowercases the stem, so mixed case should work."""
        assert _edition_from_name("STATA-MP") == "MP"
        assert _edition_from_name("Stata-Se") == "SE"


# ---------------------------------------------------------------------------
# Version from path
# ---------------------------------------------------------------------------


class TestVersionFromPath:
    """Test _version_from_path for directory-based version extraction."""

    @pytest.mark.parametrize(
        ("path_str", "expected"),
        [
            ("/Applications/Stata18/StataMP.app/Contents/MacOS/stata-mp", 18),
            ("/Applications/Stata17/Stata.app/Contents/MacOS/stata", 17),
            ("/usr/local/stata16/stata-se", 16),
            ("/usr/local/stata15/stata", 15),
            ("C:/Program Files/Stata19/StataMP-64.exe", 19),
            # StataNow (no version digit in dir name)
            ("/Applications/StataNow/StataMP.app/Contents/MacOS/stata-mp", None),
            # Plain /Applications path
            ("/Applications/StataMP.app/Contents/MacOS/stata-mp", None),
            # Version in multiple parts -- first match wins
            ("/opt/stata17/extra/stata18/stata-mp", 17),
        ],
    )
    def test_version_extraction(self, path_str: str, expected: int | None):
        assert _version_from_path(Path(path_str)) == expected


# ---------------------------------------------------------------------------
# StataInstallation dataclass
# ---------------------------------------------------------------------------


class TestStataInstallation:
    """Test the StataInstallation dataclass properties."""

    def test_basic_properties(self):
        inst = StataInstallation(
            path=Path("/usr/local/stata18/stata-mp"),
            edition="MP",
            version=18,
            platform="linux",
        )
        assert inst.edition == "MP"
        assert inst.version == 18
        assert inst.platform == "linux"

    def test_supports_unicode_true(self):
        inst = StataInstallation(path=Path("/fake"), edition="SE", version=14)
        assert inst.supports_unicode is True

    def test_supports_unicode_false(self):
        inst = StataInstallation(path=Path("/fake"), edition="IC", version=13)
        assert inst.supports_unicode is False

    def test_supports_unicode_none_version(self):
        inst = StataInstallation(path=Path("/fake"), edition="IC", version=None)
        assert inst.supports_unicode is False

    def test_supports_frames_true(self):
        inst = StataInstallation(path=Path("/fake"), edition="MP", version=16)
        assert inst.supports_frames is True

    def test_supports_frames_false(self):
        inst = StataInstallation(path=Path("/fake"), edition="MP", version=15)
        assert inst.supports_frames is False

    def test_supports_frames_none_version(self):
        inst = StataInstallation(path=Path("/fake"), edition="MP", version=None)
        assert inst.supports_frames is False

    def test_frozen_dataclass(self):
        inst = StataInstallation(path=Path("/fake"), edition="MP", version=18)
        with pytest.raises(AttributeError):
            inst.edition = "SE"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Environment variable override
# ---------------------------------------------------------------------------


class TestEnvVarOverride:
    """Verify that STATA_PATH environment variable influences discovery."""

    def test_env_var_nonexistent_path(self, monkeypatch):
        """When STATA_PATH points to a non-existent file, discovery should
        fall through to other methods (and eventually raise or return None).
        """
        monkeypatch.setenv("STATA_PATH", "/fake/path/to/stata")
        # discover_stata_or_none should not raise even with a bad path.
        result = discover_stata_or_none()
        # On a machine without Stata this will be None; on a machine with
        # Stata it will find it via the other search paths.  Either outcome
        # is acceptable -- we mainly verify no crash.
        assert result is None or isinstance(result, StataInstallation)

    def test_env_var_not_executable(self, monkeypatch, tmp_path):
        """A regular (non-executable) file should be skipped."""
        fake = tmp_path / "stata"
        fake.write_text("not a real binary")
        monkeypatch.setenv("STATA_PATH", str(fake))
        # The env-var candidate is not executable, so discovery falls through.
        result = discover_stata_or_none()
        assert result is None or isinstance(result, StataInstallation)

    def test_discover_stata_raises_when_nothing_found(self, monkeypatch):
        """discover_stata() must raise StataNotFoundError when nothing exists."""
        monkeypatch.setenv("STATA_PATH", "/absolutely/does/not/exist")
        # Also ensure the system PATH cannot find Stata.
        monkeypatch.setenv("PATH", "/empty_dir_no_stata")
        # Clear the glob-based search paths so discover_stata has nowhere to look.
        monkeypatch.setattr(
            "stata_ai_fusion.stata_discovery.SEARCH_PATHS",
            {},
        )
        with pytest.raises(StataNotFoundError):
            discover_stata()
