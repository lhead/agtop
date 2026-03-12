import subprocess
import time
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, ListItem, ListView, RichLog, Static

from .config import load_config
from .parser import (
    CLAUDE_DIR,
    CODEX_DIR,
    MAX_SESSIONS,
    REFRESH_FAST,
    REFRESH_SLOW,
    SHOW_RECENT,
    SessionParser,
)
from .providers import get_live_session_ids, has_active_children, jump_to_session
from .render import _clip, render_card, render_detail


def _detail_plain_text(session: dict) -> str:
    status_label = {
        "working": "RUNNING",
        "active": "IDLE",
        "done": "DONE",
        "waiting_question": "WAITING - Needs Input",
        "waiting_permission": "WAITING - Needs Permission",
    }
    parts = [f"[{status_label.get(session['status'], session['status'].upper())}]  {session['project']}", session["cwd"]]

    turns = session.get("turns", [])
    if not turns:
        parts.append("(no conversation yet)")
        return "\n".join(parts)

    for turn in turns:
        role = turn["role"]
        if role == "user":
            parts.append("")
            parts.append("━" * 60)
            parts.append(f"› {_clip(turn['text'], 5)}")
        elif role == "tool":
            parts.append(f"  ⚙ {turn['summary']}")
        elif role == "assistant":
            parts.append("")
            text = _clip(turn["text"], 30)
            for line in text.split("\n"):
                parts.append(f"  {line}")

    return "\n".join(parts)


