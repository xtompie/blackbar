"""Request log: one line per exchange, appended to a plain text file.

Two lines per exchange, sharing an id: `phase=sent` the moment the redacted request goes
out, `phase=back` when the reply is done. A request that is still running - or that never
came back - is then visible as a `sent` with no `back`, which one line written at the end
could never show.

Appends of a single line are atomic, so several Claude Code windows writing at once
interleave lines but never corrupt one.

`~/.local/state/blackbar/requests.log` in logfmt, so `tail -f` on it works without
blackbar in the loop at all. `watch`, `last` and `stats` all read this one file - there
is no second source of truth and no database.

Hard rule: the line describes the event, never the content. Kinds, layers, counters,
timings and vault keys (which are hashes) - no values. An audit log must not become a
new place to leak from.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Rotated to .1 above this size, one generation kept.
MAX_BYTES = 5 * 1024 * 1024


@dataclass
class RequestEvent:
    session: str | None = None
    # the endpoint that was called - without it a refusal says nothing about what was
    # refused
    path: str = "-"
    model: str | None = None
    streaming: bool = False
    chars: int = 0
    masked: int = 0
    restored: int = 0
    orphans: int = 0
    detect_ms: float = 0.0
    total_ms: float = 0.0
    status: int | None = None
    usage: dict = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    layers: dict[str, int] = field(default_factory=dict)
    # [(kind, vault key)] - lets `watch --reveal` ask the vault what was replaced,
    # while the file itself never holds a value
    keys: list[tuple[str, str]] = field(default_factory=list)
    # set when the request never reached the API, and why
    refused: str | None = None
    ts: float = field(default_factory=time.time)
    id: int | None = None

    def sent_line(self) -> str:
        """Written as the request leaves, so long ones are visible while they run."""
        fields = [
            ("ts", f"{time.time():.3f}"),
            ("id", self.id),
            ("phase", "sent"),
            ("session", self.session or "-"),
            ("path", self.path or "-"),
            ("model", self.model or "-"),
            ("stream", int(self.streaming)),
            # chars is what detect_ms was spent on - without it a slow request looks
            # like a mystery instead of a big file
            ("chars", self.chars),
            ("masked", self.masked),
            ("kinds", _counts(self.kinds)),
            ("layers", _counts(self.layers)),
            ("keys", ",".join(f"{kind}:{key}" for kind, key in self.keys) or "-"),
            ("detect_ms", f"{self.detect_ms:.1f}"),
        ]
        if self.refused:
            fields.append(("refused", self.refused))
        return " ".join(f"{name}={value}" for name, value in fields)

    def back_line(self) -> str:
        fields = [
            ("ts", f"{time.time():.3f}"),
            ("id", self.id),
            ("phase", "back"),
            ("session", self.session or "-"),
            ("status", self.status if self.status is not None else "-"),
            ("restored", self.restored),
            ("orphans", self.orphans),
            ("total_ms", f"{self.total_ms:.1f}"),
            ("cache_read", self.usage.get("cache_read_input_tokens") or 0),
            ("input_tokens", self.usage.get("input_tokens") or 0),
        ]
        return " ".join(f"{name}={value}" for name, value in fields)


def _counts(counts: dict[str, int]) -> str:
    return ",".join(f"{key}:{value}" for key, value in sorted(counts.items())) or "-"


def parse_line(line: str) -> dict | None:
    """logfmt line -> dict. Returns None for anything that does not parse."""
    fields = {}
    for part in line.strip().split(" "):
        name, sep, value = part.partition("=")
        if sep:
            fields[name] = value
    if "ts" not in fields:
        return None
    return {
        "ts": float(fields["ts"]),
        "id": _int(fields.get("id")),
        "phase": fields.get("phase", "sent"),
        "session": fields.get("session", "-"),
        "path": fields.get("path", "-"),
        "model": fields.get("model", "-"),
        "streaming": fields.get("stream") == "1",
        "status": _int(fields.get("status")),
        "chars": _int(fields.get("chars")) or 0,
        "masked": _int(fields.get("masked")) or 0,
        "restored": _int(fields.get("restored")) or 0,
        "orphans": _int(fields.get("orphans")) or 0,
        "detect_ms": _float(fields.get("detect_ms")),
        "total_ms": _float(fields.get("total_ms")),
        "kinds": _pairs(fields.get("kinds")),
        "layers": _pairs(fields.get("layers")),
        "keys": [
            tuple(item.split(":", 1))
            for item in (fields.get("keys", "-") or "-").split(",")
            if item and item != "-" and ":" in item
        ],
        "refused": None if fields.get("refused", "-") == "-" else fields.get("refused"),
        "cache_read": _int(fields.get("cache_read")) or 0,
        "input_tokens": _int(fields.get("input_tokens")) or 0,
    }


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float(value: str | None) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _pairs(raw: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in (raw or "-").split(","):
        key, sep, value = item.partition(":")
        if sep and value.isdigit():
            out[key] = int(value)
    return out


class RequestLog:
    """Append-only writer. The daemon owns it; the CLI only reads the file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._next_id = _last_id(path) + 1

    def record_sent(self, event: RequestEvent) -> int:
        event.id = self._next_id
        self._next_id += 1
        self._append(event.sent_line())
        return event.id

    def record_back(self, event: RequestEvent) -> None:
        self._append(event.back_line())

    def _append(self, line: str) -> None:
        self._rotate_if_needed()
        # One write, one line: O_APPEND makes this atomic, so parallel windows
        # interleave lines instead of corrupting them.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < MAX_BYTES:
                return
        except OSError:
            return
        os.replace(self.path, self.path.with_suffix(".log.1"))


