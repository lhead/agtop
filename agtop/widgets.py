"""Custom Textual widgets for sub-agent visualization (v0.3)."""

from __future__ import annotations

import time
from typing import Optional

from rich.console import Group, RenderableType
from rich.style import Style
from rich.text import Text

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import RichLog

from .render import _format_duration, _truncate, _display_width
from .subagents import SubAgent, SpawnBatch

# ── Palette ────────────────────────────────────────

_S_DONE = Style(color="green")
_S_DONE_DIM = Style(color="green", dim=True)
_S_WORKING = Style(color="red", bold=True)
_S_ACTIVE = Style(color="yellow")
_S_DIM = Style(dim=True)
_S_LABEL = Style(bold=True)
_S_BORDER_DONE = Style(color="green", dim=True)
_S_BORDER_WORKING = Style(color="red", bold=True)
_S_BORDER_ACTIVE = Style(color="yellow")
_S_TITLE = Style(bold=True)
_S_ARROW = Style(color="cyan", dim=True)
_S_SELECTED = Style(color="bright_cyan", bold=True)


def _style_for(status: str) -> Style:
    if status == "done":
        return _S_DONE
    if status == "working":
        return _S_WORKING
    return _S_ACTIVE


def _border_for(status: str) -> Style:
    if status == "done":
        return _S_BORDER_DONE
    if status == "working":
        return _S_BORDER_WORKING
    return _S_BORDER_ACTIVE


def _icon_for(status: str) -> str:
    if status == "done":
        return "✓"
    if status == "working":
        return "⚡"
    return "◇"


# ── Fractional bar rendering ──────────────────────

_FRAC = " ▏▎▍▌▋▊▉█"


def _bar_segment(width_f: float, style: Style) -> tuple[str, int]:
    """Render a fractional-width bar. Returns (string, display_cols)."""
    full = int(width_f)
    frac = width_f - full
    idx = int(frac * 8)
    s = "█" * full
    if idx > 0:
        s += _FRAC[idx]
        return s, full + 1
    return s, full


# ── Timeline (Gantt chart) ─────────────────────────


