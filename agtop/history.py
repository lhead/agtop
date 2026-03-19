"""History scanner: lightweight scan of all past Claude/Codex sessions."""

import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .parser import (
    CLAUDE_DIR,
    CODEX_DIR,
    _parse_claude_lines,
    _parse_codex_lines,
    _read_head_tail,
    _tool_summary,
)

HISTORY_DAYS = 7
_LRU_MAX = 64


@dataclass
class HistorySession:
    session_id: str
    path: Path
    source: str          # "claude" | "codex"
    project: str
    cwd: str             # inferred from directory name (for display)
    actual_cwd: str      # from JSONL content (for subprocess cwd)
    mtime: float
    birthtime: float
    first_user_msg: str  # for display as title
    stop_ts: Optional[float]  # from hook events file if available


@dataclass
class ProjectGroup:
    cwd: str
    project: str
    sessions: list[HistorySession] = field(default_factory=list)

    @property
    def latest_mtime(self) -> float:
        return max((s.mtime for s in self.sessions), default=0.0)


class _LRUCache:
    def __init__(self, max_size: int = _LRU_MAX) -> None:
        self._data: OrderedDict = OrderedDict()
        self._max = max_size

    def get(self, key: str):
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self._max:
            self._data.popitem(last=False)


# Module-level cache: key = (path_str, mtime, size) → full turns list
_detail_cache = _LRUCache(_LRU_MAX)


def _cwd_from_dir_name(name: str) -> str:
    """Best-effort: convert Claude dir name back to a path.

    Claude encodes paths like ``/Users/luhao/claude-monitor`` as
    ``-Users-luhao-claude-monitor``.  The encoding is ambiguous because
    ``-`` is both the separator and a legal character in directory names.

    Strategy: try every possible split, prefer the longest real path.
    Falls back to the naive all-replace if nothing matches on disk.
    """
    raw = name.lstrip("-")
    home = str(Path.home())
    # home username without leading /  e.g. "Users/luhao"
    home_prefix = home[1:]  # "Users/luhao"

    # Collect candidate paths by replacing subsets of `-` with `/`
    parts = raw.split("-")
    best: str | None = None

    def _resolve(segments: list[str]) -> str:
        """Join segments with `/` and normalise to ~/… display form."""
        p = "/" + "/".join(segments)
        if p.startswith(home):
            return "~" + p[len(home):]
        return p

    def _search(idx: int, current: list[str]) -> None:
        nonlocal best
        if idx == len(parts):
            candidate = _resolve(current)
            # Check if the real path exists on disk
            real = candidate.replace("~", home)
            if Path(real).is_dir():
                if best is None or len(real) > len(best.replace("~", home)):
                    best = candidate
            return
        # Option 1: this part starts a new path segment
        _search(idx + 1, current + [parts[idx]])
        # Option 2: this part joins the previous segment with `-`
        if current:
            _search(idx + 1, current[:-1] + [current[-1] + "-" + parts[idx]])

    if len(parts) <= 12:
        _search(0, [])

    if best is not None:
        return best

    # Fallback: naive replace (ambiguous, but better than nothing)
    naive = raw.replace("-", "/")
    if naive.startswith(home_prefix):
        return "~" + naive[len(home_prefix):]
    return "/" + naive


def _parse_head(path: Path, source: str) -> tuple[str, str]:
    """Return (first_user_message, actual_cwd) from file head."""
    try:
        head_lines, _ = _read_head_tail(path, head_n=30, tail_bytes=0)
        if source == "codex":
            parsed = _parse_codex_lines(head_lines)
        else:
            parsed = _parse_claude_lines(head_lines)
        msg = parsed.get("user_text", "").replace("\n", " ").strip()
        cwd = parsed.get("cwd", "").strip()
        return msg, cwd
    except Exception:
        return "", ""


def _stop_ts_from_event(session_id: str) -> Optional[float]:
    from .hooks import EVENTS_DIR
    path = EVENTS_DIR / f"{session_id}.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        val = data.get("stop_ts")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return None


def scan_history(days: int = HISTORY_DAYS) -> list[ProjectGroup]:
    """Scan all sessions older than live threshold, grouped by project dir."""
    cutoff = time.time() - days * 86400
    groups: dict[str, ProjectGroup] = {}

    def _process(path: Path, source: str) -> None:
        try:
            st = path.stat()
            if st.st_mtime < cutoff:
                return
        except OSError:
            return

        stem = path.stem
        if source == "codex":
            # rollout-2026-03-12T16-22-58-019ce124-4b33-7371-a8a1-d84b76555021
            # extract trailing UUID (8-4-4-4-12 hex)
            m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', stem)
            session_id = m.group(1) if m else stem
        else:
            session_id = stem
        mtime = st.st_mtime
        birthtime = getattr(st, "st_birthtime", mtime)

        first_msg, actual_cwd = _parse_head(path, source)
        stop_ts = _stop_ts_from_event(session_id)

        home = str(Path.home())

        # Determine display_cwd and project name.
        # Priority: actual_cwd from JSONL > inferred from directory name.
        if actual_cwd:
            display_cwd = actual_cwd.replace(home, "~")
            project = Path(actual_cwd).name or path.parent.name
        elif source == "codex":
            # Codex dirs are date-based (2026/03/12), useless for path info
            display_cwd = "~"
            project = "~"
        else:
            dir_name = path.parent.name
            display_cwd = _cwd_from_dir_name(dir_name)
            project = Path(display_cwd.replace("~", home)).name or dir_name

        hs = HistorySession(
            session_id=session_id,
            path=path,
            source=source,
            project=project,
            cwd=display_cwd,
            actual_cwd=actual_cwd or home,
            mtime=mtime,
            birthtime=birthtime,
            first_user_msg=first_msg,
            stop_ts=stop_ts,
        )

        # Group by display_cwd so actual_cwd takes priority over inference
        key = display_cwd
        if key not in groups:
            groups[key] = ProjectGroup(cwd=display_cwd, project=project)
        groups[key].sessions.append(hs)

    if CLAUDE_DIR.exists():
        for jsonl in CLAUDE_DIR.glob("**/*.jsonl"):
            if "subagents" in jsonl.parts:
                continue
            _process(jsonl, "claude")

    if CODEX_DIR.exists():
        for jsonl in CODEX_DIR.glob("**/*.jsonl"):
            _process(jsonl, "codex")

    # Sort sessions within each group newest first
    for g in groups.values():
        g.sessions.sort(key=lambda s: s.mtime, reverse=True)

    # Sort groups by most recent session
    return sorted(groups.values(), key=lambda g: g.latest_mtime, reverse=True)


def load_detail(hs: HistorySession) -> list[dict]:
    """Load full turns for a HistorySession, with LRU caching."""
    try:
        st = hs.path.stat()
        cache_key = f"{hs.path}:{st.st_mtime}:{st.st_size}"
    except OSError:
        return []

    cached = _detail_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        head_lines, tail_lines = _read_head_tail(hs.path)
        all_lines = head_lines + tail_lines
        if hs.source == "codex":
            parsed = _parse_codex_lines(all_lines)
        else:
            parsed = _parse_claude_lines(all_lines)
        turns = parsed.get("turns", [])
    except Exception:
        turns = []

    _detail_cache.set(cache_key, turns)
    return turns
