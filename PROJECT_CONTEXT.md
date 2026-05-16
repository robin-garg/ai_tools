# Project Context

> This file captures the background, goals, and key decisions for this project.
> Intended as a quick-start reference for any AI assistant or collaborator joining
> the project, so they don't need the full conversation history to be useful.

---

## Project Overview

**Name**: `ai_tools`

**Purpose**: A personal collection of Python-based AI/automation tools to support
daily software-development work. Built incrementally — tools are added as real
problems arise rather than pre-planned up front.

**Not** a commercial product, team project, or public library. It is a personal
toolkit, but structured as a proper Python package so individual tools can be:
- reused across multiple environments,
- wrapped later as an MCP server (see Roadmap), and
- potentially published/distributed if any tool becomes broadly useful.

---

## Initial Use Cases (seed ideas)

1. **VoIP / Flexisip log analysis** — the author maintains an app with VoIP
   functionality that uses Flexisip as a push gateway for mobile. Root-causing
   issues currently requires manual trawling through logs; a tool will
   automate common analyses (correlation, filtering, event extraction).
2. **Log capture to local file** — general-purpose utility to capture/persist
   logs from various sources to disk in a consistent format.
3. **Text-to-PDF generation** — convert plain text / structured content into
   shareable PDFs for team reports and handoffs.

More tools will be added opportunistically.

---

## Tech Stack & Rationale

| Choice | Rationale |
|---|---|
| **Python 3.12** | Stable, broad package compatibility, strong in AI/ML ecosystem. Pinned via `.python-version`. |
| **`uv`** (package manager) | Single modern tool replacing pip + venv + pyenv + pip-tools. Fast (Rust-based). Uses standard `pyproject.toml` so the project is not locked into `uv`. |
| **`src/` layout** | Recommended Python packaging convention. Cleanly separates package code from project root, avoids accidental imports of dev files, makes distribution straightforward. |
| **`uv_build` build backend** | Native `uv` backend. Swappable later for `hatchling`, `setuptools`, etc. if needed. |
| **PEP 561 typed** | `src/ai_tools/py.typed` marker declares the package ships with type hints. |

---

## Architectural Decisions

### One repo per deployable unit

Rather than a monorepo, each deployable concern lives in its own repo:

| Repo (current / planned) | Purpose |
|---|---|
| `ai_tools` *(this repo)* | Core Python tools — the library |
| `ai_tools_mcp` *(planned, separate repo)* | MCP server wrapping the tools, so they can be used from any MCP-compatible AI client |
| `ai_tools_ts` *(possible future)* | TypeScript port of the tools, if/when the author decides to build one |

**Rationale**: independent deployment, focused git history, smaller clones,
clear boundaries. Each repo stays easy to reason about on its own.

### Library-first design

Tools are designed as importable functions/classes in `src/ai_tools/`, not as
one-off scripts. This keeps them reusable from:
- CLI entry points (to be added to `pyproject.toml` as needed)
- The future MCP server
- Interactive use (Jupyter, REPL)
- Other Python code

---

## Flexisip Servers

| Key | Domain / SSH alias | Environment | Policy |
| --- | --- | --- | --- |
| `stg2` | `stg2` | Staging | Full read/write — safe to experiment |
| `stg2b` | `stg2b` | Staging | Full read/write — safe to experiment |
| `prod` | `flexisip.e1a.aws.wlcomm.net` | **Production** | **READ-ONLY** — see policy below |

### ⚠️ Production server policy (`prod`)

The `prod` server is **production**. Strict read-only access only.

#### ✅ Allowed (read-only)