class AgtopApp(App):
    TITLE = "agtop"
    CSS_PATH = "monitor.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("j", "jump", "Jump"),
        Binding("a", "subscribe", "Subscribe"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "copy_output", "Copy All"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cfg = load_config()
        self.sessions: list[dict] = []
        self.sel_id: Optional[str] = None
        self._parser = SessionParser()
        self._known_ids: list[str] = []
        self._refreshing = False
        self._last_detail_key = None
        self._notified_waiting: set[str] = set()
        self._subscribed: set[str] = set()
        self._notified_done: set[str] = set()
        self._highlight_time = 0.0
        # Light perf: skip process detection when no mtimes changed
        self._last_mtimes: dict[str, float] = {}
        self._cached_live_map: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield ListView(id="slist")
            with Vertical(id="right"):
                yield RichLog(id="output", markup=True, wrap=True, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._cur_interval = REFRESH_FAST
        self._timer = self.set_interval(REFRESH_FAST, self._do_refresh)
        self._do_refresh()

    def _scan(self) -> list[dict]:
        # Pass 1: parse all recent sessions (Claude + Codex)
        candidates: list[dict] = []
        if CLAUDE_DIR.exists():
            for path in CLAUDE_DIR.glob("**/*.jsonl"):
                if "subagents" in path.parts:
                    continue
                info = self._parser.parse(path, source="claude")
                if info is not None:
                    candidates.append(info)
        if CODEX_DIR.exists():
            for path in CODEX_DIR.glob("**/*.jsonl"):
                info = self._parser.parse(path, source="codex")
                if info is not None:
                    candidates.append(info)

        # Light perf: only rebuild live_map when mtimes changed
        cur_mtimes = {s["session_id"]: s["mtime"] for s in candidates}
        if cur_mtimes != self._last_mtimes:
            self._last_mtimes = cur_mtimes
            self._cached_live_map = get_live_session_ids(candidates)
        live_map = self._cached_live_map

        # Pass 2: filter + fix false waiting_permission
        out: list[dict] = []
        for info in candidates:
            sid = info["session_id"]
            pid = live_map.get(sid)
            info["alive"] = pid is not None
            if not info["alive"] and info["age"] > SHOW_RECENT:
                continue
            # Fix false waiting_permission: if process has active children,
            # the tool is executing (auto-approved), not waiting for permission
            if pid and info["status"] == "waiting_permission":
                if has_active_children(pid):
                    info["status"] = "working"
            info["subscribed"] = sid in self._subscribed
            out.append(info)

        out.sort(
            key=lambda s: (
                0 if s["status"].startswith("waiting") else
                1 if s["alive"] else
                2,
                -s["mtime"],
            )
        )
        return out[:MAX_SESSIONS]

    def _selected_index(self) -> int:
        if self.sel_id:
            for index, session in enumerate(self.sessions):
                if session["session_id"] == self.sel_id:
                    return index
        return 0

    def _do_refresh(self) -> None:
        self._refreshing = True
        try:
            new_sessions = self._scan()
            new_ids = [session["session_id"] for session in new_sessions]
            self.sessions = new_sessions

            if new_ids != self._known_ids:
                self._known_ids = new_ids
                self._rebuild_list()
            else:
                self._update_cards()

            self._show_detail(self._selected_index())
            self._check_notifications()
            self._adapt_interval()
        finally:
            self._refreshing = False

    def _adapt_interval(self) -> None:
        has_active = any(
            session["status"] in ("working", "waiting_permission", "waiting_question")
            or (session["session_id"] in self._subscribed and session["status"] != "done")
            for session in self.sessions
        )
        want = REFRESH_FAST if has_active else REFRESH_SLOW
        if getattr(self, "_cur_interval", None) != want:
            self._cur_interval = want
            self._timer.stop()
            self._timer = self.set_interval(want, self._do_refresh)

    def _apply_item_classes(self, item: ListItem, session: dict) -> None:
        if session["status"].startswith("waiting"):
            item.add_class("waiting")
            item.remove_class("working")
        elif session["status"] == "working":
            item.add_class("working")
            item.remove_class("waiting")
        else:
            item.remove_class("waiting")
            item.remove_class("working")

    def _rebuild_list(self) -> None:
        listview = self.query_one("#slist", ListView)
        listview.clear()
        for session in self.sessions:
            item = ListItem(Static(render_card(session), markup=True))
            self._apply_item_classes(item, session)
            listview.append(item)
        if self.sessions:
            listview.index = self._selected_index()

    def _update_cards(self) -> None:
        listview = self.query_one("#slist", ListView)
        items = list(listview.children)
        for index, session in enumerate(self.sessions):
            if index < len(items):
                items[index].query_one(Static).update(render_card(session))
                self._apply_item_classes(items[index], session)

    def _show_detail(self, index: int) -> None:
        if not self.sessions:
            cache_key = None
            parts = [Text("No active sessions found.", style="dim")]
        else:
            index = min(index, len(self.sessions) - 1)
            session = self.sessions[index]
            cache_key = (
                session["session_id"],
                session["status"],
                len(session.get("turns", [])),
                session.get("tool_summary", ""),
                session.get("last_text", ""),
            )
            if cache_key == self._last_detail_key:
                return
            parts = render_detail(session)

        self._last_detail_key = cache_key
        log = self.query_one("#output", RichLog)
        log.clear()
        for part in parts:
            log.write(part)

    def _notify(self, title: str, message: str) -> None:
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'display notification "{message}" with title "{title}"',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        self.bell()

    def _check_notifications(self) -> None:
        for session in self.sessions:
            session_id = session["session_id"]

            # Waiting notifications (existing)
            if session["status"].startswith("waiting"):
                if session_id not in self._notified_waiting:
                    self._notified_waiting.add(session_id)
                    label = (
                        "Needs Input"
                        if session["status"] == "waiting_question"
                        else "Needs Permission"
                    )
                    self._notify("agtop", f"{session['project']}: {label}")
            else:
                self._notified_waiting.discard(session_id)

            # Done notifications (subscribed sessions)
            # Trigger when session leaves "working" state (becomes active/done/exited)
            if session_id in self._subscribed:
                if session["status"] == "working":
                    self._notified_done.discard(session_id)
                elif session_id not in self._notified_done:
                    self._notified_done.add(session_id)
                    self._notify("agtop", f"{session['project']}: Task Done")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if self._refreshing:
            return
        index = event.list_view.index
        if index is None or index >= len(self.sessions):
            return
        new_id = self.sessions[index]["session_id"]
        if new_id == self.sel_id:
            return
        self.sel_id = new_id
        self._highlight_time = time.monotonic()
        self._show_detail(index)

    def _do_jump(self) -> None:
        """Jump to the terminal tab of the currently selected session."""
        idx = self._selected_index()
        if idx >= len(self.sessions):
            return
        session = self.sessions[idx]
        if not session.get("alive"):
            self.notify("Process exited", severity="warning")
            return
        ok, via = jump_to_session(
            session.get("cwd", ""),
            session.get("session_id", ""),
            session.get("_birthtime", 0),
        )
        if ok:
            self.notify(f"→ {session['project']} ({via})")
        else:
            self.notify("Tab not found", severity="warning")

    def action_jump(self) -> None:
        """Enter key → jump directly."""
        self._do_jump()

    def action_subscribe(self) -> None:
        idx = self._selected_index()
        if idx >= len(self.sessions):
            return
        sid = self.sessions[idx]["session_id"]
        if sid in self._subscribed:
            self._subscribed.discard(sid)
            self._notified_done.discard(sid)
            self.notify("Unsubscribed")
        else:
            self._subscribed.add(sid)
            self.notify("Subscribed 🔔")
        self._do_refresh()

    def action_refresh(self) -> None:
        self._do_refresh()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Mouse click on already-selected card → jump."""
        # Mouse click on unselected item fires highlighted+selected together (<50ms)
        if time.monotonic() - self._highlight_time < 0.05:
            return
        self._do_jump()

    def action_copy_output(self) -> None:
        if not self.sessions:
            self.notify("Nothing to copy", severity="warning")
            return

        session = self.sessions[self._selected_index()]
        detail = _detail_plain_text(session)
        try:
            subprocess.run(["pbcopy"], input=detail.encode(), check=True)
            self.notify("Copied to clipboard!")
        except Exception:
            self.notify("Copy failed", severity="error")
