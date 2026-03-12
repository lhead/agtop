import unicodedata
from typing import Optional

from rich.markup import escape as rich_escape
from rich.rule import Rule
from rich.text import Text


CARD_WIDTH = 42
W = CARD_WIDTH
_MAX_ASSISTANT_LINES = 30
_MAX_USER_LINES = 5


def _format_duration(sec: Optional[float]) -> str:
    if sec is None:
        return ""
    seconds = int(sec)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


def _format_age(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)}s ago"
    if sec < 3600:
        return f"{int(sec / 60)}m ago"
    return f"{int(sec / 3600)}h ago"


def _char_width(ch: str) -> int:
    width = unicodedata.east_asian_width(ch)
    return 2 if width in ("W", "F") else 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _truncate(text: str, max_width: int) -> str:
    text = text.replace("\n", " ").strip()
    width = 0
    for index, char in enumerate(text):
        char_width = _char_width(char)
        if width + char_width > max_width - 1:
            return text[:index] + "…"
        width += char_width
    return text


def _center(text: str, width: int) -> str:
    text_width = _display_width(text)
    if text_width >= width:
        return text
    padding = (width - text_width) // 2
    return " " * padding + text


def _sub_tag(session: dict) -> str:
    """Return a bell icon if subscribed."""
    return " 🔔" if session.get("subscribed") else ""


def _source_tag(session: dict) -> str:
    """Return a dim source tag for non-Claude CLIs."""
    src = session.get("source", "claude")
    if src == "codex":
        return " [dim]codex[/dim]"
    return ""


def render_card(session: dict) -> str:
    status = session["status"]
    content_width = W - 5
    tag = _source_tag(session)
    sub = _sub_tag(session)

    if status == "waiting_question":
        project = rich_escape(_truncate(session["project"], content_width))
        line1 = f"🟠 [bold]{project}[/bold]{tag}{sub}"
        line2 = _center("❓ Needs Input", content_width)
        line3 = ""
        return f"{line1}\n{line2}\n{line3}"

    if status == "waiting_permission":
        project = rich_escape(_truncate(session["project"], content_width))
        tool = session["tool_summary"].split()[0] if session["tool_summary"] else ""
        line1 = f"🟠 [bold]{project}[/bold]{tag}{sub}"
        label = f"⏳ Needs Permission  {tool}" if tool else "⏳ Needs Permission"
        line2 = _center(label, content_width)
        line3 = ""
        return f"{line1}\n{line2}\n{line3}"

    if status == "working":
        duration = _format_duration(session["task_runtime"])
        tool = session["tool_summary"].split()[0] if session["tool_summary"] else ""
        suffix = f"[red]{duration}[/red]"
        if tool:
            suffix += f" [yellow]{tool}[/yellow]"
        project_max = content_width - len(duration) - (len(tool) + 1 if tool else 0) - 2
        project = rich_escape(_truncate(session["project"], project_max))
        line1 = f"🔴 [bold]{project}[/bold]{tag}{sub}  {suffix}"
    elif status == "active":
        project = rich_escape(_truncate(session["project"], content_width))
        line1 = f"🟡 [bold]{project}[/bold]{tag}{sub}"
    else:
        project = rich_escape(_truncate(session["project"], content_width))
        line1 = f"🟢 [bold]{project}[/bold]{tag}{sub}"

    task = rich_escape(_truncate(session["task"], content_width)) if session["task"] else ""
    line2 = f"   [cyan]›[/cyan] {task}"

    output = (
        rich_escape(_truncate(session["last_text"], content_width))
        if session["last_text"]
        else ""
    )
    line3 = f"   [dim]‹[/dim] {output}"

    return f"{line1}\n{line2}\n{line3}"


def _clip(text: str, max_lines: int) -> str:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... (+{len(lines) - max_lines} lines)"


def render_detail(session: dict) -> list:
    parts = []
    status_map = {
        "working": "[bold red]RUNNING[/]",
        "active": "[yellow]IDLE[/]",
        "done": "[green]DONE[/]",
        "waiting_question": "[bold yellow]⚠ WAITING — Needs Input[/]",
        "waiting_permission": "[bold yellow]⚠ WAITING — Needs Permission[/]",
    }
    status = status_map.get(session["status"], session["status"].upper())
    parts.append(Text.from_markup(f"{status}  {rich_escape(session['project'])}"))
    parts.append(Text(session["cwd"], style="dim"))

    turns = session.get("turns", [])
    if not turns:
        parts.append(Text("(no conversation yet)", style="dim italic"))
        return parts

    for turn in turns:
        role = turn["role"]
        if role == "user":
            parts.append(Rule(style="dim cyan"))
            text = _clip(turn["text"], _MAX_USER_LINES)
            parts.append(Text(f"› {text}", style="bold cyan"))
        elif role == "tool":
            parts.append(Text(f"  ⚙ {turn['summary']}", style="yellow"))
        elif role == "assistant":
            text = _clip(turn["text"], _MAX_ASSISTANT_LINES)
            for line in text.split("\n"):
                parts.append(Text(f"  {line}"))
    return parts