def read_lines(path: Path, limit: int | None = None, since: float | None = None) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            entry = parse_line(line)
            if entry and (since is None or entry["ts"] >= since):
                entries.append(entry)
    return entries[-limit:] if limit else entries


def exchanges(entries: list[dict]) -> list[dict]:
    """Pairs the two lines of an exchange by id. A `sent` with no `back` is a request
    that is still running, or one that never came back."""
    by_id: dict[int, dict] = {}
    order: list[int] = []
    for entry in entries:
        key = entry["id"]
        if key not in by_id:
            by_id[key] = dict(entry)
            order.append(key)
            # a refused request never left, so it is not waiting for anything
            by_id[key]["pending"] = entry["phase"] == "sent" and not entry.get("refused")
            continue
        merged = by_id[key]
        if entry["phase"] == "back":
            merged.update({
                "status": entry["status"], "restored": entry["restored"],
                "orphans": entry["orphans"], "total_ms": entry["total_ms"],
                "cache_read": entry["cache_read"], "input_tokens": entry["input_tokens"],
                "pending": False,
            })
        else:
            merged.update({k: v for k, v in entry.items() if k != "phase"})
            merged["pending"] = False
    return [by_id[key] for key in order]


def summary(entries: list[dict]) -> dict:
    entries = exchanges(entries)
    kinds: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    sessions: dict[str, dict] = {}

    for entry in entries:
        kinds.update(entry["kinds"])
        layers.update(entry["layers"])
        seen = sessions.setdefault(
            entry["session"], {"session": entry["session"], "requests": 0, "last_ts": 0.0}
        )
        seen["requests"] += 1
        seen["last_ts"] = max(seen["last_ts"], entry["ts"])

    count = len(entries)
    return {
        "totals": {
            "requests": count,
            "sessions": len(sessions),
            "masked": sum(e["masked"] for e in entries),
            "restored": sum(e["restored"] for e in entries),
            "orphans": sum(e["orphans"] for e in entries),
            "detect_ms": (sum(e["detect_ms"] for e in entries) / count) if count else 0.0,
            "total_ms": (sum(e["total_ms"] for e in entries) / count) if count else 0.0,
            "cache_read": sum(e["cache_read"] for e in entries),
            "input_tokens": sum(e["input_tokens"] for e in entries),
        },
        "kinds": dict(kinds.most_common()),
        "layers": dict(layers.most_common()),
        "sessions": sorted(sessions.values(), key=lambda s: -s["requests"]),
    }


def _last_id(path: Path) -> int:
    for entry in reversed(read_lines(path, limit=50)):
        if entry["id"]:
            return entry["id"]
    return 0
