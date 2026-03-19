import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import load_config
from .hooks import EVENTS_DIR

_cfg = load_config()

CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
ACTIVE_THRESHOLD = 5 * 60
WORKING_THRESHOLD = 30
WAITING_GRACE = 10
RECENT_THRESHOLD = 7 * 24 * 60 * 60  # 7 days — parse cache guard
SHOW_RECENT = _cfg["show_recent_hours"] * 3600
MAX_SESSIONS = _cfg["max_sessions"]
TAIL_BYTES = 256 * 1024
HEAD_LINES = 10
REFRESH_FAST = _cfg["refresh_fast"]
REFRESH_SLOW = _cfg["refresh_slow"]
WAITING_STATUSES = {"waiting_question", "waiting_permission"}
KNOWN_STATUSES = {"working", "active", "done", *WAITING_STATUSES}


def _read_head_tail(
    path: Path,
    head_n: int = HEAD_LINES,
    tail_bytes: int = TAIL_BYTES,
) -> tuple[list[str], list[str]]:
    size = path.stat().st_size
    head_lines: list[str] = []
    tail_lines: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as file_obj:
        for _ in range(head_n):
            line = file_obj.readline()
            if not line:
                break
            head_lines.append(line.rstrip("\n"))
        if size <= tail_bytes:
            tail_lines = [line.rstrip("\n") for line in file_obj.readlines()]
        else:
            file_obj.seek(max(0, size - tail_bytes))
            file_obj.readline()
            tail_lines = [line.rstrip("\n") for line in file_obj.readlines()]
    return head_lines, tail_lines


def _compute_status(age: float, pending_tool: bool, pending_tool_name: str) -> str:
    if pending_tool and pending_tool_name == "AskUserQuestion":
        if age <= ACTIVE_THRESHOLD:
            return "waiting_question"

    if pending_tool and age > WAITING_GRACE and age <= ACTIVE_THRESHOLD:
        return "waiting_permission"

    if age <= WORKING_THRESHOLD:
        return "working"
    if age <= ACTIVE_THRESHOLD:
        return "active"
    return "done"


def _read_event_state(
    session_id: str,
) -> tuple[Optional[dict[str, Any]], float, int]:
    path = EVENTS_DIR / f"{session_id}.json"
    try:
        stat_result = path.stat()
    except OSError:
        return None, 0, 0

    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return None, 0, 0

    if not isinstance(data, dict):
        return None, 0, 0
    return data, stat_result.st_mtime, stat_result.st_size


def _event_epoch(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _waiting_status_from_event(event_state: dict[str, Any]) -> str:
    status = str(event_state.get("status", "")).strip()
    if status in WAITING_STATUSES:
        return status

    notification_type = str(event_state.get("notification_type", "")).strip()
    message = str(event_state.get("message", "")).lower()
    title = str(event_state.get("title", "")).lower()

    if notification_type == "permission_prompt":
        return "waiting_permission"
    if notification_type in {"idle_prompt", "elicitation_dialog"}:
        return "waiting_question"
    if "permission" in title or "permission" in message:
        return "waiting_permission"
    return "waiting_question"


def _compute_status_from_event(
    event_state: dict[str, Any],
    now: float,
) -> Optional[str]:
    if _event_epoch(event_state.get("stop_ts")) is not None:
        return "done"

    last_event = str(event_state.get("last_event", "")).strip()
    last_event_ts = _event_epoch(event_state.get("last_event_ts"))

    if last_event == "notification":
        return _waiting_status_from_event(event_state)

    if last_event == "prompt":
        if last_event_ts is not None and now - last_event_ts <= WORKING_THRESHOLD:
            return "working"
        return "active"

    status = str(event_state.get("status", "")).strip()
    if status not in KNOWN_STATUSES:
        return None
    if status == "working":
        if last_event_ts is not None and now - last_event_ts <= WORKING_THRESHOLD:
            return "working"
        return "active"
    return status


def _extract_user_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            if isinstance(block, dict) and block.get("type") == "text"
            else block if isinstance(block, str) else ""
            for block in content
        ).strip()
    return ""


