# Contributing to stata-ai-fusion

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/haoyu-haoyu/stata-ai-fusion.git
   cd stata-ai-fusion
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

3. Run the tests:
   ```bash
   uv run pytest
   ```

4. Run the linter:
   ```bash
   uv run ruff check .
   ```

## Code Style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.
- Maximum line length is 100 characters.
- All public functions and classes must have docstrings.

## Pull Requests

1. Fork the repository and create a feature branch.
2. Write tests for any new functionality.
3. Ensure all tests pass and the linter is clean.
4. Open a pull request with a clear description of your changes.

## Reporting Issues

If you find a bug or have a feature request, please open a GitHub issue with:
- A clear title and description
- Steps to reproduce (for bugs)
- Your Stata version and OS

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