class Timeline(Widget):
    """Horizontal Gantt chart showing sub-agent lifetimes."""

    DEFAULT_CSS = """
    Timeline {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        session_start: float = 0,
        session_end: Optional[float] = None,
        batches: list[SpawnBatch] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._session_start = session_start
        self._session_end = session_end
        self._batches = batches or []

    def update_data(
        self,
        session_start: float,
        session_end: Optional[float],
        batches: list[SpawnBatch],
    ) -> None:
        self._session_start = session_start
        self._session_end = session_end
        self._batches = batches
        self.refresh()

    def render(self) -> RenderableType:
        return self._render_timeline()

    def _render_timeline(self) -> RenderableType:
        if not self._batches:
            return Text("  No sub-agents", style=_S_DIM)

        agents: list[SubAgent] = []
        for batch in self._batches:
            agents.extend(batch.agents)
        if not agents:
            return Text("  No sub-agents", style=_S_DIM)

        now = time.time()

        # ── Build compressed time segments ──
        intervals: list[tuple[float, float]] = []
        for a in agents:
            a_end = a.mtime if a.status == "done" else now
            intervals.append((a.start_ts, a_end))
        intervals.sort()

        # Merge overlapping
        merged: list[tuple[float, float]] = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= merged[-1][1] + 30:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))

        GAP_COLS = 1
        active_total = sum(e - s for s, e in merged)
        active_total = max(active_total, 1.0)

        LABEL_W = 7
        bar_w = max(self.size.width - LABEL_W - 12, 20)
        gap_total_cols = GAP_COLS * max(len(merged) - 1, 0)
        active_cols = bar_w - gap_total_cols
        active_cols = max(active_cols, 10)

        def _map_time(t: float) -> float:
            col = 0.0
            for i, (seg_s, seg_e) in enumerate(merged):
                seg_dur = seg_e - seg_s
                seg_cols = (seg_dur / active_total) * active_cols
                if t <= seg_s:
                    return col
                if t <= seg_e:
                    frac = (t - seg_s) / seg_dur if seg_dur > 0 else 0
                    return col + frac * seg_cols
                col += seg_cols
                if i < len(merged) - 1:
                    col += GAP_COLS
            return col

        lines: list[Text] = []

        # ── Main session bar ──
        main = Text()
        main.append(f"{'Main':<{LABEL_W}}", style=_S_LABEL)
        all_done = all(a.status == "done" for a in agents)
        main_style = _S_DONE if all_done else _S_WORKING

        main_cols = int(active_cols + gap_total_cols)
        bar_str, bar_cols = _bar_segment(max(main_cols, 1.0), main_style)
        main.append(bar_str, style=main_style)
        if not all_done:
            remaining = bar_w - bar_cols
            if remaining > 0:
                main.append("░" * remaining, style=_S_DIM)
        lines.append(main)

        # ── Agent bars ──
        for i, agent in enumerate(agents):
            line = Text()
            label = f"Ag.{i + 1}"
            line.append(f"{label:<{LABEL_W}}", style=_S_LABEL)

            a_end = agent.mtime if agent.status == "done" else now
            off_col = _map_time(agent.start_ts)
            end_col = _map_time(a_end)

            off_i = int(max(off_col, 0))
            off_i = min(off_i, bar_w - 1)
            len_f = max(end_col - off_col, 1.0)
            len_f = min(len_f, bar_w - off_i)

            style = _style_for(agent.status)
            line.append(" " * off_i)
            bar_str, bar_cols = _bar_segment(len_f, style)
            line.append(bar_str, style=style)

            marker = "✓" if agent.status == "done" else "▊"
            line.append(marker, style=style)

            dur = _format_duration(a_end - agent.start_ts)
            line.append(f" {dur}", style=_S_DIM)

            lines.append(line)

        # ── Time axis ──
        axis = Text()
        axis.append(" " * LABEL_W)
        axis_chars = ["─"] * bar_w
        col_pos = 0.0
        for i, (seg_s, seg_e) in enumerate(merged):
            seg_dur = seg_e - seg_s
            seg_cols = (seg_dur / active_total) * active_cols
            start_c = int(col_pos)
            if start_c < bar_w:
                axis_chars[start_c] = "┼"
            end_c = int(col_pos + seg_cols)
            if end_c < bar_w:
                axis_chars[min(end_c, bar_w - 1)] = "┼"
            col_pos += seg_cols
            if i < len(merged) - 1:
                gap_start = int(col_pos)
                if gap_start < bar_w:
                    axis_chars[gap_start] = "·"
                col_pos += GAP_COLS
        axis.append("".join(axis_chars), style=_S_DIM)
        lines.append(axis)

        # ── Segment duration labels ──
        labels = Text()
        labels.append(" " * LABEL_W)
        label_buf = [" "] * bar_w
        col_pos = 0.0
        last_end = -1
        for i, (seg_s, seg_e) in enumerate(merged):
            seg_dur = seg_e - seg_s
            seg_cols = (seg_dur / active_total) * active_cols
            start_c = int(col_pos)
            lbl = _format_duration(seg_dur)
            if start_c >= last_end + 1 and start_c + len(lbl) <= bar_w:
                for j, ch in enumerate(lbl):
                    label_buf[start_c + j] = ch
                last_end = start_c + len(lbl)
            col_pos += seg_cols
            if i < len(merged) - 1:
                col_pos += GAP_COLS
        labels.append("".join(label_buf), style=_S_DIM)
        lines.append(labels)

        return Group(*lines)


# ── AgentFlow (waterfall diagram) ──────────────────

# Card dimensions
_CARD_INNER = 28
_CARD_W = _CARD_INNER + 2  # +2 for borders
_CARD_GAP = 2


class AgentFlow(Widget):
    """Top-to-bottom waterfall diagram of sub-agent spawn batches."""

    DEFAULT_CSS = """
    AgentFlow {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        session_summary: str = "",
        session_status: str = "done",
        session_duration: str = "",
        batches: list[SpawnBatch] | None = None,
        total_agents: int = 0,
        done_agents: int = 0,
        selected: int = -1,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._summary = session_summary
        self._status = session_status
        self._duration = session_duration
        self._batches = batches or []
        self._total = total_agents
        self._done = done_agents
        self._selected = selected

    def update_data(
        self,
        session_summary: str,
        session_status: str,
        session_duration: str,
        batches: list[SpawnBatch],
        total_agents: int,
        done_agents: int,
        selected: int = -1,
    ) -> None:
        self._summary = session_summary
        self._status = session_status
        self._duration = session_duration
        self._batches = batches
        self._total = total_agents
        self._done = done_agents
        self._selected = selected
        self.refresh()

    def set_selected(self, index: int) -> None:
        self._selected = index
        self.refresh()

    def render(self) -> RenderableType:
        lines: list[Text] = []
        avail_w = max(self.size.width - 2, 40)

        # ── Main session header box ──
        lines.extend(self._render_main_box(avail_w))

        # ── Spawn batches ──
        flat_idx = 0
        for batch in self._batches:
            lines.extend(self._render_arrows(len(batch.agents), avail_w))
            lines.extend(self._render_batch(batch, avail_w, flat_idx))
            flat_idx += len(batch.agents)

        if not self._batches:
            lines.append(Text(""))
            lines.append(Text("  No sub-agents spawned", style=_S_DIM))

        return Group(*lines)

    def _render_main_box(self, avail_w: int) -> list[Text]:
        inner_w = min(avail_w - 2, 60)
        border_style = _border_for(self._status)
        status_icon = _icon_for(self._status)
        status_style = _style_for(self._status)

        summary = _truncate(self._summary, inner_w - 20) if self._summary else "Session"
        right = self._duration or ""

        lines: list[Text] = []

        # Top border
        top = Text()
        top.append("  ┌─ ", style=border_style)
        top.append(f"{status_icon} ", style=status_style)
        remaining_border = inner_w - 3
        top.append("─" * remaining_border, style=border_style)
        top.append("┐", style=border_style)
        lines.append(top)

        # Summary line
        mid1 = Text()
        mid1.append("  │ ", style=border_style)
        mid1.append(summary, style=_S_TITLE)
        pad = inner_w - _display_width(summary) - _display_width(right)
        if pad > 0:
            mid1.append(" " * pad)
        if right:
            mid1.append(right, style=status_style)
        mid1.append(" │", style=border_style)
        lines.append(mid1)

        # Progress line
        if self._total > 0:
            mid2 = Text()
            mid2.append("  │ ", style=border_style)
            filled = int(self._done / self._total * 20)
            mid2.append("█" * filled, style=_S_DONE)
            mid2.append("░" * (20 - filled), style=_S_DIM)
            count_str = f"  {self._done}/{self._total} done"
            mid2.append(count_str, style=_S_DIM)
            pad2 = inner_w - 20 - len(count_str)
            if pad2 > 0:
                mid2.append(" " * pad2)
            mid2.append(" │", style=border_style)
            lines.append(mid2)

        # Bottom border
        bot = Text()
        bot.append("  └", style=border_style)
        bot.append("─" * (inner_w + 1), style=border_style)
        bot.append("┘", style=border_style)
        lines.append(bot)

        return lines

    def _render_arrows(self, count: int, avail_w: int) -> list[Text]:
        card_total_w = _CARD_W * count + _CARD_GAP * max(count - 1, 0)
        offset = max((avail_w - card_total_w) // 2, 2)
        centers = []
        for i in range(count):
            center = offset + i * (_CARD_W + _CARD_GAP) + _CARD_W // 2
            centers.append(center)

        line1 = Text()
        buf = [" "] * avail_w
        for c in centers:
            if c < avail_w:
                buf[c] = "│"
        line1.append("".join(buf), style=_S_ARROW)

        line2 = Text()
        buf2 = [" "] * avail_w
        for c in centers:
            if c < avail_w:
                buf2[c] = "▼"
        line2.append("".join(buf2), style=_S_ARROW)

        return [line1, line2]

    def _render_batch(
        self, batch: SpawnBatch, avail_w: int, start_flat_idx: int = 0
    ) -> list[Text]:
        n = len(batch.agents)
        if n == 0:
            return []

        card_inner = _CARD_INNER
        card_w = card_inner + 2
        card_total_w = card_w * n + _CARD_GAP * max(n - 1, 0)

        if card_total_w > avail_w and n > 0:
            card_w = max((avail_w - _CARD_GAP * max(n - 1, 0)) // n, 10)
            card_inner = card_w - 2

        card_total_w = card_w * n + _CARD_GAP * max(n - 1, 0)
        offset = max((avail_w - card_total_w) // 2, 2)

        cards: list[list[Text]] = []
        for i, agent in enumerate(batch.agents):
            is_selected = (start_flat_idx + i) == self._selected

            status_style = _style_for(agent.status)
            border_style = _S_SELECTED if is_selected else _border_for(agent.status)

            # Border characters: double for selected, single otherwise
            if is_selected:
                tl, tr, bl, br = "╔", "╗", "╚", "╝"
                hb, vb = "═", "║"
            else:
                tl, tr, bl, br = "┌", "┐", "└", "┘"
                hb, vb = "─", "│"

            # Duration + status icon
            dur = _format_duration(agent.mtime - agent.start_ts)
            status_icon = _icon_for(agent.status)
            l3_content = f"{status_icon} {dur}"

            card_lines: list[Text] = []

            # Top border
            t = Text()
            t.append(tl, style=border_style)
            t.append(hb * card_inner, style=border_style)
            t.append(tr, style=border_style)
            card_lines.append(t)

            # Line 1: type icon + agent type
            c1 = Text()
            c1.append(vb, style=border_style)
            icon_str = f" {agent.icon} "
            icon_w = _display_width(icon_str)
            c1.append(icon_str, style=status_style)
            type_avail = card_inner - icon_w - 1  # -1 for space before right border
            type_truncated = _truncate(agent.agent_type, type_avail)
            c1.append(type_truncated, style=_S_TITLE if is_selected else Style())
            pad = type_avail - _display_width(type_truncated)
            if pad > 0:
                c1.append(" " * pad)
            c1.append(f" {vb}", style=border_style)
            card_lines.append(c1)

            # Line 2: prompt
            c2 = Text()
            c2.append(f"{vb} ", style=border_style)
            prompt_truncated = (
                _truncate(agent.prompt, card_inner - 2) if agent.prompt else ""
            )
            c2.append(prompt_truncated, style=_S_DIM)
            pad = card_inner - 1 - _display_width(prompt_truncated)
            if pad > 0:
                c2.append(" " * pad)
            c2.append(vb, style=border_style)
            card_lines.append(c2)

            # Line 3: status icon + duration
            c3 = Text()
            c3.append(f"{vb} ", style=border_style)
            c3.append(l3_content, style=status_style)
            pad = card_inner - 1 - _display_width(l3_content)
            if pad > 0:
                c3.append(" " * pad)
            c3.append(vb, style=border_style)
            card_lines.append(c3)

            # Bottom border
            b = Text()
            b.append(bl, style=border_style)
            b.append(hb * card_inner, style=border_style)
            b.append(br, style=border_style)
            card_lines.append(b)

            cards.append(card_lines)

        # Merge cards side-by-side
        num_lines = 5
        result: list[Text] = []
        gap = " " * _CARD_GAP

        for row in range(num_lines):
            merged = Text()
            merged.append(" " * offset)
            for ci, card in enumerate(cards):
                if ci > 0:
                    merged.append(gap)
                merged.append_text(card[row])
            result.append(merged)

        return result


# ── Agent Detail Modal ──────────────────────────────


class AgentDetailModal(ModalScreen[None]):
    """Modal overlay showing full sub-agent details."""

    DEFAULT_CSS = """
    AgentDetailModal {
        align: center middle;
    }

    #detail-box {
        width: 70;
        max-height: 80%;
        background: $surface;
        border: double $accent;
        padding: 1 2;
    }

    #detail-log {
        height: auto;
        max-height: 100%;
    }
    """

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, agent: SubAgent) -> None:
        super().__init__()
        self._agent = agent

    def compose(self):
        with VerticalScroll(id="detail-box"):
            yield RichLog(id="detail-log", wrap=True, auto_scroll=False)

    def on_mount(self) -> None:
        log = self.query_one("#detail-log", RichLog)
        a = self._agent

        # Header
        header = Text()
        header.append(f"{a.icon} ", style=_style_for(a.status))
        header.append(a.agent_type, style=Style(bold=True))
        log.write(header)
        log.write(Text(""))

        # Status + duration
        status_icon = _icon_for(a.status)
        status_label = {"done": "done", "working": "running", "active": "idle"}.get(
            a.status, a.status
        )
        dur = _format_duration(a.mtime - a.start_ts)

        info = Text()
        info.append(f"  {status_icon} ", style=_style_for(a.status))
        info.append(status_label, style=_style_for(a.status))
        info.append(f"    ⏱ {dur}", style=_S_DIM)
        log.write(info)
        log.write(Text(""))

        # Prompt
        log.write(Text("Prompt", style=Style(bold=True)))
        prompt = a.prompt or "(none)"
        for line in prompt.split("\n"):
            log.write(Text(f"  {line}"))
        log.write(Text(""))

        # Tool summary
        if a.tool_summary:
            log.write(Text("Last tool", style=Style(bold=True)))
            log.write(Text(f"  {a.tool_summary}", style=Style(color="yellow")))
            log.write(Text(""))

        # Path
        log.write(Text("Path", style=Style(dim=True, bold=True)))
        log.write(Text(f"  {a.path}", style=_S_DIM))
        log.write(Text(""))

        log.write(Text("Press Esc to close", style=_S_DIM))
