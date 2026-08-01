"""Request log: one line per exchange, appended to a plain text file.

One line covers both directions, so the fields say which one they mean: `sent_*` is what
went to the API, `back_*` is what came out of the reply. Without that, `restored=0` reads
like a failure, when it usually just means the model did not repeat any placeholder.

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

    def to_line(self) -> str:
        fields = [
            ("ts", f"{self.ts:.3f}"),
            ("id", self.id),
            ("session", self.session or "-"),
            ("model", self.model or "-"),
            ("stream", int(self.streaming)),
            ("status", self.status if self.status is not None else "-"),
            # sent_chars is what detect_ms was spent on - without it a slow request
            # looks like a mystery instead of a big file
            ("sent_chars", self.chars),
            ("sent_masked", self.masked),
            ("sent_kinds", _counts(self.kinds)),
            ("sent_layers", _counts(self.layers)),
            ("sent_keys", ",".join(f"{kind}:{key}" for kind, key in self.keys) or "-"),
            ("back_restored", self.restored),
            ("back_orphans", self.orphans),
            ("detect_ms", f"{self.detect_ms:.1f}"),
            ("total_ms", f"{self.total_ms:.1f}"),
            ("cache_read", self.usage.get("cache_read_input_tokens") or 0),
            ("input_tokens", self.usage.get("input_tokens") or 0),
        ]
        if self.refused:
            fields.append(("refused", self.refused))
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
        "session": fields.get("session", "-"),
        "model": fields.get("model", "-"),
        "streaming": fields.get("stream") == "1",
        "status": _int(fields.get("status")),
        "chars": _int(fields.get("sent_chars")) or 0,
        "masked": _int(fields.get("sent_masked")) or 0,
        "restored": _int(fields.get("back_restored")) or 0,
        "orphans": _int(fields.get("back_orphans")) or 0,
        "detect_ms": _float(fields.get("detect_ms")),
        "total_ms": _float(fields.get("total_ms")),
        "kinds": _pairs(fields.get("sent_kinds")),
        "layers": _pairs(fields.get("sent_layers")),
        "keys": [
            tuple(item.split(":", 1))
            for item in (fields.get("sent_keys", "-") or "-").split(",")
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

    def record(self, event: RequestEvent) -> int:
        event.id = self._next_id
        self._next_id += 1
        self._rotate_if_needed()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.to_line() + "\n")
        return event.id

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


def summary(entries: list[dict]) -> dict:
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
