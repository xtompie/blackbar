"""System service: launchd (macOS) or systemd --user (Linux).

Under a service the daemon comes back after a reboot and after a crash. That is a
precondition for `attach` mode - with the base URL hard-wired, claude will not start
at all unless the daemon is alive.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from .config import Config, state_dir

LABEL = "dev.blackbar.daemon"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def unit_path() -> Path:
    if is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "blackbar.service"


def _plist(config: Config) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>blackbar</string>
        <string>start</string>
        <string>--foreground</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{config.log_path}</string>
    <key>StandardErrorPath</key><string>{config.log_path}</string>
    <key>WorkingDirectory</key><string>{state_dir()}</string>
</dict>
</plist>
"""


def _systemd_unit(config: Config) -> str:
    return f"""[Unit]
Description=blackbar - local PII redaction proxy
After=network.target

[Service]
ExecStart={sys.executable} -m blackbar start --foreground
Restart=always
RestartSec=2
StandardOutput=append:{config.log_path}
StandardError=append:{config.log_path}

[Install]
WantedBy=default.target
"""


def install(config: Config) -> str:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.log_path.parent.mkdir(parents=True, exist_ok=True)

    if is_macos():
        path.write_text(_plist(config), encoding="utf-8")
        uid = _uid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed")
        return str(path)

    path.write_text(_systemd_unit(config), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "blackbar.service"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "systemctl enable failed")
    return str(path)


def uninstall(config: Config) -> bool:
    path = unit_path()
    if is_macos():
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"], capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "blackbar.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def installed() -> bool:
    return unit_path().exists()


def status() -> str:
    if not installed():
        return "none"
    if is_macos():
        result = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"], capture_output=True, text=True)
        if result.returncode != 0:
            return "installed, not loaded"
        for line in result.stdout.splitlines():
            if "state = " in line:
                return f"loaded ({line.split('=')[1].strip()})"
        return "loaded"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "blackbar.service"], capture_output=True, text=True
    )
    return result.stdout.strip() or "unknown"


def _uid() -> int:
    import os

    return os.getuid()
