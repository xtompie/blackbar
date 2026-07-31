"""Daemon lifecycle: background start, pidfile, health check.

Detaching from the terminal is complete (setsid + double fork + its own log), because
a daemon brought up by `blackbar claude` has to survive closing that window.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import httpx

from .config import Config


def read_pid(config: Config) -> int | None:
    try:
        pid = int(config.pid_path.read_text().strip())
    except (OSError, ValueError):
        return None
    if not _alive(pid):
        config.pid_path.unlink(missing_ok=True)
        return None
    return pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def health(config: Config, timeout: float = 1.0) -> dict | None:
    try:
        response = httpx.get(f"{config.base_url}/_admin/health", timeout=timeout)
        if response.status_code == 200:
            return response.json()
    except httpx.HTTPError:
        return None
    return None


def is_running(config: Config) -> bool:
    return health(config) is not None


def run_foreground(config: Config) -> None:
    import uvicorn

    from .server import create_app

    config.pid_path.parent.mkdir(parents=True, exist_ok=True)
    config.pid_path.write_text(str(os.getpid()))
    try:
        uvicorn.run(
            create_app(config),
            host=config.host,
            port=config.port,
            log_level="warning",
            access_log=False,
        )
    finally:
        config.pid_path.unlink(missing_ok=True)


def start_background(config: Config, wait: float = 20.0) -> bool:
    """Starts the daemon as an independent process. True once it answers health."""
    if is_running(config):
        return True

    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        log = os.open(config.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        os.execv(sys.executable, [sys.executable, "-m", "blackbar", "start", "--foreground"])
        os._exit(1)

    os.waitpid(pid, 0)
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running(config):
            return True
        time.sleep(0.2)
    return False


def stop(config: Config, timeout: float = 10.0) -> bool:
    pid = read_pid(config)
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            config.pid_path.unlink(missing_ok=True)
            return True
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    config.pid_path.unlink(missing_ok=True)
    return True


def admin_get(config: Config, path: str, timeout: float = 5.0, **params) -> dict:
    response = httpx.get(f"{config.base_url}/_admin/{path}", timeout=timeout, params=params)
    response.raise_for_status()
    return response.json()


def admin_post(config: Config, path: str, timeout: float = 5.0) -> dict:
    response = httpx.post(f"{config.base_url}/_admin/{path}", timeout=timeout)
    response.raise_for_status()
    return response.json()
