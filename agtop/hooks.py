import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EVENTS_DIR = Path.home() / ".config" / "agtop" / "events"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

HOOK_EVENTS = ("prompt", "notification", "stop")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _read_hook_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("hook payload is required on stdin")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook payload must be a JSON object")
    return data


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, indent=2, ensure_ascii=True)
        file_obj.write("\n")
    tmp_path.replace(path)


def _process_tree() -> dict[int, dict[str, Any]]:
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,comm=,args="],
            text=True,
            timeout=2,
        )
    except Exception:
        return {}

    tree: dict[int, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        tree[pid] = {
            "ppid": ppid,
            "comm": parts[2],
            "args": parts[3] if len(parts) > 3 else "",
        }
    return tree


def _find_agent_pid(tree: dict[int, dict[str, Any]]) -> int:
    current = os.getpid()
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        info = tree.get(current)
        if not info:
            break
        comm = str(info.get("comm", ""))
        args = str(info.get("args", ""))
        cmd = Path(args.split()[0]).name if args else comm
        if comm == "claude" or cmd == "claude":
            return current
        current = int(info.get("ppid", 0))
    return os.getppid()


def _detect_tty() -> str:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            return os.ttyname(stream.fileno())
        except (AttributeError, OSError):
            continue

    try:
        output = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(os.getpid())],
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return ""

    if not output or output == "??":
        return ""
    return output if output.startswith("/dev/") else f"/dev/{output}"


def _detect_term_program(tree: dict[int, dict[str, Any]]) -> str:
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    if term_program:
        return term_program

    known = {
        "iTerm2": "iTerm2",
        "Terminal": "Terminal",
        "Warp": "Warp",
        "wezterm-gui": "WezTerm",
        "kaku-gui": "Kaku",
        "tmux": "tmux",
    }

    current = os.getpid()
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        info = tree.get(current)
        if not info:
            break
        comm = str(info.get("comm", ""))
        if comm in known:
            return known[comm]
        current = int(info.get("ppid", 0))

    return ""


def _detect_terminal_info(tree: dict[int, dict[str, Any]]) -> dict[str, Any]:
    window_id = (
        os.environ.get("WINDOWID")
        or os.environ.get("TERM_SESSION_ID")
        or os.environ.get("ITERM_SESSION_ID")
        or ""
    )
    info = {
        "tty": _detect_tty(),
        "term_program": _detect_term_program(tree),
        "window_id": window_id,
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "iterm_session": os.environ.get("ITERM_SESSION_ID", ""),
        "term_session_id": os.environ.get("TERM_SESSION_ID", ""),
        "wezterm_pane": os.environ.get("WEZTERM_PANE", ""),
    }
    return {key: value for key, value in info.items() if value}


def _merge_terminal_info(existing: Any, current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(current)
    return merged


def _session_id_from_payload(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id", "")).strip()
    if session_id:
        return session_id

    transcript_path = str(payload.get("transcript_path", "")).strip()
    if transcript_path:
        return Path(transcript_path).stem

    raise ValueError("session_id is missing from hook payload")


def _status_for_notification(
    payload: dict[str, Any],
    existing_status: str,
) -> str:
    notification_type = str(payload.get("notification_type", "")).strip()
    message = str(payload.get("message", "")).lower()
    title = str(payload.get("title", "")).lower()

    if notification_type == "permission_prompt":
        return "waiting_permission"
    if notification_type in {"idle_prompt", "elicitation_dialog"}:
        return "waiting_question"
    if "permission" in title or "permission" in message:
        return "waiting_permission"
    if "waiting for your input" in message or "input" in message:
        return "waiting_question"
    return existing_status or "active"


def run_hook(event: str) -> int:
    if event not in HOOK_EVENTS:
        raise ValueError(f"unsupported hook event: {event}")

    payload = _read_hook_payload()
    session_id = _session_id_from_payload(payload)
    event_path = EVENTS_DIR / f"{session_id}.json"

    existing: dict[str, Any]
    try:
        existing = _read_json_file(event_path)
    except (OSError, json.JSONDecodeError, ValueError):
        existing = {}

    now = int(time.time())

    # Stop hook: 只更新状态，跳过耗时的进程树检测
    if event == "stop":
        state = {
            **existing,
            "last_event": event,
            "last_event_ts": now,
            "status": "done",
            "stop_ts": now,
            "notification_type": None,
            "message": None,
            "title": None,
        }
        _write_json_file(event_path, state)
        return 0

    tree = _process_tree()
    terminal = _merge_terminal_info(
        existing.get("terminal"),
        _detect_terminal_info(tree),
    )
    state = {
        "session_id": session_id,
        "pid": _find_agent_pid(tree),
        "cwd": str(payload.get("cwd") or existing.get("cwd") or ""),
        "transcript_path": str(
            payload.get("transcript_path") or existing.get("transcript_path") or "",
        ),
        "terminal": terminal,
        "last_event": event,
        "last_event_ts": now,
        "stop_ts": existing.get("stop_ts"),
    }

    if event == "prompt":
        state["status"] = "working"
        state["stop_ts"] = None
        state["notification_type"] = None
        state["message"] = None
        state["title"] = None
    else:  # notification
        state["status"] = _status_for_notification(
            payload,
            str(existing.get("status", "")),
        )
        state["notification_type"] = payload.get("notification_type")
        state["message"] = payload.get("message")
        state["title"] = payload.get("title")

    _write_json_file(event_path, state)
    return 0


def _resolve_install_launcher() -> str | None:
    argv0 = sys.argv[0].strip()
    if argv0:
        resolved = shutil.which(argv0)
        if not resolved:
            argv0_path = Path(argv0).expanduser()
            if argv0_path.exists():
                resolved = str(argv0_path.resolve())
        if resolved:
            launcher = Path(resolved).resolve()
            if launcher.is_file() and not launcher.name.lower().startswith("python"):
                return str(launcher)

    venv_launcher = REPO_ROOT / ".venv" / "bin" / "agtop"
    if venv_launcher.is_file():
        return str(venv_launcher)

    return None


def _build_hook_command(event: str) -> str:
    launcher = _resolve_install_launcher()
    if launcher:
        return f"{shlex.quote(launcher)} --hook {event}"

    python_exe = shlex.quote(str(Path(sys.executable).resolve()))
    pythonpath = shlex.quote(str(REPO_ROOT))
    return f"PYTHONPATH={pythonpath} {python_exe} -m agtop --hook {event}"


def _desired_claude_hooks() -> dict[str, list[dict[str, Any]]]:
    return {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _build_hook_command("prompt"),
                    },
                ],
            },
        ],
        "Notification": [
            {
                "matcher": "permission_prompt",
                "hooks": [
                    {
                        "type": "command",
                        "command": _build_hook_command("notification"),
                    },
                ],
            },
            {
                "matcher": "idle_prompt|elicitation_dialog",
                "hooks": [
                    {
                        "type": "command",
                        "command": _build_hook_command("notification"),
                    },
                ],
            },
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _build_hook_command("stop"),
                    },
                ],
            },
        ],
    }


