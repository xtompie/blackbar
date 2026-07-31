"""Event log and statistics.

Hard rule: metrics describe events, never content. The database holds kinds, layers,
counters and placeholder keys (which are hashes) - no values. An audit log must not
become a new place to leak from.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    session TEXT,
    model TEXT,
    streaming INTEGER NOT NULL DEFAULT 0,
    masked INTEGER NOT NULL DEFAULT 0,
    restored INTEGER NOT NULL DEFAULT 0,
    orphans INTEGER NOT NULL DEFAULT 0,
    detect_ms REAL NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0,
    status INTEGER,
    cache_read INTEGER,
    cache_write INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS detections (
    request_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    layer TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_detections_request ON detections(request_id);
"""


@dataclass
class RequestEvent:
    provider: str
    session: str | None = None
    model: str | None = None
    streaming: bool = False
    masked: int = 0
    restored: int = 0
    orphans: int = 0
    detect_ms: float = 0.0
    total_ms: float = 0.0
    status: int | None = None
    usage: dict = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    layers: dict[str, int] = field(default_factory=dict)
    # (kind, layer) -> count - stored so you can see which layer actually does the
    # work for which kind
    pairs: dict[tuple[str, str], int] = field(default_factory=dict)
    # [(kind, vault key)] for this request - published to live watchers only, never
    # written to the database
    keys: list[tuple[str, str]] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    id: int | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "provider": self.provider,
            "session": self.session,
            "model": self.model,
            "streaming": self.streaming,
            "masked": self.masked,
            "restored": self.restored,
            "orphans": self.orphans,
            "detect_ms": round(self.detect_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "status": self.status,
            "kinds": self.kinds,
            "layers": self.layers,
            "keys": [list(entry) for entry in self.keys],
            "cache_read": self.usage.get("cache_read_input_tokens"),
            "cache_write": self.usage.get("cache_creation_input_tokens"),
        }


class EventLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def record(self, event: RequestEvent) -> int:
        usage = event.usage or {}
        cursor = self._conn.execute(
            """INSERT INTO requests
               (ts, provider, session, model, streaming, masked, restored, orphans,
                detect_ms, total_ms, status, cache_read, cache_write, input_tokens, output_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.ts, event.provider, event.session, event.model,
                int(event.streaming), event.masked, event.restored, event.orphans,
                event.detect_ms, event.total_ms, event.status,
                usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
                usage.get("input_tokens"), usage.get("output_tokens"),
            ),
        )
        request_id = int(cursor.lastrowid)
        pairs = event.pairs or {(kind, "unknown"): count for kind, count in event.kinds.items()}
        for (kind, layer), count in pairs.items():
            self._conn.execute(
                "INSERT INTO detections (request_id, kind, layer, count) VALUES (?,?,?,?)",
                (request_id, kind, layer, count),
            )
        self._conn.commit()
        event.id = request_id
        return request_id

    def recent(self, limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["kinds"] = {
                d["kind"]: d["count"]
                for d in self._conn.execute(
                    "SELECT kind, SUM(count) AS count FROM detections WHERE request_id=? GROUP BY kind",
                    (row["id"],),
                ).fetchall()
            }
            out.append(entry)
        return out

    def summary(self, since: float | None = None) -> dict:
        where = "WHERE ts >= ?" if since else ""
        args = (since,) if since else ()
        totals = self._conn.execute(
            f"""SELECT COUNT(*) AS requests, SUM(masked) AS masked, SUM(restored) AS restored,
                       SUM(orphans) AS orphans, AVG(detect_ms) AS detect_ms, AVG(total_ms) AS total_ms,
                       SUM(cache_read) AS cache_read, SUM(input_tokens) AS input_tokens,
                       COUNT(DISTINCT session) AS sessions
                FROM requests {where}""",
            args,
        ).fetchone()
        kinds = self._conn.execute(
            f"""SELECT kind, SUM(count) AS count FROM detections
                {"WHERE request_id IN (SELECT id FROM requests WHERE ts >= ?)" if since else ""}
                GROUP BY kind ORDER BY count DESC""",
            args,
        ).fetchall()
        layers = self._conn.execute(
            f"""SELECT layer, SUM(count) AS count FROM detections
                {"WHERE request_id IN (SELECT id FROM requests WHERE ts >= ?)" if since else ""}
                GROUP BY layer ORDER BY count DESC""",
            args,
        ).fetchall()
        sessions = self._conn.execute(
            f"""SELECT session, COUNT(*) AS requests, MAX(ts) AS last_ts
                FROM requests {where} GROUP BY session ORDER BY requests DESC""",
            args,
        ).fetchall()
        return {
            "totals": dict(totals) if totals else {},
            "kinds": {row["kind"]: row["count"] for row in kinds},
            "layers": {row["layer"]: row["count"] for row in layers},
            "sessions": [dict(row) for row in sessions],
        }

    def close(self) -> None:
        self._conn.close()
