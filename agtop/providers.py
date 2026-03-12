import json
import os
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class TerminalProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable terminal name."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if this terminal is running and reachable."""

    @abstractmethod
    def get_ttys(self) -> dict[str, Any]:
        """Return a mapping of tty path to provider-specific handle."""

    @abstractmethod
    def activate(self, handle: Any) -> bool:
        """Switch to the pane identified by handle."""


class KakuProvider(TerminalProvider):
    def __init__(self) -> None:
        self._sock = os.environ.get("KAKU_UNIX_SOCKET", "")
        self._bin = "kaku"

    @property
    def name(self) -> str:
        return "Kaku"

    def available(self) -> bool:
        return bool(self._sock)

    def _env(self) -> dict:
        return {**os.environ, "WEZTERM_UNIX_SOCKET": self._sock}

    def get_ttys(self) -> dict[str, int]:
        try:
            output = subprocess.check_output(
                [self._bin, "cli", "list", "--format", "json"],
                env=self._env(),
                text=True,
                timeout=2,
            )
            return {pane["tty_name"]: pane["tab_id"] for pane in json.loads(output)}
        except Exception:
            return {}

    def activate(self, tab_id: int) -> bool:
        try:
            subprocess.run(
                [self._bin, "cli", "activate-tab", "--tab-id", str(tab_id)],
                env=self._env(),
                timeout=2,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


class ITermProvider(TerminalProvider):
    _LIST_SCRIPT = r'''
tell application "iTerm2"
    set output to ""
    repeat with w in every window
        set wid to id of w
        set tabCount to count of tabs of w
        repeat with tabIdx from 1 to tabCount
            set t to tab tabIdx of w
            repeat with s in every session of t
                set output to output & (tty of s) & "\t" & wid & "\t" & tabIdx & linefeed
            end repeat
        end repeat
    end repeat
    return output
end tell
'''

    @property
    def name(self) -> str:
        return "iTerm2"

    def available(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to get (name of processes) contains "iTerm2"',
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return "true" in result.stdout.lower()
        except Exception:
            return False

    def get_ttys(self) -> dict[str, tuple[int, int]]:
        try:
            result = subprocess.run(
                ["osascript", "-e", self._LIST_SCRIPT],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {}
        except Exception:
            return {}

        tty_map: dict[str, tuple[int, int]] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                tty_map[parts[0]] = (int(parts[1]), int(parts[2]))
        return tty_map

    def activate(self, handle: tuple[int, int], tty: str = "") -> bool:
        wid, tab_idx = handle
        script = f'''
tell application "iTerm2"
    activate
    repeat with w in every window
        if id of w is {wid} then
            select w
            set t to tab {tab_idx} of w
            tell w to select t
            repeat with s in every session of t
                if tty of s is "{tty}" then
                    tell t to select s
                end if
            end repeat
            return "ok"
        end if
    end repeat
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


class TmuxProvider(TerminalProvider):
    @property
    def name(self) -> str:
        return "tmux"

    def available(self) -> bool:
        return bool(os.environ.get("TMUX"))

    def get_ttys(self) -> dict[str, str]:
        try:
            output = subprocess.check_output(
                [
                    "tmux", "list-panes", "-a",
                    "-F", "#{pane_tty}\t#{session_name}:#{window_index}.#{pane_index}",
                ],
                text=True,
                timeout=2,
            )
        except Exception:
            return {}
        tty_map: dict[str, str] = {}
        for line in output.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0]:
                tty_map[parts[0]] = parts[1]
        return tty_map

    def activate(self, target: str) -> bool:
        try:
            subprocess.run(
                ["tmux", "switch-client", "-t", target],
                timeout=2,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


class WezTermProvider(TerminalProvider):
    @property
    def name(self) -> str:
        return "WezTerm"

    def available(self) -> bool:
        if os.environ.get("KAKU_UNIX_SOCKET"):
            return False
        try:
            subprocess.run(
                ["wezterm", "cli", "list"],
                capture_output=True,
                timeout=2,
            )
            return True
        except Exception:
            return False

    def get_ttys(self) -> dict[str, int]:
        try:
            output = subprocess.check_output(
                ["wezterm", "cli", "list", "--format", "json"],
                text=True,
                timeout=2,
            )
            return {pane["tty_name"]: pane["tab_id"] for pane in json.loads(output)}
        except Exception:
            return {}

    def activate(self, tab_id: int) -> bool:
        try:
            subprocess.run(
                ["wezterm", "cli", "activate-tab", "--tab-id", str(tab_id)],
                timeout=2,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


class TerminalAppProvider(TerminalProvider):
    _LIST_SCRIPT = r'''
tell application "Terminal"
    set output to ""
    repeat with w in every window
        set wid to id of w
        set tabCount to count of tabs of w
        repeat with tabIdx from 1 to tabCount
            set t to tab tabIdx of w
            set output to output & (tty of t) & "\t" & wid & "\t" & tabIdx & linefeed
        end repeat
    end repeat
    return output
end tell
'''

    @property
    def name(self) -> str:
        return "Terminal"

    def available(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to get (name of processes) contains "Terminal"',
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return "true" in result.stdout.lower()
        except Exception:
            return False

    def get_ttys(self) -> dict[str, tuple[int, int]]:
        try:
            result = subprocess.run(
                ["osascript", "-e", self._LIST_SCRIPT],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {}
        except Exception:
            return {}

        tty_map: dict[str, tuple[int, int]] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                tty_map[parts[0]] = (int(parts[1]), int(parts[2]))
        return tty_map

    def activate(self, handle: tuple[int, int], **_: Any) -> bool:
        wid, tab_idx = handle
        script = f'''
tell application "Terminal"
    activate
    repeat with w in every window
        if id of w is {wid} then
            set selected tab of w to tab {tab_idx} of w
            set index of w to 1
            return "ok"
        end if
    end repeat
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


class WarpProvider(TerminalProvider):
    @property
    def name(self) -> str:
        return "Warp"

    def available(self) -> bool:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "Warp.app"],
                capture_output=True,
                timeout=2,
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_ttys(self) -> dict[str, None]:
        return {}

    def activate(self, handle: Any = None) -> bool:
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Warp" to activate'],
                capture_output=True,
                timeout=3,
            )
            return True
        except Exception:
            return False


def _is_warp_process(pid: str, ps_cache: dict) -> bool:
    cur = pid
    visited: set[str] = set()
    for _ in range(6):
        info = ps_cache.get(cur)
        if not info or cur in visited:
            break
        visited.add(cur)
        if "Warp.app" in info.get("cmd", ""):
            return True
        cur = info.get("ppid", "1")
    return False


_WARP_PROVIDER = WarpProvider()

TERMINAL_PROVIDERS: list[TerminalProvider] = [
    KakuProvider(),
    TmuxProvider(),
    WezTermProvider(),
    ITermProvider(),
    TerminalAppProvider(),
]


def _build_ps_cache() -> dict[str, dict]:
    cache: dict[str, dict] = {}
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,tty,args"],
            text=True,
            timeout=2,
        )
    except Exception:
        return cache

    for line in output.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        cache[parts[0]] = {
            "ppid": parts[1],
            "tty": parts[2],
            "cmd": " ".join(parts[3:]),
        }
    return cache


