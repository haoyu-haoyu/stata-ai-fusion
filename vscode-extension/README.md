# Stata AI Fusion

MCP Server + Skill Knowledge Base + VS Code Extension for Stata.

A three-in-one Stata AI integration: let AI directly execute Stata code,
generate high-quality statistical analysis code, and provide a complete IDE
experience in VS Code.

## Features

- **MCP Server**: 11 tools that let AI agents operate Stata directly
  - `run_command` / `run_do_file` -- execute code
  - `inspect_data` / `codebook` -- data exploration
  - `get_results` -- extract r()/e()/c() results
  - `export_graph` -- graph export with auto-capture
  - `search_log` / `install_package` -- utility tools
  - `list_sessions` / `close_session` -- session management
- **Skill Knowledge Base**: 5,600+ lines of Stata knowledge
  - Econometrics, causal inference, survival analysis, clinical data analysis
  - 14 reference documents with Progressive Disclosure architecture
- **VS Code Extension**: complete Stata IDE experience
  - Syntax highlighting (350+ functions)
  - 30 code snippets
  - Run code / .do files with one keypress
  - Graph preview panel

## Quick Start

### MCP Server (Claude Desktop / Claude Code / Cursor)

```bash
# One-command launch with uvx
uvx --from stata-ai-fusion stata-ai-fusion
```

Or configure in your AI assistant's MCP settings:

```json
{
  "mcpServers": {
    "stata": {
      "command": "uvx",
      "args": ["--from", "stata-ai-fusion", "stata-ai-fusion"]
    }
  }
}
```

### VS Code Extension

```bash
code --install-extension stata-ai-fusion-0.2.2.vsix
```

### Skill Only (Claude.ai)

Download `stata-ai-fusion-skill.zip` from the
[Releases](https://github.com/haoyu-haoyu/stata-ai-fusion/releases) page,
then upload via Claude.ai Settings > Skills.

## Requirements

- Stata 17+ installed locally (MP, SE, IC, or BE)
- Python 3.11+ (for MCP server)
- VS Code 1.85+ (for extension)

## Supported Platforms

- macOS (Intel & Apple Silicon)
- Linux
- Windows

## Development

```bash
git clone https://github.com/haoyu-haoyu/stata-ai-fusion.git
cd stata-ai-fusion
uv sync
uv run pytest tests/ -v
```

## License

MIT -- see [LICENSE](LICENSE) for details.
