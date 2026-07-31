"""Global hook-up: every `claude` goes through the proxy, no launcher to remember.

The entry goes into ~/.claude/settings.json (the `env` field) rather than ~/.zshrc,
because it then applies to Claude Code only and does not disturb other tools that read
ANTHROPIC_BASE_URL.

The warning the CLI has to show before enabling this: in this mode there is no launcher
to check whether the daemon is alive. A dead daemon means claude does not start at all.
That is why attach only makes sense together with the system service.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Config

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
ENV_KEY = "ANTHROPIC_BASE_URL"


def read_settings(path: Path = SETTINGS_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc


def current(path: Path = SETTINGS_PATH) -> str | None:
    return (read_settings(path).get("env") or {}).get(ENV_KEY)


def is_attached(config: Config, path: Path = SETTINGS_PATH) -> bool:
    value = current(path)
    return bool(value and value.startswith(config.base_url))


def preview(config: Config, path: Path = SETTINGS_PATH) -> str:
    settings = read_settings(path)
    env = dict(settings.get("env") or {})
    env[ENV_KEY] = config.provider_url("anthropic")
    settings["env"] = env
    return json.dumps(settings, indent=2, ensure_ascii=False)


def attach(config: Config, path: Path = SETTINGS_PATH) -> Path | None:
    """Writes the entry and returns the backup path (None when there was no file)."""
    settings = read_settings(path)
    backup = None
    if path.exists():
        backup = path.with_suffix(".json.blackbar-backup")
        shutil.copy2(path, backup)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    env = dict(settings.get("env") or {})
    env[ENV_KEY] = config.provider_url("anthropic")
    settings["env"] = env
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


def detach(config: Config, path: Path = SETTINGS_PATH) -> bool:
    settings = read_settings(path)
    env = settings.get("env") or {}
    value = env.get(ENV_KEY)
    if not value or not value.startswith(config.base_url):
        return False
    env.pop(ENV_KEY, None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True
