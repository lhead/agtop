import subprocess
import time
from datetime import datetime
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Footer,
    Header,
    ListItem,
    ListView,
    RichLog,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from .config import load_config
from .history import HistorySession, ProjectGroup, load_detail, scan_history
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
from .subagents import scan_subagents
from .widgets import AgentDetailModal, AgentFlow, Timeline


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


def _relative_time(mtime: float) -> str:
    age = time.time() - mtime
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        dt = datetime.fromtimestamp(mtime)
        return dt.strftime("%H:%M")
    if age < 86400 * 7:
        days = int(age / 86400)
        return f"{days}d ago"
    return datetime.fromtimestamp(mtime).strftime("%m/%d")


def _history_session_label(hs: HistorySession) -> str:
    time_str = _relative_time(hs.mtime)
    src = " [codex]" if hs.source == "codex" else ""
    title = hs.first_user_msg[:48] + "…" if len(hs.first_user_msg) > 48 else hs.first_user_msg
    title = title or hs.session_id[:16]
    return f"{time_str}  {title}{src}"


class AgtopApp(App):
    TITLE = "agtop"
    CSS_PATH = "monitor.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("j", "jump", "Jump"),
        Binding("a", "subscribe", "Subscribe"),
        Binding("r", "refresh_or_resume", "Refresh/Resume"),
        Binding("c", "copy_output", "Copy"),
        Binding("v", "view_agents", "Agents"),
        # "h" registered dynamically — see on_mount / _enter/_exit_history
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
        self._last_mtimes: dict[str, float] = {}
        self._cached_live_map: dict[str, str] = {}
        self._footer_state: tuple | None = None
        self._session_paths: dict[str, str] = {}  # session_id → path
        # History mode
        self._history_mode = False
        self._history_groups: list[ProjectGroup] = []
        self._sel_history: Optional[HistorySession] = None
        # Set by resume action; read by __main__ after run() returns
        self._resume_session_id: Optional[str] = None
        self._resume_cwd: Optional[str] = None
        self._resume_source: Optional[str] = None  # "claude" | "codex"
        # v0.3 Agent visualization mode
        self._agent_viz_mode = False
        self._agent_viz_session_id: Optional[str] = None
        self._agent_viz_session_path: Optional[str] = None
        self._agent_viz_selected: int = 0
        self._agent_viz_agents: list = []  # flat list of SubAgent

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield ListView(id="slist")
                yield Tree("Sessions", id="htree")
            with Vertical(id="right"):
                yield RichLog(id="output", markup=True, wrap=True, auto_scroll=True)
        # v0.3 Agent visualization containers (hidden by default)
        with VerticalScroll(id="agent-viz"):
            yield AgentFlow(id="agent-flow")
            yield Timeline(id="agent-timeline")
        yield Footer()

    def on_mount(self) -> None:
        # Hide tree and agent-viz initially
        self.query_one("#htree").styles.display = "none"
        self.query_one("#agent-viz").styles.display = "none"
        self._cur_interval = REFRESH_FAST
        self._timer = self.set_interval(REFRESH_FAST, self._do_refresh)
        self._sync_footer_bindings()
        self._do_refresh()

    # ── Live mode ────────────────────────────────────────────

    def _scan(self) -> list[dict]:
        candidates: list[dict] = []
        if CLAUDE_DIR.exists():
            for path in CLAUDE_DIR.glob("**/*.jsonl"):
                if "subagents" in path.parts:
                    continue
                info = self._parser.parse(path, source="claude")
                if info is not None:
                    candidates.append(info)
                    self._session_paths[info["session_id"]] = path
        if CODEX_DIR.exists():
            for path in CODEX_DIR.glob("**/*.jsonl"):
                info = self._parser.parse(path, source="codex")
                if info is not None:
                    candidates.append(info)
                    self._session_paths[info["session_id"]] = path

        cur_mtimes = {s["session_id"]: s["mtime"] for s in candidates}
        if cur_mtimes != self._last_mtimes:
            self._last_mtimes = cur_mtimes
            self._cached_live_map = get_live_session_ids(candidates)
        live_map = self._cached_live_map

        out: list[dict] = []
        for info in candidates:
            sid = info["session_id"]
            pid = live_map.get(sid)
            info["alive"] = pid is not None
            if not info["alive"] and info["age"] > SHOW_RECENT:
                continue
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
        if self._history_mode or self._agent_viz_mode:
            return
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
            self._sync_footer_bindings()
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
        if len(items) != len(self.sessions):
            self._rebuild_list()
            return
        for index, session in enumerate(self.sessions):
            items[index].query_one(Static).update(render_card(session))
            self._apply_item_classes(items[index], session)
        selected_index = self._selected_index()
        if self.sessions and listview.index != selected_index:
            listview.index = selected_index

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

    def _force_redraw(self) -> None:
        self._last_detail_key = None
        self._rebuild_list()
        self._show_detail(self._selected_index())
        self.query_one("#slist", ListView).refresh(repaint=True, layout=True)
        self.query_one("#output", RichLog).refresh(repaint=True, layout=True)
        self.refresh(repaint=True, layout=True)

    def _selected_live_session(self) -> Optional[dict]:
        if not self.sessions:
            return None
        index = self._selected_index()
        if 0 <= index < len(self.sessions):
            return self.sessions[index]
        return None

    def _set_binding(self, key: str, action: str, description: str) -> None:
        self._bindings.key_to_bindings[key] = [Binding(key, action, description)]

    def _sync_footer_bindings(self) -> None:
        session = self._selected_live_session()
        subscribed = bool(
            not self._history_mode
            and session
            and session["session_id"] in self._subscribed
        )
        state = (
            self._history_mode,
            bool(self.sessions),
            bool(session),
            bool(session and session.get("alive")),
            subscribed,
            self._sel_history is not None,
        )
        if state == self._footer_state:
            return

        self._footer_state = state
        self._set_binding("j", "jump", "Jump")
        self._set_binding("a", "subscribe", "Unsubscribe" if subscribed else "Subscribe")
        self._set_binding(
            "r",
            "refresh_or_resume",
            "Resume" if self._history_mode else "Refresh",
        )
        self._set_binding(
            "c",
            "copy_output",
            "Copy Resume" if self._history_mode else "Copy",
        )
        self._set_binding(
            "h",
            "toggle_history",
            "Live" if self._history_mode else "History",
        )
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "jump":
            session = self._selected_live_session()
            return bool(not self._history_mode and session and session.get("alive"))
        if action == "subscribe":
            return bool(not self._history_mode and self._selected_live_session())
        if action == "refresh_or_resume":
            return None if self._history_mode and self._sel_history is None else True
        if action == "copy_output":
            if self._history_mode:
                return None if self._sel_history is None else True
            return bool(self.sessions)
        return True

    # ── History mode ─────────────────────────────────────────

    def _enter_history(self) -> None:
        self._history_mode = True
        self.query_one("#slist").styles.display = "none"
        htree = self.query_one("#htree")
        htree.styles.display = "block"
        htree.focus()
        self._load_history_tree()
        log = self.query_one("#output", RichLog)
        log.clear()
        log.write(Text("Select a session to preview", style="dim italic"))
        self.sub_title = "History"
        self._sync_footer_bindings()

    def _exit_history(self) -> None:
        self._history_mode = False
        self._sel_history = None
        self.query_one("#htree").styles.display = "none"
        slist = self.query_one("#slist")
        slist.styles.display = "block"
        slist.focus()
        self.sub_title = ""
        self._last_detail_key = None
        self._sync_footer_bindings()
        self._do_refresh()

    def _load_history_tree(self) -> None:
        self._history_groups = scan_history(
            days=self._cfg.get("history_days", 7)
        )
        tree = self.query_one("#htree", Tree)
        tree.clear()
        tree.root.expand()

        for group in self._history_groups:
            count = len(group.sessions)
            group_label = f"{group.cwd}  ({count})"
            group_node = tree.root.add(group_label, data=group, expand=True)
            for hs in group.sessions:
                group_node.add_leaf(_history_session_label(hs), data=hs)

    def _show_history_detail(self, hs: HistorySession) -> None:
        self._sel_history = hs
        turns = load_detail(hs)
        log = self.query_one("#output", RichLog)
        log.clear()

        # Header
        from rich.markup import escape as rich_escape
        from rich.rule import Rule
        log.write(Text.from_markup(f"[green]DONE[/]  {rich_escape(hs.project)}"))
        log.write(Text(hs.cwd, style="dim"))

        if not turns:
            log.write(Text("(no conversation)", style="dim italic"))
            return

        for turn in turns:
            role = turn["role"]
            if role == "user":
                log.write(Rule(style="dim cyan"))
                text = _clip(turn["text"], 5)
                log.write(Text(f"› {text}", style="bold cyan"))
            elif role == "tool":
                log.write(Text(f"  ⚙ {turn['summary']}", style="yellow"))
            elif role == "assistant":
                text = _clip(turn["text"], 30)
                for line in text.split("\n"):
                    log.write(Text(f"  {line}"))

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if not self._history_mode:
            return
        node = event.node
        if isinstance(node.data, HistorySession):
            self._show_history_detail(node.data)
            self._sync_footer_bindings()

    # ── Notifications ────────────────────────────────────────

    def _notify(self, title: str, message: str) -> None:
        esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
        esc_msg = message.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{esc_msg}" with title "{esc_title}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        self.bell()

    def _check_notifications(self) -> None:
        for session in self.sessions:
            session_id = session["session_id"]

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

            if session_id in self._subscribed:
                if session["status"] == "working":
                    self._notified_done.discard(session_id)
                elif session_id not in self._notified_done:
                    self._notified_done.add(session_id)
                    self._notify("agtop", f"{session['project']}: Task Done")

    # ── Event handlers ───────────────────────────────────────

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
        self._sync_footer_bindings()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Suppress default Enter/click — jump is bound to 'j' only."""
        pass

    def on_key(self, event: events.Key) -> None:
        if not self._agent_viz_mode:
            return
        if event.key in ("up", "k"):
            self._agent_nav(-1)
            event.prevent_default()
            event.stop()
        elif event.key in ("down", "j"):
            self._agent_nav(1)
            event.prevent_default()
            event.stop()
        elif event.key == "enter":
            self._show_agent_detail()
            event.prevent_default()
            event.stop()

    def on_app_focus(self, event: events.AppFocus) -> None:
        self._force_redraw()

    def on_screen_resume(self, event: events.ScreenResume) -> None:
        self._force_redraw()

    # ── Actions ──────────────────────────────────────────────

    def _do_jump(self) -> None:
        idx = self._selected_index()
        if idx >= len(self.sessions):
            return
        session = self.sessions[idx]
        if not session.get("alive"):
            self.notify("Process exited", severity="warning")
            return
        ok, via = jump_to_session(
            session.get("session_id", ""),
            session.get("cwd", ""),
            session.get("_birthtime", 0),
        )
        if ok:
            self.notify(f"→ {session['project']} ({via})")
        else:
            self.notify(f"Jump failed: {via}", severity="warning")

    def action_jump(self) -> None:
        if self._history_mode:
            return
        self._do_jump()

    def action_toggle_history(self) -> None:
        if self._history_mode:
            self._exit_history()
        else:
            self._enter_history()

    def action_refresh_or_resume(self) -> None:
        if self._history_mode:
            self._action_resume()
        else:
            self._do_refresh()

    def _action_resume(self) -> None:
        hs = self._sel_history
        if hs is None:
            self.notify("Select a session first", severity="warning")
            return
        # Store session ID + cwd, then exit cleanly.
        # __main__.py runs claude after Textual restores the terminal.
        self._resume_session_id = hs.session_id
        self._resume_cwd = hs.actual_cwd or None
        self._resume_source = hs.source
        self.exit()

    def action_subscribe(self) -> None:
        if self._history_mode:
            return
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
            session = self.sessions[idx]
            if session["status"] != "working":
                self._notified_done.add(sid)
            self.notify("Subscribed 🔔")
        self._do_refresh()

    def action_copy_output(self) -> None:
        if self._history_mode:
            hs = self._sel_history
            if hs is None:
                self.notify("Select a session first", severity="warning")
                return
            cwd = hs.actual_cwd or ""
            if hs.source == "codex":
                resume_part = f"codex resume {hs.session_id}"
            else:
                resume_part = f"claude --resume {hs.session_id}"
            if cwd:
                cmd = f"cd {cwd} && {resume_part}"
            else:
                cmd = resume_part
            try:
                subprocess.run(["pbcopy"], input=cmd.encode(), check=True)
                self.notify(f"Copied: {cmd}")
            except Exception:
                self.notify("Copy failed", severity="error")
            return

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

    # ── v0.3 Agent Visualization ────────────────────────────────────

    def action_view_agents(self) -> None:
        """Enter agent visualization mode for the selected session."""
        from pathlib import Path as _Path

        if self._history_mode:
            if not self._sel_history:
                self.notify("Select a session first", severity="warning")
                return
            session_id = self._sel_history.session_id
            session_path = self._sel_history.path
        else:
            if not self.sessions:
                self.notify("No sessions", severity="warning")
                return
            session = self.sessions[self._selected_index()]
            session_id = session["session_id"]
            # Try cached path first, then search CLAUDE_DIR
            session_path = self._session_paths.get(session_id)
            if not session_path:
                found = list(CLAUDE_DIR.glob(f"**/{session_id}.jsonl"))
                session_path = found[0] if found else None
            if not session_path:
                self.notify("No sub-agents (session not found)", severity="warning")
                return

        # Check subagents dir exists
        session_path = _Path(session_path)
        subagents_dir = session_path.parent / session_path.stem / "subagents"
        if not subagents_dir.exists():
            self.notify("No sub-agents for this session", severity="warning")
            return

        self._enter_agent_viz(session_path.stem, str(session_path))

    def _enter_agent_viz(self, session_id: str, session_path: str) -> None:
        """Switch to agent visualization view."""
        self._agent_viz_mode = True
        self._agent_viz_session_id = session_id
        self._agent_viz_session_path = session_path
        self._agent_viz_selected = 0

        # Hide main view, show agent viz
        self.query_one("#main").styles.display = "none"
        viz = self.query_one("#agent-viz")
        viz.styles.display = "block"
        viz.focus()

        # Load and render agent data
        self._refresh_agent_viz()

        # Update footer
        self._set_agent_viz_bindings()
        self.refresh_bindings()

    def _exit_agent_viz(self) -> None:
        """Exit agent visualization mode."""
        self._agent_viz_mode = False
        self._agent_viz_session_id = None
        self._agent_viz_session_path = None
        self._agent_viz_agents = []

        # Show main view, hide agent viz
        self.query_one("#main").styles.display = "block"
        self.query_one("#agent-viz").styles.display = "none"

        # Restore v binding and re-sync all footer bindings
        self._restore_agent_viz_bindings()
        self._footer_state = None  # force re-render
        self._sync_footer_bindings()

        # Restore focus to whichever panel is active
        if self._history_mode:
            self.query_one("#htree").focus()
        else:
            self.query_one("#slist").focus()

    def _refresh_agent_viz(self) -> None:
        """Refresh agent visualization data."""
        if not self._agent_viz_session_path:
            return

        from pathlib import Path
        from .render import _format_duration

        session_path = Path(self._agent_viz_session_path)
        batches = scan_subagents(self._agent_viz_session_id, session_path)

        if not batches:
            return

        all_agents = [a for b in batches for a in b.agents]
        if not all_agents:
            return

        self._agent_viz_agents = all_agents

        # Clamp selection
        if self._agent_viz_selected >= len(all_agents):
            self._agent_viz_selected = len(all_agents) - 1

        t0 = min(a.start_ts for a in all_agents)
        t_end = max(a.mtime for a in all_agents)
        done = sum(1 for a in all_agents if a.status == "done")

        # Get session summary from parser
        info = self._parser.parse(session_path, source="claude")
        summary = info.get("task", "Session") if info else "Session"
        status = info.get("status", "done") if info else "done"
        duration = info.get("task_runtime", 0) if info else 0
        duration_str = _format_duration(duration)

        # Update widgets with data
        flow_widget = self.query_one("#agent-flow", AgentFlow)
        flow_widget.update_data(
            session_summary=summary,
            session_status=status,
            session_duration=duration_str,
            batches=batches,
            total_agents=len(all_agents),
            done_agents=done,
            selected=self._agent_viz_selected,
        )

        timeline_widget = self.query_one("#agent-timeline", Timeline)
        timeline_widget.update_data(session_start=t0, session_end=t_end, batches=batches)

    def _agent_nav(self, delta: int) -> None:
        """Navigate agent selection by delta (+1 next, -1 prev)."""
        if not self._agent_viz_agents:
            return
        n = len(self._agent_viz_agents)
        self._agent_viz_selected = (self._agent_viz_selected + delta) % n

        # Update flow widget highlight
        flow = self.query_one("#agent-flow", AgentFlow)
        flow.set_selected(self._agent_viz_selected)

        # Scroll to keep selected card visible
        self._scroll_to_agent(self._agent_viz_selected)

    def _scroll_to_agent(self, flat_idx: int) -> None:
        """Scroll agent-viz container so the selected card is visible."""
        if not self._agent_viz_agents:
            return
        agent = self._agent_viz_agents[flat_idx]
        # Header box ≈ 5 lines, each batch = 2 arrows + 5 card = 7 lines
        approx_y = max(5 + agent.spawn_index * 7 - 3, 0)
        container = self.query_one("#agent-viz", VerticalScroll)
        container.scroll_to(y=approx_y, animate=True, duration=0.2)

    def _show_agent_detail(self) -> None:
        """Open modal with selected agent's full details."""
        if not self._agent_viz_agents:
            return
        idx = self._agent_viz_selected
        if 0 <= idx < len(self._agent_viz_agents):
            agent = self._agent_viz_agents[idx]
            self.push_screen(AgentDetailModal(agent))

    def _set_agent_viz_bindings(self) -> None:
        """Set footer bindings for agent viz mode."""
        self._set_binding("v", "exit_agent_viz", "Back")

    def _restore_agent_viz_bindings(self) -> None:
        """Restore v binding after exiting agent viz."""
        self._set_binding("v", "view_agents", "Agents")

    def action_exit_agent_viz(self) -> None:
        """Exit agent visualization mode."""
        self._exit_agent_viz()

