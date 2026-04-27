import json
from pathlib import Path

from agtop import hooks


def _read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _commands_for(settings: dict, hook_name: str) -> list[str]:
    commands: list[str] = []
    for group in settings["hooks"][hook_name]:
        for hook in group.get("hooks", []):
            if isinstance(hook, dict) and hook.get("type", "command") == "command":
                commands.append(str(hook.get("command", "")))
    return commands


def test_install_claude_hooks_uses_absolute_launcher(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    launcher = tmp_path / "bin" / "agtop"

    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hooks, "_resolve_install_launcher", lambda: str(launcher))

    path, changed = hooks.install_claude_hooks()

    settings = _read_settings(settings_path)
    assert path == settings_path
    assert changed is True
    assert _commands_for(settings, "UserPromptSubmit") == [f"{launcher} --hook prompt"]
    assert _commands_for(settings, "Stop") == [f"{launcher} --hook stop"]
    assert _commands_for(settings, "Notification") == [
        f"{launcher} --hook notification",
        f"{launcher} --hook notification",
    ]


def test_install_claude_hooks_upgrades_legacy_agtop_commands(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": "agtop --hook prompt"},
                            ],
                        },
                    ],
                    "Notification": [
                        {
                            "matcher": "permission_prompt",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "agtop --hook notification",
                                },
                            ],
                        },
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "agtop --hook stop"},
                            ],
                        },
                    ],
                },
                "theme": "dark",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "bin" / "agtop"

    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hooks, "_resolve_install_launcher", lambda: str(launcher))

    _, changed = hooks.install_claude_hooks()
    settings = _read_settings(settings_path)

    assert changed is True
    assert settings["theme"] == "dark"
    prompt_commands = _commands_for(settings, "UserPromptSubmit")
    stop_commands = _commands_for(settings, "Stop")
    notification_commands = _commands_for(settings, "Notification")

    assert prompt_commands == [f"{launcher} --hook prompt"]
    assert stop_commands == [f"{launcher} --hook stop"]
    assert notification_commands == [
        f"{launcher} --hook notification",
        f"{launcher} --hook notification",
    ]
    assert "agtop --hook prompt" not in prompt_commands
    assert "agtop --hook stop" not in stop_commands
    assert "agtop --hook notification" not in notification_commands
    assert [group.get("matcher") for group in settings["hooks"]["Notification"]] == [
        "permission_prompt",
        "idle_prompt|elicitation_dialog",
    ]

    _, changed_again = hooks.install_claude_hooks()
    settings_again = _read_settings(settings_path)

    assert changed_again is False
    assert settings_again == settings