def _strip_env_assignments(parts: list[str]) -> list[str]:
    index = 0
    while index < len(parts):
        part = parts[index]
        key, sep, _ = part.partition("=")
        if sep != "=" or not key.isidentifier():
            break
        index += 1
    return parts[index:]


def _is_agtop_hook_command(command: Any, *, event: str) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False

    try:
        parts = shlex.split(command)
    except ValueError:
        return False

    tail = _strip_env_assignments(parts)
    if len(tail) >= 3 and Path(tail[0]).name == "agtop":
        return tail[1:3] == ["--hook", event]
    if len(tail) >= 5:
        return tail[1:5] == ["-m", "agtop", "--hook", event]
    return False


def _ensure_group_handler(
    group: Any,
    *,
    command: str,
    matcher: str | None,
    event: str,
) -> tuple[bool, bool]:
    if not isinstance(group, dict):
        return False, False

    group_matcher = group.get("matcher")
    if matcher is not None and group_matcher != matcher:
        return False, False
    if matcher is None and group_matcher not in (None, ""):
        return False, False

    hooks = group.get("hooks")
    if isinstance(hooks, list):
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            if hook.get("type", "command") != "command":
                continue
            existing_command = hook.get("command")
            if existing_command == command:
                return True, False
            if _is_agtop_hook_command(existing_command, event=event):
                hook["command"] = command
                return True, True

    existing_command = group.get("command")
    if matcher is None and isinstance(existing_command, str):
        if existing_command == command:
            return True, False
        if _is_agtop_hook_command(existing_command, event=event):
            group["command"] = command
            return True, True

    return False, False


def install_claude_hooks() -> tuple[Path, bool]:
    settings = _read_json_file(CLAUDE_SETTINGS_PATH)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{CLAUDE_SETTINGS_PATH} must contain an object at hooks")

    changed = False
    desired_hooks = _desired_claude_hooks()
    hook_events = {
        "UserPromptSubmit": "prompt",
        "Notification": "notification",
        "Stop": "stop",
    }
    for hook_name, desired_groups in desired_hooks.items():
        existing_groups = hooks.setdefault(hook_name, [])
        if not isinstance(existing_groups, list):
            raise ValueError(
                f"{CLAUDE_SETTINGS_PATH} hooks.{hook_name} must be a JSON array",
            )

        event = hook_events[hook_name]
        for desired_group in desired_groups:
            desired_matcher = desired_group.get("matcher")
            desired_handlers = desired_group.get("hooks", [])
            command = ""
            if desired_handlers and isinstance(desired_handlers[0], dict):
                command = str(desired_handlers[0].get("command", ""))
            if not command:
                continue

            already_present = False
            for group in existing_groups:
                present, updated = _ensure_group_handler(
                    group,
                    command=command,
                    matcher=desired_matcher,
                    event=event,
                )
                changed = changed or updated
                if present:
                    already_present = True
                    break

            if already_present:
                continue

            existing_groups.append(desired_group)
            changed = True

    _write_json_file(CLAUDE_SETTINGS_PATH, settings)
    return CLAUDE_SETTINGS_PATH, changed