def _resolve_tty(pid: str, known_ttys: set[str], ps_cache: dict) -> Optional[str]:
    tty = ps_cache.get(pid, {}).get("tty")
    if tty and f"/dev/{tty}" in known_ttys:
        return f"/dev/{tty}"

    cur = pid
    visited: set[str] = set()
    for _ in range(5):
        info = ps_cache.get(cur)
        if not info or cur in visited:
            break
        visited.add(cur)
        ppid = info.get("ppid", "1")
        if ppid in ("1", "0"):
            server_cmd = info.get("cmd", "")
            for pinfo in ps_cache.values():
                cmd = pinfo.get("cmd", "")
                tty_name = pinfo.get("tty", "")
                if not tty_name or tty_name == "??":
                    continue
                if f"/dev/{tty_name}" not in known_ttys:
                    continue
                if ("zellij" in cmd and "zellij" in server_cmd) or (
                    "tmux" in cmd and "tmux" in server_cmd
                ):
                    return f"/dev/{tty_name}"
            break
        cur = ppid
        tty_name = ps_cache.get(cur, {}).get("tty")
        if tty_name and f"/dev/{tty_name}" in known_ttys:
            return f"/dev/{tty_name}"
    return None


def _iter_cli_pids(ps_cache: dict):
    """Yield (pid, cmd, source) for all Claude and Codex CLI processes."""
    for pid, info in ps_cache.items():
        cmd = info["cmd"]
        first_arg = cmd.split()[0] if cmd else ""
        basename = first_arg.rsplit("/", 1)[-1] if first_arg else ""
        # Claude CLI
        if basename == "claude":
            yield pid, cmd, "claude"
            continue
        # Codex CLI native binary (basename is "codex", not the "node" launcher)
        if basename == "codex":
            yield pid, cmd, "codex"


def _pid_cwd(pid: str) -> str:
    try:
        output = subprocess.check_output(
            ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
            text=True,
            timeout=2,
        )
        for line in output.splitlines():
            if line.startswith("n/"):
                return line[1:]
    except Exception:
        pass
    return ""


def _pid_start_time(pid: str) -> float:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", pid],
            text=True,
            timeout=2,
        ).strip()
        return datetime.strptime(output, "%a %b %d %H:%M:%S %Y").timestamp()
    except Exception:
        return 0


