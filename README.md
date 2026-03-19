# agtop

TUI monitor for AI coding agents — [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex) and more.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

![screenshot](screenshot.svg)

## Features

- **Live monitoring** — real-time status tracking (working, idle, waiting for input/permission, done) with conversation preview and tool call summaries
- **Terminal jump** — one-key jump to the terminal running a session. Supports iTerm2, WezTerm, Terminal.app, Warp, Kaku, and tmux
- **History browser** — browse all past sessions grouped by project, preview full conversations, and resume with one key. Configurable time range (default 7 days)
- **Sub-agent visualization** — waterfall diagram showing spawn batches and a Gantt timeline of agent lifetimes. Still being improved — more details and richer display coming soon
- **Hook-based status sync** — optional Claude Code hooks for precise state detection (vs. mtime heuristics)
- **Notifications** — macOS notifications + terminal bell when a session needs attention. Subscribe to specific sessions for completion alerts
- Works with any terminal. Supports Claude Code and OpenAI Codex CLI

## Install

### Homebrew

```bash
brew install lhead/tap/agtop
```

## Usage

```bash
agtop
```

### Claude Hooks

Install Claude Code hooks before using status sync and terminal jump:

```bash
agtop --install-hooks
```

For local development, install hooks with the same executable you use to run agtop:

```bash
.venv/bin/agtop --install-hooks
.venv/bin/agtop
```

This avoids Claude calling a stale global `agtop` binary. After installing hooks, start a new Claude session so `~/.config/agtop/events/` begins receiving event files.

### Keybindings

| Key | Action |
|-----|--------|
| `j` | Jump to session's terminal tab |
| `h` | Toggle Live / History mode |
| `v` | View sub-agent diagram |
| `a` | Subscribe/unsubscribe to completion alerts |
| `r` | Refresh (Live) / Resume session (History) |
| `c` | Copy detail (Live) / Copy resume command (History) |
| `q` | Quit |

## Configuration

`~/.config/agtop/config.toml`

```toml
[general]
show_recent_hours = 4   # Show closed sessions from last N hours
max_sessions = 20       # Max sessions in list

[refresh]
fast = 1                # Seconds - when active sessions exist
slow = 3                # Seconds - when all idle/done

[notifications]
enabled = true          # macOS notification for waiting sessions
sound = true            # Terminal bell
```

## License

MIT