def _tool_summary(name: str, inp: dict) -> str:
    if not name:
        return ""
    summary = name
    if name in ("Read", "Write", "Edit") and inp.get("file_path"):
        summary += f"  {Path(inp['file_path']).name}"
    elif name == "Bash" and inp.get("command"):
        summary += f"  {inp['command'][:35]}"
    elif name in ("exec_command",) and inp.get("cmd"):
        summary += f"  {inp['cmd'][:35]}"
    elif name == "Grep" and inp.get("pattern"):
        summary += f"  /{inp['pattern'][:25]}/"
    elif name == "Glob" and inp.get("pattern"):
        summary += f"  {inp['pattern'][:25]}"
    elif name == "Agent" and inp.get("description"):
        summary += f"  {inp['description'][:25]}"
    elif name == "WebSearch" and inp.get("query"):
        summary += f"  {inp['query'][:25]}"
    return summary


def _parse_ts(ts_str: str) -> Optional[float]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── Claude JSONL parsing ─────────────────────────────────

def _parse_claude_lines(all_lines: list[str]) -> dict:
    """Parse Claude Code JSONL lines → intermediate state dict."""
    cwd = ""
    user_text = ""
    user_epoch: Optional[float] = None
    assistant_text = ""
    tool_name = ""
    tool_input: dict = {}
    pending_tool = False
    pending_tool_name = ""
    turns: list[dict] = []

    for raw in all_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "file-history-snapshot":
            continue
        if not cwd and obj.get("cwd"):
            cwd = obj["cwd"]

        epoch = _parse_ts(obj.get("timestamp", ""))
        msg = obj.get("message", {})
        role = msg.get("role", "")

        if role == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        pending_tool = False
                        pending_tool_name = ""

            text = _extract_user_text(msg)
            if text:
                user_text, user_epoch = text, epoch
                assistant_text = ""
                tool_name, tool_input = "", {}
                pending_tool = False
                pending_tool_name = ""
                turns.append({"role": "user", "text": text})

        elif role == "assistant":
            content = msg.get("content", [])
            has_tool_use = False
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            assistant_text = text
                            turns.append({"role": "assistant", "text": text})
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        has_tool_use = True
                        pending_tool_name = tool_name
                        turns.append({
                            "role": "tool",
                            "summary": _tool_summary(tool_name, tool_input),
                        })
            elif isinstance(content, str) and content.strip():
                assistant_text = content.strip()
                turns.append({"role": "assistant", "text": assistant_text})

            if has_tool_use:
                pending_tool = True

    return {
        "cwd": cwd, "user_text": user_text, "user_epoch": user_epoch,
        "assistant_text": assistant_text, "tool_name": tool_name,
        "tool_input": tool_input, "pending_tool": pending_tool,
        "pending_tool_name": pending_tool_name, "turns": turns,
        "source": "claude",
    }


# ── Codex JSONL parsing ──────────────────────────────────