def _match_session_to_pid(
    cwd: str,
    session_id: str,
    birthtime: float,
    ps_cache: dict,
) -> Optional[str]:
    if session_id:
        for pid, cmd, _ in _iter_cli_pids(ps_cache):
            if f"--resume {session_id}" in cmd:
                return pid

    target = cwd.rstrip("/")
    if not target:
        return None

    candidates: list[str] = []
    for pid, _, _ in _iter_cli_pids(ps_cache):
        if _pid_cwd(pid).rstrip("/") == target:
            candidates.append(pid)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1 and birthtime > 0:
        best_pid = None
        best_diff = float("inf")
        for pid in candidates:
            process_start = _pid_start_time(pid)
            if process_start > 0 and process_start <= birthtime:
                diff = birthtime - process_start
                if diff < best_diff:
                    best_diff = diff
                    best_pid = pid
        if best_pid:
            return best_pid

    return None


def jump_to_session(
    cwd: str,
    session_id: str = "",
    birthtime: float = 0,
) -> tuple[bool, str]:
    if not cwd:
        return False, "No cwd"

    ps_cache = _build_ps_cache()
    exact_pid = _match_session_to_pid(cwd, session_id, birthtime, ps_cache)

    for provider in TERMINAL_PROVIDERS:
        if not provider.available():
            continue
        tty_map = provider.get_ttys()
        if not tty_map:
            continue
        known_ttys = set(tty_map.keys())

        if exact_pid:
            resolved = _resolve_tty(exact_pid, known_ttys, ps_cache)
        else:
            target = cwd.rstrip("/")
            resolved = None
            for pid, _, _ in _iter_cli_pids(ps_cache):
                if _pid_cwd(pid).rstrip("/") == target:
                    resolved = _resolve_tty(pid, known_ttys, ps_cache)
                    if resolved:
                        break

        if resolved and resolved in tty_map:
            handle = tty_map[resolved]
            if isinstance(provider, ITermProvider):
                ok = provider.activate(handle, tty=resolved)
            else:
                ok = provider.activate(handle)
            if ok:
                return True, provider.name

    if _WARP_PROVIDER.available():
        target = cwd.rstrip("/")
        for pid, _, _ in _iter_cli_pids(ps_cache):
            if _pid_cwd(pid).rstrip("/") == target and _is_warp_process(pid, ps_cache):
                if _WARP_PROVIDER.activate():
                    return True, "Warp"

    return False, "not found"


def has_active_children(pid: str) -> bool:
    """Check if a process has actively running child processes.

    When waiting for permission: claude is blocked on stdin, no children.
    When tool is executing: there's a child process (shell, node, etc).
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", pid],
            text=True, timeout=2,
        )
        return bool(out.strip())
    except Exception:
        return False


def get_live_session_ids(sessions: list[dict]) -> dict[str, str]:
    """Return {session_id: pid} for sessions with a running claude process.

    Direction: for each process, find the best matching session (not vice versa).
    One process → at most one session_id marked alive.

    Matching tiers (same as jump_to_session):
      1. --resume <session_id> in args  (exact)
      2. only one session for this CWD  (unique)
      3. multiple sessions → closest birthtime after process start
    """
    ps_cache = _build_ps_cache()
    cli_pids = list(_iter_cli_pids(ps_cache))
    if not cli_pids:
        return set()

    # One lsof call for all claude PIDs → {pid: cwd}
    all_pids = [pid for pid, _, _ in cli_pids]
    pid_cwd: dict[str, str] = {}
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-p", ",".join(all_pids), "-d", "cwd", "-Fn"],
            text=True, timeout=3,
        )
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = line[1:]
            elif line.startswith("n") and cur:
                pid_cwd[cur] = line[1:].rstrip("/")
    except Exception:
        return set()

    # Build session indexes
    from collections import defaultdict
    sessions_by_cwd: dict[str, list[dict]] = defaultdict(list)
    sessions_by_id: dict[str, dict] = {}
    for s in sessions:
        sessions_by_cwd[s["cwd"].rstrip("/")].append(s)
        sessions_by_id[s["session_id"]] = s

    live_map: dict[str, str] = {}  # session_id → pid

    for pid, cmd, _ in cli_pids:
        cwd = pid_cwd.get(pid, "").rstrip("/")
        if not cwd:
            continue

        # Tier 1: --resume <session_id> in args
        matched = False
        for sid in sessions_by_id:
            if f"--resume {sid}" in cmd:
                live_map[sid] = pid
                matched = True
                break
        if matched:
            continue

        cwd_sessions = sessions_by_cwd.get(cwd, [])
        if not cwd_sessions:
            continue

        # Tier 2: only one session for this CWD
        if len(cwd_sessions) == 1:
            live_map[cwd_sessions[0]["session_id"]] = pid
            continue

        # Tier 3: multiple sessions → process started just before session file created
        pstart = _pid_start_time(pid)
        if pstart > 0:
            best_sid, best_diff = None, float("inf")
            for s in cwd_sessions:
                bt = s.get("_birthtime", 0)
                if bt > 0 and pstart <= bt:
                    diff = bt - pstart
                    if diff < best_diff:
                        best_diff, best_sid = diff, s["session_id"]
            if best_sid:
                live_map[best_sid] = pid

    return live_map
