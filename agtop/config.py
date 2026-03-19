"""Configuration loader for agtop."""

import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "agtop" / "config.toml"

DEFAULTS = {
    "show_recent_hours": 4,
    "max_sessions": 20,
    "refresh_fast": 1,
    "refresh_slow": 3,
    "notify": True,
    "notify_sound": True,
    "history_days": 7,
}


_KEY_MAP = {
    # toml section.key → internal key
    "fast": "refresh_fast",
    "slow": "refresh_slow",
    "enabled": "notify",
    "sound": "notify_sound",
}


def load_config() -> dict:
    """Load config from ~/.config/agtop/config.toml, merged with defaults."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as f:
                user = tomllib.load(f)
            for section in user.values():
                if isinstance(section, dict):
                    for k, v in section.items():
                        cfg[_KEY_MAP.get(k, k)] = v
        except Exception:
            pass
    return cfg
