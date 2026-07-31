"""Configuration and paths (XDG convention)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP = "blackbar"

DEFAULT_CONFIG = """\
# blackbar configuration. Show with: blackbar config get

[proxy]
host = "127.0.0.1"
port = 8555

[launcher]
# true = `blackbar claude` brings the daemon up when it is not answering
autostart = true

[detection]
model = "urchade/gliner_multi_pii-v1"
threshold = 0.5
# layers in priority order; drop "gliner" to run on rules and regexes only
layers = ["rules", "regex", "gliner"]

[upstream]
url = "https://api.anthropic.com"
"""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / APP


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8555
    autostart: bool = True
    model: str = "urchade/gliner_multi_pii-v1"
    threshold: float = 0.5
    layers: list[str] = field(default_factory=lambda: ["rules", "regex", "gliner"])
    upstream: str = "https://api.anthropic.com"
    path: Path = field(default_factory=lambda: config_dir() / "config.toml")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def rules_path(self) -> Path:
        return config_dir() / "rules.yaml"

    @property
    def pid_path(self) -> Path:
        return state_dir() / "daemon.pid"

    @property
    def log_path(self) -> Path:
        return state_dir() / "daemon.log"

    @property
    def requests_path(self) -> Path:
        """One line per request; `tail -f` on it works without blackbar."""
        return state_dir() / "requests.log"

    @property
    def install_report_path(self) -> Path:
        return config_dir() / "install-report.md"


def load(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    config = Config(path=path)
    if not path.exists():
        return config

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    proxy = data.get("proxy") or {}
    config.host = str(proxy.get("host", config.host))
    config.port = int(proxy.get("port", config.port))

    launcher = data.get("launcher") or {}
    config.autostart = bool(launcher.get("autostart", config.autostart))

    detection = data.get("detection") or {}
    config.model = str(detection.get("model", config.model))
    config.threshold = float(detection.get("threshold", config.threshold))
    config.layers = [str(layer) for layer in detection.get("layers", config.layers)]

    upstream = data.get("upstream") or {}
    config.upstream = str(upstream.get("url", config.upstream)).rstrip("/")
    return config


def ensure_files() -> Config:
    """Creates the config and rules files on first run."""
    from .detect.rules import write_default_rules

    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)

    path = directory / "config.toml"
    if not path.exists():
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")

    config = load(path)
    write_default_rules(config.rules_path)
    return config


def set_value(key: str, value: str) -> str:
    """Minimal setter for `blackbar config set section.field value`."""
    path = config_dir() / "config.toml"
    if not path.exists():
        ensure_files()
    lines = path.read_text(encoding="utf-8").splitlines()

    section, _, field_name = key.rpartition(".")
    if not section:
        raise ValueError("key must look like section.field, e.g. proxy.port")

    formatted = value if _is_toml_literal(value) else f'"{value}"'
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == f"[{section}]"
            continue
        if in_section and stripped.split("=")[0].strip() == field_name:
            lines[index] = f"{field_name} = {formatted}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return f"{key} = {formatted}"

    lines.append("")
    lines.append(f"[{section}]")
    lines.append(f"{field_name} = {formatted}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"{key} = {formatted}"


def _is_toml_literal(value: str) -> bool:
    if value in ("true", "false"):
        return True
    if value.startswith("[") or value.startswith("{"):
        return True
    try:
        float(value)
        return True
    except ValueError:
        return False
