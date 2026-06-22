"""Extract stored results (r(), e(), c()) from a running Stata session.

Stata commands store their results in three namespaces:

- **r()** -- general results from ``r``-class commands (e.g. ``summarize``).
- **e()** -- estimation results from ``e``-class commands (e.g. ``regress``).
- **c()** -- system parameters and settings (``c(version)``, ``c(os)``, ...).

This module provides :class:`ResultExtractor` which wraps a
:class:`~stata_ai_fusion.stata_session.StataSession` and exposes
coroutine methods to retrieve individual scalars, macros, matrices, or
the full result dictionary.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stata_ai_fusion.stata_session import StataSession

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Stata's missing-value sentinel.
_STATA_MISSING = "."

# Matches a scalar line in ``return list`` / ``ereturn list`` output, e.g.:
#   "              r(N) =  74"
#   "           e(rmse) =  .0345678901234567"
_SCALAR_RE = re.compile(
    r"^\s+([rec])\((\w+)\)\s*=\s*(.+?)\s*$",
    re.MULTILINE,
)

# Matches a macro line in ``return list`` / ``ereturn list`` output, e.g.:
#   '         r(varlist) : "price mpg"'
_MACRO_RE = re.compile(
    r'^\s+([rec])\((\w+)\)\s*:\s*"(.*?)"\s*$',
    re.MULTILINE,
)

# Matches a matrix entry line in ``return list`` output, e.g.:
#   "           e(b) :  1 x 3"
_MATRIX_LIST_RE = re.compile(
    r"^\s+([rec])\((\w+)\)\s*:\s*\d+\s*x\s*\d+\s*$",
    re.MULTILINE,
)

# Matches the dimension header of ``matrix list`` output, e.g.:
#   "e(b)[1,3]"
_MATRIX_DIM_RE = re.compile(
    r"^[rec]\(\w+\)\[(\d+),(\d+)\]",
    re.MULTILINE,
)

# A valid stored-result name is a plain Stata identifier.  Names that reach
# get_scalar/get_matrix/get_macro are interpolated into executed Stata code,
# so anything else is rejected to prevent command injection (e.g. a crafted
# key like ``N)\nshell ...`` smuggled in through the get_results `keys` field).
_RESULT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_numeric(value: str) -> float | None:
    """Parse a Stata numeric value to a Python float.

    Returns ``None`` for Stata's system missing value (``"."``), extended
    missing values (``.a`` .. ``.z``), and any other un-parseable string.
    """
    value = value.strip()
    if not value:
        return None
    # Stata missing: "." or ".a" through ".z"
    if value == _STATA_MISSING or re.fullmatch(r"\.[a-z]", value):
        return None
    try:
        return float(value)
    except ValueError:
        log.debug("Could not parse numeric value: %r", value)
        return None


def _parse_scalar_value(raw: str) -> float | str | None:
    """Interpret a scalar value from ``display`` output.

    Stata scalars can hold either a numeric value or a string.  We attempt
    numeric parsing first and fall back to a stripped string.
    """
    raw = raw.strip()
    if not raw:
        return None
    num = _parse_numeric(raw)
    if num is not None:
        return num
    # If the numeric parse returned None but the raw value is not a
    # missing-value sentinel, treat it as a string scalar.
    if raw == _STATA_MISSING or re.fullmatch(r"\.[a-z]", raw):
        return None
    return raw


def _parse_matrix_output(output: str) -> list[list[float]] | None:
    """Parse the output of ``matrix list <name>`` into a 2-D list.

    Expected format::

        e(b)[1,3]
                      mpg      weight       _cons
            y1  -49.512221   1.746559   1946.0687

    Returns ``None`` when the output cannot be parsed.
    """
    lines = output.strip().splitlines()
    if not lines:
        return None

    # Find the dimension header line.
    dim_match: re.Match[str] | None = None
    dim_line_idx = -1
    for idx, line in enumerate(lines):
        dim_match = _MATRIX_DIM_RE.search(line)
        if dim_match:
            dim_line_idx = idx
            break
    if dim_match is None or dim_line_idx < 0:
        log.debug("Could not locate matrix dimension header in output")
        return None

    nrows = int(dim_match.group(1))
    ncols = int(dim_match.group(2))

    # The column-header line follows the dimension line.
    # Data rows start after the column-header line.
    # We skip the dimension line and the column-header line.
    data_start = dim_line_idx + 2  # +1 for col headers, +1 for first data row
    data_lines = lines[data_start:]

    matrix: list[list[float]] = []
    for line in data_lines:
        if not line.strip():
            continue
        # Each data line has a row label followed by numeric values.
        # Split on whitespace and take the last *ncols* tokens as values.
        tokens = line.split()
        if len(tokens) < ncols:
            # Possibly a continuation line or footer; skip.
            continue
        # The row label(s) occupy the leading tokens; values are the tail.
        value_tokens = tokens[-ncols:]
        row: list[float] = []
        for tok in value_tokens:
            num = _parse_numeric(tok)
            row.append(num if num is not None else float("nan"))
        matrix.append(row)
        if len(matrix) >= nrows:
            break

    if not matrix:
        return None
    return matrix


def _parse_return_list(output: str) -> dict:
    """Parse the output of ``return list`` / ``ereturn list`` / ``creturn list``.

    Returns a dictionary with top-level keys ``"scalars"``, ``"macros"``,
    and ``"matrices"``, each mapping result names to their values.
    """
    result: dict[str, dict[str, float | str | None]] = {
        "scalars": {},
        "macros": {},
        "matrices": {},
    }

    # -- scalars -------------------------------------------------------------
    for m in _SCALAR_RE.finditer(output):
        name = m.group(2)
        raw_val = m.group(3).strip()
        result["scalars"][name] = _parse_scalar_value(raw_val)

    # -- macros --------------------------------------------------------------
    for m in _MACRO_RE.finditer(output):
        name = m.group(2)
        result["macros"][name] = m.group(3)

    # -- matrices (names only; values are not printed by return list) --------
    for m in _MATRIX_LIST_RE.finditer(output):
        name = m.group(2)
        # Store the dimension string as a placeholder; callers can use
        # get_matrix() to retrieve the full content.
        result["matrices"][name] = None

    return result


# ---------------------------------------------------------------------------
# Commands for each result class
# ---------------------------------------------------------------------------

_RETURN_LIST_CMD: dict[str, str] = {
    "r": "return list",
    "e": "ereturn list",
    "c": "creturn list",
}


# ---------------------------------------------------------------------------
# ResultExtractor
# ---------------------------------------------------------------------------


class ResultExtractor:
    """Extract stored results from a running Stata session.

    Parameters
    ----------
    session:
        A :class:`~stata_ai_fusion.stata_session.StataSession` instance.
        The session must expose an ``async execute(code: str)`` method that
        returns an object with ``.output`` (str) and ``.return_code`` (int)
        attributes.
    """

    def __init__(self, session: StataSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute(self, code: str) -> str | None:
        """Run *code* via the session and return the output text.

        Returns ``None`` if the command fails (non-zero return code) or if
        the session raises an exception (e.g. the session is not connected).
        """
        try:
            result = await self.session.execute(code)
        except Exception:
            log.warning("Session execute failed for: %s", code, exc_info=True)
            return None

        if result.return_code != 0:
            log.debug(
                "Stata returned non-zero rc=%d for: %s\nOutput: %s",
                result.return_code,
                code,
                result.output,
            )
            return None

        return result.output

    @staticmethod
    def _validate_result_class(result_class: str) -> str:
        """Normalise and validate the *result_class* parameter."""
        rc = result_class.strip().lower()
        if rc not in ("r", "e", "c"):
            msg = f"result_class must be 'r', 'e', or 'c', got {result_class!r}"
            raise ValueError(msg)
        return rc

    @staticmethod
    def _validate_result_name(name: str) -> str:
        """Validate a stored-result *name* before interpolating it into Stata.

        Stored-result names (scalars/macros/matrices in r()/e()/c()) are plain
        Stata identifiers.  Rejecting anything else prevents a crafted name from
        injecting extra Stata/OS commands.
        """
        cleaned = name.strip()
        if not _RESULT_NAME_RE.match(cleaned):
            msg = f"result name must be a Stata identifier, got {name!r}"
            raise ValueError(msg)
        return cleaned

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_scalar(
        self,
        name: str,
        result_class: str = "r",
    ) -> float | str | None:
        """Extract a single scalar result.

        Parameters
        ----------
        name:
            The scalar name (e.g. ``"mean"``, ``"N"``).
        result_class:
            One of ``"r"``, ``"e"``, ``"c"``.

        Returns
        -------
        float | str | None
            The scalar value, or ``None`` when it cannot be retrieved (missing
            value, session error, or non-existent scalar).

        Examples
        --------
        >>> value = await extractor.get_scalar("mean", "r")
        """
        rc = self._validate_result_class(result_class)
        name = self._validate_result_name(name)
        code = f"display {rc}({name})"
        output = await self._execute(code)
        if output is None:
            return None

        # The ``display`` command prints the value on a line by itself.
        # Strip surrounding whitespace and blank lines.
        cleaned = output.strip()
        if not cleaned:
            return None

        # Take the last non-empty line (Stata may echo the command first
        # depending on the session mode).
        lines = [ln for ln in cleaned.splitlines() if ln.strip()]
        if not lines:
            return None
        value_line = lines[-1].strip()
        return _parse_scalar_value(value_line)

    async def get_matrix(
        self,
        name: str,
        result_class: str = "e",
    ) -> list[list[float]] | None:
        """Extract a matrix result as a 2-D list of floats.

        Parameters
        ----------
        name:
            The matrix name (e.g. ``"b"``, ``"V"``).
        result_class:
            One of ``"r"``, ``"e"``, ``"c"``.

        Returns
        -------
        list[list[float]] | None
            A row-major 2-D list, or ``None`` when the matrix cannot be
            retrieved or parsed.

        Examples
        --------
        >>> coeffs = await extractor.get_matrix("b", "e")
        """
        rc = self._validate_result_class(result_class)
        name = self._validate_result_name(name)
        code = f"matrix list {rc}({name})"
        output = await self._execute(code)
        if output is None:
            return None

        return _parse_matrix_output(output)

    async def get_macro(
        self,
        name: str,
        result_class: str = "e",
    ) -> str | None:
        """Extract a string macro result.

        Parameters
        ----------
        name:
            The macro name (e.g. ``"depvar"``, ``"cmd"``).
        result_class:
            One of ``"r"``, ``"e"``, ``"c"``.

        Returns
        -------
        str | None
            The macro string, or ``None`` when it cannot be retrieved.

        Examples
        --------
        >>> depvar = await extractor.get_macro("depvar", "e")
        """
        rc = self._validate_result_class(result_class)
        name = self._validate_result_name(name)
        code = f"display {rc}({name})"
        output = await self._execute(code)
        if output is None:
            return None

        cleaned = output.strip()
        if not cleaned:
            return None

        # Take the last non-empty line.
        lines = [ln for ln in cleaned.splitlines() if ln.strip()]
        if not lines:
            return None
        return lines[-1].strip()

    async def get_all(self, result_class: str = "r") -> dict:
        """Extract all stored results for a given result class.

        Executes ``return list``, ``ereturn list``, or ``creturn list``
        and parses the output into a dictionary with keys ``"scalars"``,
        ``"macros"``, and ``"matrices"``.

        Parameters
        ----------
        result_class:
            One of ``"r"``, ``"e"``, ``"c"``.

        Returns
        -------
        dict
            A dictionary with three sub-dicts::

                {
                    "scalars":  {"N": 74.0, "mean": 6165.256...},
                    "macros":   {"varlist": "price"},
                    "matrices": {"b": None, "V": None},
                }

            Matrix entries are ``None`` placeholders; use
            :meth:`get_matrix` to fetch the full content.  Returns an
            empty structure when the session is unavailable or the
            command fails.

        Examples
        --------
        >>> results = await extractor.get_all("e")
        >>> results["scalars"]["r2"]
        0.8734
        """
        rc = self._validate_result_class(result_class)
        cmd = _RETURN_LIST_CMD[rc]
        output = await self._execute(cmd)
        if output is None:
            return {"scalars": {}, "macros": {}, "matrices": {}}

        return _parse_return_list(output)
