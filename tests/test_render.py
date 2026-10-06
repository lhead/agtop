import pytest
from textual.markup import to_content

from agtop.render import render_card


def _session(**overrides):
    session = {
        "session_id": "s1",
        "project": "demo",
        "task": "",
        "last_text": "",
        "status": "working",
        "task_runtime": 0.6,
        "tool_summary": "",
    }
    session.update(overrides)
    return session


@pytest.mark.parametrize(
    "text",
    [
        "[Image: original 3840x2880, displayed at 2000x1500. Multiply coordinates by 1.92",
        "[/dim] closing tag in a prompt",
        "[bold]markup-looking text[/bold]",
        "unclosed [ bracket",
        "ends with a backslash \\",
    ],
)
def test_card_survives_bracket_text(text):
    for status in ("working", "active", "done", "waiting_permission", "waiting_question"):
        card = render_card(_session(status=status, task=text, last_text=text, project=text))
        to_content(card)  # raises MarkupError when escaping is incomplete
