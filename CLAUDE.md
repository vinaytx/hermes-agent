# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Essential Reading

Before making changes, read [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md) — they contain architectural decisions and contribution rules that aren't duplicated here.

## Commands

```bash
# Dev setup
uv venv .venv --python 3.11
uv pip install -e ".[all,dev]"
source .venv/bin/activate

# Run agent
./hermes
./hermes chat -q "Hello"

# Tests — canonical (per-file isolation, hermetic env, matches CI)
scripts/run_tests.sh
scripts/run_tests.sh tests/agent/test_foo.py
scripts/run_tests.sh -- -v --tb=long
scripts/run_tests.sh -- -m integration

# Config / logs
cat ~/.hermes/config.yaml
hermes logs [--follow] [--level ERROR] [--session ID]
hermes doctor
```

## Architecture

### Core Data Flow

```
User message (CLI / gateway platform)
  → AIAgent.run_conversation()     [run_agent.py]
      → build system prompt (identity + skills + context files + memory)
      → LLM API call (OpenAI-compatible endpoint)
      → loop while tool_calls:
          → handle_function_call()   [model_tools.py]
          → append tool result
          → re-call LLM
      → persist to SQLite            [hermes_state.py]
      → return final_response
```

### File Dependency Chain

```
tools/registry.py   (no deps — imported by all tool files)
       ↑
tools/*.py          (each calls registry.register() at import time)
       ↑
model_tools.py      (imports tools/* → triggers self-registration)
       ↑
run_agent.py  cli.py  batch_runner.py  environments/*
```

### Key Files

| File/Dir | Role |
|---|---|
| `run_agent.py` | `AIAgent` class — core conversation loop (~12k LOC) |
| `cli.py` | `HermesCLI` — interactive TUI via prompt_toolkit (~11k LOC) |
| `model_tools.py` | Tool discovery (`discover_builtin_tools`) and dispatch (`handle_function_call`) |
| `toolsets.py` | Tool groupings per platform; `_HERMES_CORE_TOOLS` list |
| `hermes_state.py` | `SessionDB` — SQLite + FTS5 full-text search across sessions |
| `hermes_constants.py` | `get_hermes_home()` — profile-aware path resolution |
| `hermes_cli/commands.py` | Central `COMMAND_REGISTRY` — all slash commands derive from here |
| `tools/registry.py` | Self-registration hub for all tools |
| `tools/environments/` | Terminal backends: local, docker, ssh, modal, daytona, singularity |
| `gateway/` | Messaging gateway — `run.py`, `session.py`, `platforms/` adapters |
| `agent/` | Provider adapters, memory, caching, compression, skill loading |
| `plugins/` | Memory providers, model backends, image gen, observability |
| `skills/` | Bundled procedural skills (shipped with every install) |
| `optional-skills/` | Niche/heavyweight skills (not active by default) |
| `cron/` | Scheduler (`jobs.py`, `scheduler.py`) using croniter |
| `acp_adapter/` | ACP server for VS Code / Zed / JetBrains |

### Tool Self-Registration Pattern

```python
# tools/my_tool.py
from tools.registry import registry

MY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "...",
        "parameters": {"type": "object", "properties": {...}, "required": [...]},
    },
}

registry.register(
    name="my_tool",
    toolset="my_toolset",
    schema=MY_TOOL_SCHEMA,
    handler=lambda args, **kw: my_tool(**args, **kw),
    check_fn=lambda: True,  # return False if optional dep missing
)
```

After creating `tools/my_tool.py`, wire the name into `toolsets.py`.

### Slash Command Registration

All slash commands live in `COMMAND_REGISTRY` (`hermes_cli/commands.py`). Adding an entry there automatically propagates to CLI dispatch, gateway dispatch, Telegram menus, Slack routing, autocomplete, and help text. Then add the handler in `HermesCLI.process_command()` in `cli.py` (and `gateway/run.py` if gateway-available).

### Tool vs Skill Decision

- **Tool** (Python): needs API keys/auth, binary data, streaming, real-time events, custom Python logic
- **Skill** (Markdown): expressible as instructions + shell commands + existing tools — **prefer skills**

Memory provider plugins are not accepted in-tree; publish as a standalone package implementing `MemoryProvider` ABC.

## User Config Locations

| Path | Contents |
|---|---|
| `~/.hermes/config.yaml` | All settings (model, provider, toolsets, compression, etc.) |
| `~/.hermes/.env` | Secrets only (API keys) |
| `~/.hermes/state.db` | SQLite session DB — canonical history store |
| `~/.hermes/skills/` | Active skills (bundled + installed + agent-created) |
| `~/.hermes/memories/` | Persistent memory files |
| `~/.hermes/logs/` | `agent.log` (INFO+), `errors.log` (WARNING+), `gateway.log` |

## Dependency Policy

All deps use **exact pins** (`==X.Y.Z`), not ranges. This was tightened after the Mini Shai-Hulud worm hit `mistralai 2.4.6` on PyPI (2026-05-12). After bumping a version, regenerate with `uv lock`. Only packages needed by every session belong in `dependencies`; everything else goes in an optional extra and lazy-installs via `tools/lazy_deps.py`.

## Testing Notes

- `@pytest.mark.integration` — requires external services; skip with `-m 'not integration'`
- `@pytest.mark.real_concurrent_gate` — opts out of the autouse stub that disables `_detect_concurrent_hermes_instances`
- Per-test timeout: 30s hard cap (pytest-timeout, signal method)
- `conftest.py` blanks environment variables to keep tests hermetic