| Method | Allowed commands / operations |
| --- | --- |
| **Redis** (`redis-cli`) | `GET`, `HGETALL`, `KEYS`, `SCAN`, `TTL`, `TYPE` — no writes |
| **Flexisip socket** (`/tmp/flexisip-proxy-3`) | `REGISTRAR_GET`, `CONFIG_GET`, `CONFIG_LIST` — read-only queries only |
| **Event logs** | `cat`, `tail`, `ls` on `/var/opt/belledonne-communications/log/flexisip/users/...` via `podman exec` |
| **Proxy log** | `tail`, `cat`, `grep` on `/usr/local/var/log/flexisip/flexisip-proxy.log` via `podman exec` |
| **Config file** | `cat`, `grep` on `/etc/flexisip/DEVOPS-32/flexisip.conf` |

#### ❌ Never allowed on production

- Any `CONFIG_SET`, `REGISTRAR_CLEAR`, `REGISTRAR_DELETE`, or other mutating Flexisip socket commands
- Any `redis-cli SET`, `DEL`, `HSET`, `HDEL`, `EXPIRE`, or any write to Redis
- Restarting, stopping, or reconfiguring any container or service
- Running Python scripts (or any script) inside the container unless it is provably read-only
- Any `podman exec` command that writes files, changes state, or modifies configuration

#### 🔴 Abort rule

If a task **cannot** be completed without making a change on `prod`:
1. **Stop immediately** — do not proceed
2. **Inform the user** what would need to change and why
3. **Wait for explicit approval** before doing anything

#### Visibility note

Scripts run inside the container via `podman exec` are **visible** to anyone
monitoring the server (e.g. `ps aux` inside the container). Even read-only
scripts can alarm operators. Prefer simple one-liner commands over multi-line
Python scripts when possible on production.

SSH access: username `rgarg`, key `~/.ssh/id_ed25519`.

---

## Flexisip Log Conventions

### Always fetch logs live from the server

Never use locally cached log files unless the user explicitly says to use them.
Always SSH to the target server and pull logs fresh for every analysis.

### Container roles — which container to use for logs

| Container | Role | Use for logs? |
| --- | --- | --- |
| `flexisip-proxy` | SIP call routing, registrations, push relay | **Yes — always use this for container logs** |
| `flexisip-regevent` | Schedules background push notifications to keep devices alive | **No — it has no log files** |

`flexisip-regevent` only sends periodic push pings to devices; it does not
produce any log output worth analysing. Never treat it as a log source.

### Log sources by type

| Log type | Location | How to fetch |
| --- | --- | --- |
| SIP call / registration proxy logs | Inside `flexisip-proxy` container: `/usr/local/var/log/flexisip/flexisip-proxy.log` | `docker exec` via `log_extractor.extract()` |
| Registration event-logs | On the **host**: `/var/log/flexisip/event-logs` | Direct SSH via `log_extractor.extract_event_logs()` |

---

## Conventions

- **Package name**: `ai_tools` (snake_case, Python convention)
- **Distribution name**: `ai-tools` (kebab-case, PyPI convention)
- **Python version**: always match `.python-version`; update in `pyproject.toml`
  (`requires-python`) simultaneously if bumped.
- **Commit style**: Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, etc.)
- **Dependencies**: always add via `uv add` / `uv add --dev`, never hand-edit
  `pyproject.toml` for deps.
- **Virtual env**: `.venv/` managed by `uv`; never commit it.
- **Secrets**: never commit `.env`; use `.env.example` as a template when needed.

---

## Roadmap

Short-term (next few sessions):
- [ ] Implement the first tool (VoIP / Flexisip log analyzer most likely)
- [ ] Add dev tooling: `ruff` (lint + format), `pytest` (tests)
- [ ] Set up a git remote (GitHub / GitLab / Bitbucket — TBD)
- [ ] Flesh out `README.md` with setup and usage examples

Medium-term:
- [ ] Add 2–3 more daily-use tools
- [ ] Define CLI entry points in `pyproject.toml` for the most-used tools
- [ ] Start the `ai_tools_mcp` repo wrapping these tools as MCP server

Long-term / optional:
- [ ] TypeScript port (`ai_tools_ts`) if it becomes useful
- [ ] Publish to PyPI if any tool is broadly applicable
