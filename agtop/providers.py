import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from .hooks import EVENTS_DIR


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str, timeout: int = 5) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False


def _pgrep_running(*args: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", *args],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


def _load_terminal_info(session_id: str) -> Optional[dict[str, Any]]:
    if not session_id:
        return None

    path = EVENTS_DIR / f"{session_id}.json"
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return None

    terminal = data.get("terminal")
    if not isinstance(terminal, dict) or not terminal:
        return None
    return terminal


def _normalize_term_program(term_program: Any) -> str:
    value = str(term_program or "").strip().lower()
    if not value:
        return ""
    if "kaku" in value:
        return "kaku"
    if "wezterm" in value:
        return "wezterm"
    if "iterm" in value:
        return "iterm2"
    if value in {"terminal", "apple_terminal"}:
        return "terminal"
    if "warp" in value:
        return "warp"
    return value


class TerminalProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable terminal name."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if this terminal is running and reachable."""

    @abstractmethod
    def activate(self, terminal_info: dict[str, Any]) -> bool:
        """Switch to the session identified by hook-provided terminal info."""


class KakuProvider(TerminalProvider):
    def __init__(self) -> None:
        self._bin = "kaku"

    @property
    def name(self) -> str:
        return "Kaku"

    def available(self) -> bool:
        return shutil.which(self._bin) is not None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        sock = env.get("KAKU_UNIX_SOCKET", "")
        if sock:
            env["WEZTERM_UNIX_SOCKET"] = sock
        return env

    def _list_panes(self) -> list[dict[str, Any]]:
        try:
            output = subprocess.check_output(
                [self._bin, "cli", "list", "--format", "json"],
                env=self._env(),
                text=True,
                timeout=2,
            )
            panes = json.loads(output)
        except Exception:
            return []
        return panes if isinstance(panes, list) else []

    def _resolve_target(self, terminal_info: dict[str, Any]) -> tuple[str, str] | None:
        pane_id = str(terminal_info.get("wezterm_pane", "")).strip()
        if pane_id:
            return "pane", pane_id

        tty = str(terminal_info.get("tty", "")).strip()
        if not tty:
            return None

        for pane in self._list_panes():
            if str(pane.get("tty_name", "")).strip() != tty:
                continue
            direct_pane_id = pane.get("pane_id")
            if direct_pane_id not in (None, ""):
                return "pane", str(direct_pane_id)
            tab_id = pane.get("tab_id")
            if tab_id not in (None, ""):
                return "tab", str(tab_id)
        return None

    def activate(self, terminal_info: dict[str, Any]) -> bool:
        target = self._resolve_target(terminal_info)
        if target is None:
            return False

        kind, target_id = target
        cmd = [self._bin, "cli", "activate-pane", "--pane-id", target_id]
        if kind == "tab":
            cmd = [self._bin, "cli", "activate-tab", "--tab-id", target_id]

        try:
            subprocess.run(
                cmd,
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
        repeat with t in every tab of w
            repeat with s in every session of t
                set output to output & (tty of s) & linefeed
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
        return _pgrep_running("-x", "iTerm2")

    def activate(self, terminal_info: dict[str, Any]) -> bool:
        session_id = str(terminal_info.get("iterm_session", "")).strip()
        tty = str(terminal_info.get("tty", "")).strip()
        if not session_id and not tty:
            return False

        match = (
            f'id of s is "{_escape_applescript_string(session_id)}"'
            if session_id
            else f'tty of s is "{_escape_applescript_string(tty)}"'
        )
        script = f'''
tell application "iTerm2"
    activate
    repeat with w in every window
        repeat with t in every tab of w
            repeat with s in every session of t
                if {match} then
                    select w
                    tell w to select t
                    tell t to select s
                    return "ok"
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
        return _run_osascript(script)


class TmuxProvider(TerminalProvider):
    @property
    def name(self) -> str:
        return "tmux"

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def _resolve_target(self, terminal_info: dict[str, Any]) -> str:
        pane = str(terminal_info.get("tmux_pane", "")).strip()
        if pane:
            return pane

        tty = str(terminal_info.get("tty", "")).strip()
        if not tty:
            return ""

        try:
            output = subprocess.check_output(
                [
                    "tmux", "list-panes", "-a",
                    "-F", "#{pane_tty}\t#{pane_id}",
                ],
                text=True,
                timeout=2,
            )
        except Exception:
            return ""

        for line in output.splitlines():
            pane_tty, _, pane_id = line.partition("\t")
            if pane_tty == tty:
                return pane_id.strip()
        return ""

    def activate(self, terminal_info: dict[str, Any]) -> bool:
        target = self._resolve_target(terminal_info)
        if not target:
            return False

        try:
            window_target = subprocess.check_output(
                [
                    "tmux", "display-message", "-p", "-t", target,
                    "#{session_name}:#{window_index}",
                ],
                text=True,
                timeout=2,
            ).strip()
        except Exception:
            window_target = ""

        if window_target:
            try:
                subprocess.run(
                    ["tmux", "switch-client", "-t", window_target.split(":", 1)[0]],
                    timeout=2,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

            try:
                subprocess.run(
                    ["tmux", "select-window", "-t", window_target],
                    timeout=2,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                return False

        try:
            subprocess.run(
                ["tmux", "select-pane", "-t", target],
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
        return shutil.which("wezterm") is not None

    def _list_panes(self) -> list[dict[str, Any]]:
        try:
            output = subprocess.check_output(
                ["wezterm", "cli", "list", "--format", "json"],
                text=True,
                timeout=2,
            )
            panes = json.loads(output)
        except Exception:
            return []
        return panes if isinstance(panes, list) else []

    def _resolve_target(self, terminal_info: dict[str, Any]) -> tuple[str, str] | None:
        pane_id = str(terminal_info.get("wezterm_pane", "")).strip()
        if pane_id:
            return "pane", pane_id

        tty = str(terminal_info.get("tty", "")).strip()
        if not tty:
            return None

        for pane in self._list_panes():
            if str(pane.get("tty_name", "")).strip() != tty:
                continue
            direct_pane_id = pane.get("pane_id")
            if direct_pane_id not in (None, ""):
                return "pane", str(direct_pane_id)
            tab_id = pane.get("tab_id")
            if tab_id not in (None, ""):
                return "tab", str(tab_id)
        return None

    def activate(self, terminal_info: dict[str, Any]) -> bool:
        target = self._resolve_target(terminal_info)
        if target is None:
            return False

        kind, target_id = target
        cmd = ["wezterm", "cli", "activate-pane", "--pane-id", target_id]
        if kind == "tab":
            cmd = ["wezterm", "cli", "activate-tab", "--tab-id", target_id]

        try:
            subprocess.run(
                cmd,
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
        repeat with t in every tab of w
            set output to output & (tty of t) & linefeed
        end repeat
    end repeat
    return output
end tell
'''

    @property
    def name(self) -> str:
        return "Terminal"

    def available(self) -> bool:
        return _pgrep_running("-x", "Terminal")

    def activate(self, terminal_info: dict[str, Any]) -> bool:
        tty = str(terminal_info.get("tty", "")).strip()
        if not tty:
            return False

        script = f'''
tell application "Terminal"
    activate
    repeat with w in every window
        repeat with t in every tab of w
            if tty of t is "{_escape_applescript_string(tty)}" then
                set selected tab of w to t
                set index of w to 1
                return "ok"
            end if
        end repeat
    end repeat
end tell
'''
        return _run_osascript(script)


class WarpProvider(TerminalProvider):
    @property
    def name(self) -> str:
        return "Warp"

    def available(self) -> bool:
        return _pgrep_running("-f", "Warp.app")

    def activate(self, terminal_info: dict[str, Any]) -> bool:
        return _run_osascript('tell application "Warp" to activate', timeout=3)


_PROVIDER_BY_KEY: dict[str, TerminalProvider] = {
    "kaku": KakuProvider(),
    "tmux": TmuxProvider(),
    "wezterm": WezTermProvider(),
    "iterm2": ITermProvider(),
    "terminal": TerminalAppProvider(),
    "warp": WarpProvider(),
}
TERMINAL_PROVIDERS: list[TerminalProvider] = list(_PROVIDER_BY_KEY.values())


def _append_provider(
    providers: list[TerminalProvider],
    key: str,
) -> None:
    provider = _PROVIDER_BY_KEY.get(key)
    if provider is not None and provider not in providers:
        providers.append(provider)


def _candidate_providers(terminal_info: dict[str, Any]) -> list[TerminalProvider]:
    providers: list[TerminalProvider] = []
    normalized = _normalize_term_program(terminal_info.get("term_program"))

    if terminal_info.get("tmux_pane"):
        _append_provider(providers, "tmux")

    if normalized:
        _append_provider(providers, normalized)

    if terminal_info.get("iterm_session"):
        _append_provider(providers, "iterm2")

    if terminal_info.get("wezterm_pane"):
        if normalized == "wezterm":
            _append_provider(providers, "wezterm")
            _append_provider(providers, "kaku")
        else:
            _append_provider(providers, "kaku")
            _append_provider(providers, "wezterm")

    if terminal_info.get("term_session_id"):
        _append_provider(providers, "terminal")

    if terminal_info.get("tty"):
        for key in ("kaku", "wezterm", "iterm2", "terminal"):
            _append_provider(providers, key)

    if normalized == "warp":
        _append_provider(providers, "warp")

    return providers


def _provider_known_ttys(provider: TerminalProvider) -> set[str]:
    if isinstance(provider, KakuProvider):
        return {
            str(pane.get("tty_name", "")).strip()
            for pane in provider._list_panes()
            if str(pane.get("tty_name", "")).strip()
        }

    if isinstance(provider, WezTermProvider):
        return {
            str(pane.get("tty_name", "")).strip()
            for pane in provider._list_panes()
            if str(pane.get("tty_name", "")).strip()
        }

    if isinstance(provider, TmuxProvider):
        try:
            output = subprocess.check_output(
                ["tmux", "list-panes", "-a", "-F", "#{pane_tty}"],
                text=True,
                timeout=2,
            )
        except Exception:
            return set()
        return {line.strip() for line in output.splitlines() if line.strip()}

    if isinstance(provider, ITermProvider):
        try:
            output = subprocess.check_output(
                ["osascript", "-e", provider._LIST_SCRIPT],
                text=True,
                timeout=5,
            )
        except Exception:
            return set()
        return {line.strip() for line in output.splitlines() if line.strip()}

    if isinstance(provider, TerminalAppProvider):
        try:
            output = subprocess.check_output(
                ["osascript", "-e", provider._LIST_SCRIPT],
                text=True,
                timeout=5,
            )
        except Exception:
            return set()
        return {line.strip() for line in output.splitlines() if line.strip()}

    return set()


def _is_warp_process(pid: str, ps_cache: dict[str, dict[str, str]]) -> bool:
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


def _resolve_tty(
    pid: str,
    known_ttys: set[str],
    ps_cache: dict[str, dict[str, str]],
) -> Optional[str]:
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
                tty_path = f"/dev/{tty_name}"
                if tty_path not in known_ttys:
                    continue
                if ("zellij" in cmd and "zellij" in server_cmd) or (
                    "tmux" in cmd and "tmux" in server_cmd
                ):
                    return tty_path
            break
        cur = ppid
        tty_name = ps_cache.get(cur, {}).get("tty")
        if tty_name and f"/dev/{tty_name}" in known_ttys:
            return f"/dev/{tty_name}"
    return None


def _match_session_to_pid(
    cwd: str,
    session_id: str,
    birthtime: float,
    ps_cache: dict[str, dict[str, str]],
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


def _fallback_jump(
    cwd: str,
    session_id: str,
    birthtime: float,
) -> tuple[bool, str]:
    if not cwd:
        return False, "No cwd"

    ps_cache = _build_ps_cache()
    exact_pid = _match_session_to_pid(cwd, session_id, birthtime, ps_cache)

    fallback_providers = [
        _PROVIDER_BY_KEY["kaku"],
        _PROVIDER_BY_KEY["tmux"],
        _PROVIDER_BY_KEY["wezterm"],
        _PROVIDER_BY_KEY["iterm2"],
        _PROVIDER_BY_KEY["terminal"],
    ]
    for provider in fallback_providers:
        if not provider.available():
            continue
        known_ttys = _provider_known_ttys(provider)
        if not known_ttys:
            continue

        resolved = None
        if exact_pid:
            resolved = _resolve_tty(exact_pid, known_ttys, ps_cache)
        else:
            target = cwd.rstrip("/")
            for pid, _, _ in _iter_cli_pids(ps_cache):
                if _pid_cwd(pid).rstrip("/") == target:
                    resolved = _resolve_tty(pid, known_ttys, ps_cache)
                    if resolved:
                        break

        if resolved and provider.activate({"tty": resolved}):
            return True, f"{provider.name} fallback"

    warp = _PROVIDER_BY_KEY["warp"]
    if warp.available():
        target = cwd.rstrip("/")
        for pid, _, _ in _iter_cli_pids(ps_cache):
            if _pid_cwd(pid).rstrip("/") == target and _is_warp_process(pid, ps_cache):
                if warp.activate({}):
                    return True, "Warp fallback"

    return False, "not found"


def jump_to_session(
    session_id: str,
    cwd: str = "",
    birthtime: float = 0,
) -> tuple[bool, str]:
    terminal_info = _load_terminal_info(session_id)
    no_hook_msg = "run agtop --install-hooks, then start a new Claude session"
    if terminal_info is not None:
        providers = _candidate_providers(terminal_info)
        activated: list[str] = []
        for provider in providers:
            if not provider.available():
                continue
            if not provider.activate(terminal_info):
                continue
            activated.append(provider.name)
            if provider.name != "tmux":
                return True, " + ".join(activated)
        if activated:
            return True, " + ".join(activated)

    # Keep the old PID→TTY fallback around for debugging/comparison, but
    # disable it for now so jump failures only reflect the hook-based path.
    # return _fallback_jump(cwd, session_id, birthtime)
    return False, no_hook_msg


def has_active_children(pid: str) -> bool:
    """Check if a process has actively running child processes.

    When waiting for permission: claude is blocked on stdin, no children.
    When tool is executing: there's a child process (shell, node, etc).
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", pid],
            text=True,
            timeout=2,
        )
        return bool(out.strip())
    except Exception:
        return False


def _build_ps_cache() -> dict[str, dict[str, str]]:
    cache: dict[str, dict[str, str]] = {}
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


def _iter_cli_pids(ps_cache: dict[str, dict[str, str]]):
    """Yield (pid, cmd, source) for all Claude and Codex CLI processes."""
    for pid, info in ps_cache.items():
        cmd = info["cmd"]
        first_arg = cmd.split()[0] if cmd else ""
        basename = first_arg.rsplit("/", 1)[-1] if first_arg else ""
        if basename == "claude":
            yield pid, cmd, "claude"
            continue
        if basename == "codex":
            yield pid, cmd, "codex"


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


def get_live_session_ids(sessions: list[dict]) -> dict[str, str]:
    """Return {session_id: pid} for sessions with a running claude process.

    Direction: for each process, find the best matching session (not vice versa).
    One process → at most one session_id marked alive.
    """
    ps_cache = _build_ps_cache()
    cli_pids = list(_iter_cli_pids(ps_cache))
    if not cli_pids:
        return {}

    all_pids = [pid for pid, _, _ in cli_pids]
    pid_cwd: dict[str, str] = {}
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-p", ",".join(all_pids), "-d", "cwd", "-Fn"],
            text=True,
            timeout=3,
        )
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = line[1:]
            elif line.startswith("n") and cur:
                pid_cwd[cur] = line[1:].rstrip("/")
    except Exception:
        return {}

    from collections import defaultdict

    sessions_by_cwd: dict[str, list[dict]] = defaultdict(list)
    sessions_by_id: dict[str, dict] = {}
    for session in sessions:
        sessions_by_cwd[session["cwd"].rstrip("/")].append(session)
        sessions_by_id[session["session_id"]] = session

    live_map: dict[str, str] = {}

    for pid, cmd, _ in cli_pids:
        cwd = pid_cwd.get(pid, "").rstrip("/")
        if not cwd:
            continue

        matched = False
        for session_id in sessions_by_id:
            if f"--resume {session_id}" in cmd:
                live_map[session_id] = pid
                matched = True
                break
        if matched:
            continue

        cwd_sessions = sessions_by_cwd.get(cwd, [])
        if not cwd_sessions:
            continue

        if len(cwd_sessions) == 1:
            live_map[cwd_sessions[0]["session_id"]] = pid
            continue

        pstart = _pid_start_time(pid)
        if pstart <= 0:
            continue

        best_sid = None
        best_diff = float("inf")
        for session in cwd_sessions:
            birthtime = session.get("_birthtime", 0)
            if birthtime > 0 and pstart <= birthtime:
                diff = birthtime - pstart
                if diff < best_diff:
                    best_diff = diff
                    best_sid = session["session_id"]
        if best_sid:
            live_map[best_sid] = pid

    return live_map
