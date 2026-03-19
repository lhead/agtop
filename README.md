# agtop

TUI monitor for AI coding agents — [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex) and more.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

![screenshot](screenshot.svg)

## Features

- Live status tracking: working, idle, waiting for input/permission, done
- Conversation preview with tool call summaries
- One-key jump to the terminal tab/pane running a session
- Session history browser with project grouping and resume
- Sub-agent visualization: waterfall diagram and Gantt timeline
- Hook-based status sync for precise state detection
- macOS notifications + bell when a session needs attention
- Subscribe to sessions for task-completion alerts
- Supports Claude Code and OpenAI Codex CLI
- Terminal support: iTerm2, WezTerm, Terminal.app, Warp, Kaku, tmux

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