def _parse_codex_lines(all_lines: list[str]) -> dict:
    """Parse Codex CLI JSONL lines → intermediate state dict."""
    cwd = ""
    user_text = ""
    user_epoch: Optional[float] = None
    assistant_text = ""
    tool_name = ""
    tool_input: dict = {}
    pending_tool = False
    pending_tool_name = ""
    turns: list[dict] = []
    pending_call_ids: set[str] = set()

    for raw in all_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        entry_type = obj.get("type", "")
        payload = obj.get("payload", {})
        epoch = _parse_ts(obj.get("timestamp", ""))

        if entry_type == "session_meta":
            if not cwd and payload.get("cwd"):
                cwd = payload["cwd"]

        elif entry_type == "turn_context":
            if not cwd and payload.get("cwd"):
                cwd = payload["cwd"]

        elif entry_type == "response_item":
            p_type = payload.get("type", "")
            role = payload.get("role", "")

            if p_type == "message":
                content = payload.get("content", [])
                if role == "user":
                    # Extract user text from input_text blocks
                    texts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "input_text":
                            texts.append(block.get("text", ""))
                    text = "\n".join(texts).strip()
                    if text:
                        user_text, user_epoch = text, epoch
                        assistant_text = ""
                        tool_name, tool_input = "", {}
                        pending_tool = False
                        pending_tool_name = ""
                        pending_call_ids.clear()
                        turns.append({"role": "user", "text": text})

                elif role == "assistant":
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "output_text":
                            text = block.get("text", "").strip()
                            if text:
                                assistant_text = text
                                turns.append({"role": "assistant", "text": text})

            elif p_type == "function_call":
                fname = payload.get("name", "")
                try:
                    fargs = json.loads(payload.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    fargs = {}
                call_id = payload.get("call_id", "")
                tool_name, tool_input = fname, fargs
                pending_tool = True
                pending_tool_name = fname
                if call_id:
                    pending_call_ids.add(call_id)
                turns.append({
                    "role": "tool",
                    "summary": _tool_summary(fname, fargs),
                })

            elif p_type == "function_call_output":
                call_id = payload.get("call_id", "")
                pending_call_ids.discard(call_id)
                if not pending_call_ids:
                    pending_tool = False
                    pending_tool_name = ""

    return {
        "cwd": cwd, "user_text": user_text, "user_epoch": user_epoch,
        "assistant_text": assistant_text, "tool_name": tool_name,
        "tool_input": tool_input, "pending_tool": pending_tool,
        "pending_tool_name": pending_tool_name, "turns": turns,
        "source": "codex",
    }


# ── Unified parser ────────────────────────────────────────

class SessionParser:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, int, float, int, dict]] = {}

    def parse(self, path: Path, source: str = "claude") -> Optional[dict]:
        try:
            stat_result = path.stat()
            mtime, size = stat_result.st_mtime, stat_result.st_size
            birthtime = getattr(stat_result, "st_birthtime", mtime)
            now = time.time()
            age = now - mtime
            if age > RECENT_THRESHOLD:
                self._cache.pop(str(path), None)
                return None

            event_state, event_mtime, event_size = _read_event_state(path.stem)

            key = str(path)
            if key in self._cache:
                (
                    cached_mtime,
                    cached_size,
                    cached_event_mtime,
                    cached_event_size,
                    cached_result,
                ) = self._cache[key]
                if (
                    cached_mtime == mtime
                    and cached_size == size
                    and cached_event_mtime == event_mtime
                    and cached_event_size == event_size
                ):
                    result = cached_result.copy()
                    result["age"] = age
                    event_state = result.get("_event_state")
                    event_status = None
                    if isinstance(event_state, dict):
                        event_status = _compute_status_from_event(
                            event_state,
                            now,
                        )
                    if event_status is not None:
                        result["status"] = event_status
                    else:
                        result["status"] = _compute_status(
                            age,
                            result.get("_pending_tool"),
                            result.get("_pending_tool_name"),
                        )
                    if result["_task_ep"] and result["status"] == "working":
                        result["task_runtime"] = now - result["_task_ep"]
                    else:
                        result["task_runtime"] = None
                    return result

            head_lines, tail_lines = _read_head_tail(path)
            all_lines = head_lines + tail_lines
            if not all_lines:
                return None

            if source == "codex":
                parsed = _parse_codex_lines(all_lines)
            else:
                parsed = _parse_claude_lines(all_lines)

            cwd = parsed["cwd"]
            user_text = parsed["user_text"]
            assistant_text = parsed["assistant_text"]

            if not cwd and not user_text and not assistant_text:
                return None

            project = (
                Path(cwd).name
                if cwd
                else path.parent.name.lstrip("-").replace("-", "/")
            )

            status = _compute_status(
                age, parsed["pending_tool"], parsed["pending_tool_name"],
            )
            event_status = None
            if event_state is not None:
                event_status = _compute_status_from_event(event_state, now)
                if event_status is not None:
                    status = event_status

            task_runtime = None
            if parsed["user_epoch"] and status == "working":
                task_runtime = now - parsed["user_epoch"]

            result = {
                "session_id": path.stem,
                "project": project,
                "cwd": cwd,
                "task": user_text,
                "_task_ep": parsed["user_epoch"],
                "_pending_tool": parsed["pending_tool"],
                "_pending_tool_name": parsed["pending_tool_name"],
                "task_runtime": task_runtime,
                "mtime": mtime,
                "_birthtime": birthtime,
                "age": age,
                "status": status,
                "last_text": assistant_text,
                "tool_summary": _tool_summary(
                    parsed["tool_name"], parsed["tool_input"],
                ),
                "turns": parsed["turns"],
                "source": parsed["source"],
                "_event_state": event_state,
            }
            self._cache[key] = (
                mtime,
                size,
                event_mtime,
                event_size,
                result.copy(),
            )
            return result
        except Exception:
            return None
