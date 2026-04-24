# ai-tools

A personal collection of Python tools for daily software-development work.

Built as an importable library so individual tools can be reused across scripts,
wrapped behind an MCP server, or called from other Python code.

## Status

Early stage. Tools are added incrementally as real needs arise.

## Planned tools

- **VoIP / Flexisip log analyzer** — automate common log-analysis tasks for apps
  using Flexisip as a push gateway.
- **Log capture utility** — capture logs from various sources to local files in
  a consistent format.
- **Text-to-PDF generator** — produce shareable PDF reports from text input.

More to follow.

## Requirements

- Python **3.12** (pinned via `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) for dependency and environment management

## Setup

```bash
# Install uv (macOS)
brew install uv

# Clone and enter the project
git clone <repo-url> ai_tools
cd ai_tools

# Create the virtual environment and install the project (editable)
uv sync
```

`uv sync` reads `pyproject.toml` + `uv.lock`, provisions Python 3.12 if needed,
creates `.venv/`, and installs all dependencies.

## Usage

Run commands inside the project's environment with `uv run`:

```bash
# Quick smoke test
uv run python -c "from ai_tools import hello; print(hello())"
# → Hello from ai-tools!
```

As tools are added, CLI entry points will be defined in `pyproject.toml` so
they can be invoked directly, e.g. `uv run ai-tools-log-analyze <path>`.

## Project layout

```
ai_tools/
├── pyproject.toml           # project metadata and dependencies
├── uv.lock                  # locked dependency versions (reproducible installs)
├── .python-version          # pinned Python version (3.12)
├── README.md                # this file
├── PROJECT_CONTEXT.md       # project background, decisions, and roadmap
└── src/
    └── ai_tools/            # package code
        ├── __init__.py
        └── py.typed         # PEP 561 marker (typed package)
```

## Development

Common `uv` commands:

```bash
uv add <package>              # add a runtime dependency
uv add --dev <package>        # add a dev-only dependency
uv remove <package>           # remove a dependency
uv sync                       # install/refresh deps from pyproject.toml + uv.lock
uv run <command>              # run a command inside the project venv
uv lock --upgrade             # upgrade dependency versions within constraints
```

## Related projects

- **`ai_tools_mcp`** *(planned, separate repo)* — MCP server exposing these
  tools to MCP-compatible AI clients.
