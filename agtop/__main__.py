import argparse
import json
import shutil
import subprocess
import sys

from .hooks import HOOK_EVENTS, install_claude_hooks, run_hook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agtop")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--hook",
        choices=HOOK_EVENTS,
        metavar="EVENT",
        help="write a Claude hook event snapshot (prompt, notification, stop)",
    )
    group.add_argument(
        "--install-hooks",
        action="store_true",
        help="install Claude Code hooks into ~/.claude/settings.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.hook:
        try:
            return run_hook(args.hook)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"agtop: {exc}", file=sys.stderr)
            return 1

    if args.install_hooks:
        try:
            path, changed = install_claude_hooks()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"agtop: {exc}", file=sys.stderr)
            return 1
        if changed:
            print(f"Installed Claude hooks in {path}")
        else:
            print(f"Claude hooks already installed in {path}")
        return 0

    from .app import AgtopApp

    while True:
        app = AgtopApp()
        app.run()

        session_id = app._resume_session_id
        if not session_id:
            break

        # Textual has fully restored the terminal — safe to run now
        resume_cwd = app._resume_cwd or None
        try:
            if app._resume_source == "codex":
                codex_bin = shutil.which("codex") or "codex"
                subprocess.run(
                    [codex_bin, "resume", session_id],
                    cwd=resume_cwd,
                )
            else:
                claude_bin = shutil.which("claude") or "claude"
                subprocess.run(
                    [claude_bin, "--resume", session_id],
                    cwd=resume_cwd,
                )
        except KeyboardInterrupt:
            pass
        # After claude exits, loop restarts agtop automatically

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
