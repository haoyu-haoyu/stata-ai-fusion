# Changelog

## 0.3.0 (2026-06-22)

### Added
- **Persistent sessions without a PTY**: a new `PipeSession` keeps a single Stata process alive over stdin (output read from a log file) on hosts that deny PTY allocation (e.g. sandboxed launches). In-memory data, scalars, macros, and `e()`/`r()` results now persist between tool calls, and `stata_cancel_command` works. Fallback order is interactive (pexpect) → pipe → batch.
- **`stata_install_package` `from_url`**: install from a custom `net install` source (an http(s) URL or absolute path, validated to block injection).

### Fixed
- **Graph auto-export off-by-one**: the injected `graph export` was placed *before* a graph command that wasn't on the first line (Stata `r(601)`, no image returned); it is now inserted after, and non-rendering `graph` subcommands (`drop`, `dir`, …) no longer trigger a spurious export.
- **`stata_install_package` never installed missing packages**: `capture which` suppressed the "not found"/`r(111)`, so every missing package was reported "already installed"; the check now reads `_rc` explicitly.
- **Error-detection false positives**: a return code merely mentioned in output text (e.g. `display "see r(198)"`) was reported as an error; detection is now anchored to `r(NNN);` at the start of a line.
- **`stata_get_results` command injection**: the `keys` field reached executed Stata code unvalidated; stored-result names are now validated against a Stata-identifier whitelist (`get_scalar`/`get_matrix`/`get_macro`).
- **Discovery picked the oldest Stata**: with several versions installed, the lexicographically-first glob match (the oldest) was chosen; it now selects the newest version, then the most capable edition.
- **`stata_search_log` ReDoS**: a caller-supplied regex ran on the asyncio event loop with no timeout; the match loop now runs in a worker thread bounded by a timeout (and `context_lines` is clamped).
- **Matrix parsing**: `get_matrix` returned `None` for symmetric (`symmetric e(V)[n,n]`, lower-triangle) and wide/wrapped matrices and emitted invalid-JSON `nan`; it now parses all three layouts and maps missing values to `null`.
- **VS Code extension**: corrected the tool names (`run_command` → `stata_run_command`) and argument (`file_path` → `path`) so Run Selection/Run File work; the client now sends `notifications/initialized`; and `autoConfigureMcp` no longer deletes the user's other MCP servers when `mcp.json` contains JSONC comments or fails to parse.

### Changed
- **Removed the `echo` parameter** from `stata_run_command`: it injected `set output inform`, which suppressed *results* (not echoes) and, in a persistent session, was never reset — poisoning later commands. It had no correct implementation.
- Documentation: corrected MCP tool names and examples in both READMEs.

## 0.2.2 (2026-02-27)

### Security
- **Codebook command injection prevention**: Variable names are now validated against a strict regex before being embedded in Stata commands
- **Graph export path safety**: Uses `cd` + relative filename instead of embedding the full tmpdir path in Stata commands; auto-generated filenames use UUID instead of timestamps

### Bug Fixes
- **Windows StataBE-64 detection**: Added `statabe-64` to edition map so Windows BE edition is correctly identified (was falling back to IC)
- **search_log avoids unnecessary session creation**: Uses `get_session()` instead of `get_or_create()` to prevent spinning up a new Stata process just to search an empty log
- **Output preservation in `_clean_do_output`**: Refined `. ` stripping to only remove echoed command lines (starting with letters/underscores), preserving numeric output that happens to start with `. `
- **Batch return code captured**: `run_do_file` now reports non-zero Stata exit codes instead of silently ignoring them
- **`width` parameter validation**: `export_graph` rejects width values outside 100–10000 to prevent resource exhaustion
- **`timeout` parameter clamped**: `run_command` and `run_do_file` clamp timeout to 1–3600s to prevent negative or unbounded values

### Internal
- Removed dead `register()` functions and unused `Server` imports from all 9 tool modules
- Removed unused `_EDITION_PRIORITY` constant and empty `TYPE_CHECKING` block
- Added `py.typed` marker for PEP 561 type checker support
- Log buffer capped at 1000 entries (FIFO eviction) to bound memory usage
- Temp `.do` file cleanup failures now logged at debug level

## 0.2.1 (2026-02-27)

### Bug Fixes
- **Process group kill in `close()`**: Session cleanup now kills the entire Stata process group (including MP worker processes) instead of just the main process
- **Batch mode process isolation**: `_run_batch()` uses `start_new_session=True` for proper process group isolation; timeout kills the whole group
- **Stale log cleanup**: `run_do_file` removes leftover `.log` files before each batch run to prevent reading stale output
- **Multi-graph capture**: `maybe_inject_graph_export()` now injects `graph export` after *every* graph command (not just the last), with unique timestamps
- **Unused imports removed**: Cleaned up `base64` and `uuid` imports in `run_do_file.py`
- **Tool description guidance**: `run_command` description now directs AI to use `run_do_file` for long-running models (mixed, bootstrap, simulate)
- **Session timeout auto-cleanup**: `SessionManager` tracks per-session activity and automatically closes idle sessions after 1 hour (configurable via `session_timeout`)

### Internal
- MCP Server now exposes 11 tools (added `stata_cancel_command`)
- Batch do-file execution rewritten with `subprocess.Popen` for reliability
- Improved timeout handling with partial output capture

## 0.2.0 (2026-02-19)

### Features
- Session stability improvements
- Cancel command tool (`stata_cancel_command`)
- Batch-mode do-file execution

## 0.1.0 (2025-02-19)

### Features
- MCP Server with 10 tools (run_command, inspect_data, get_results, etc.)
- Skill knowledge base (5,600+ lines, 14 reference documents)
- VS Code extension with syntax highlighting, snippets, and code execution
- Auto-discovery of Stata installation (macOS/Linux/Windows)
- Multi-session support with data isolation
- Graph caching and auto-export
- r()/e()/c() result extraction
- MCP Resources for Skill knowledge access

### Supported Stata Versions
- Stata 17, 18, 19 (MP, SE, IC, BE)
- StataNow editions
