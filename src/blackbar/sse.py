"""Restoring originals inside an SSE stream - the core of this project.

Two problems that off-the-shelf solutions get wrong (see LiteLLM #22821):

1. A placeholder can arrive split across two chunks ("{{sensi" | "tive:email:a1}}").
   Any tail that could still grow into a placeholder is held back until it resolves.
   The held tail can never grow unbounded, because "could still complete" has hard
   length limits on both the kind and the key.

2. Tool call arguments arrive as input_json_delta, i.e. fragments of JSON. There the
   value must be re-escaped, otherwise a file body with a newline breaks the JSON the
   client is reassembling from those fragments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .vault import PLACEHOLDER_MAX_LEN, Vault

_OPEN = "{{sensitive:"
_REST_RE = re.compile(r"[a-z0-9_]{0,32}(:[0-9a-f]{0,16}\}?)?")


def _may_complete(tail: str) -> bool:
    """Could `tail` be the start of a placeholder that later chunks finish?"""
    if len(tail) < len(_OPEN):
        return _OPEN.startswith(tail)
    if not tail.startswith(_OPEN):
        return False
    return _REST_RE.fullmatch(tail[len(_OPEN) :]) is not None


def pending_index(buf: str) -> int:
    """Index to hold emission from; len(buf) when everything can be sent."""
    window = max(0, len(buf) - PLACEHOLDER_MAX_LEN)
    index = buf.find("{", window)
    while index != -1:
        if _may_complete(buf[index:]):
            return index
        index = buf.find("{", index + 1)
    return len(buf)


class StreamRestorer:
    """Buffer for a single content block: takes fragments, returns sendable text."""

    def __init__(self, vault: Vault, *, json_string: bool = False) -> None:
        self._vault = vault
        self._json_string = json_string
        self._buf = ""
        self.restored = 0
        self.orphans = 0

    def feed(self, text: str) -> str:
        self._buf += text
        cut = pending_index(self._buf)
        emit, self._buf = self._buf[:cut], self._buf[cut:]
        return self._restore(emit)

    def flush(self) -> str:
        """End of block: whatever is left never completed, so send it as-is."""
        emit, self._buf = self._buf, ""
        return self._restore(emit)

    def _restore(self, text: str) -> str:
        if not text:
            return ""
        out, restored, orphans = self._vault.restore(text, json_string=self._json_string)
        self.restored += restored
        self.orphans += orphans
        return out


@dataclass
class StreamStats:
    restored: int = 0
    orphans: int = 0
    usage: dict = field(default_factory=dict)


class SSERewriter:
    """Rewrites Anthropic's SSE stream, restoring originals on the fly.

    Every content block gets its own buffer: text blocks and tool_use blocks follow
    different escaping rules and arrive interleaved.
    """

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._restorers: dict[int, StreamRestorer] = {}
        self._tail = ""
        self.stats = StreamStats()

    def feed(self, chunk: bytes) -> bytes:
        """Takes raw upstream bytes, returns bytes for the client."""
        self._tail += chunk.decode("utf-8", errors="replace")
        out: list[str] = []
        # The last fragment may be an unfinished event - keep it for the next round.
        parts = self._tail.split("\n\n")
        self._tail = parts.pop()
        for raw in parts:
            if raw.strip():
                out.append(self._rewrite_event(raw))
        return "".join(out).encode("utf-8")

    def close(self) -> bytes:
        """Close the stream: leftover buffers and any unfinished event."""
        out: list[str] = []
        for index in sorted(self._restorers):
            rest = self._restorers[index].flush()
            if rest:
                out.append(_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": rest},
                }))
        if self._tail.strip():
            out.append(self._tail if self._tail.endswith("\n\n") else self._tail + "\n\n")
            self._tail = ""
        self._collect(None)
        return "".join(out).encode("utf-8")

    def _rewrite_event(self, raw: str) -> str:
        event_name: str | None = None
        payload: dict | None = None
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    return raw + "\n\n"  # not ours - pass through untouched

        if payload is None:
            return raw + "\n\n"

        extra = self._transform(payload)
        self._collect(payload)
        return _event(event_name, payload) + "".join(extra)

    def _transform(self, payload: dict) -> list[str]:
        """Mutates the payload in place; returns any extra events to append."""
        kind = payload.get("type")

        if kind == "content_block_start":
            index = int(payload.get("index", 0))
            block = payload.get("content_block") or {}
            is_json = block.get("type") == "tool_use"
            self._restorers[index] = StreamRestorer(self._vault, json_string=is_json)
            if isinstance(block.get("text"), str) and block["text"]:
                block["text"] = self._restorers[index].feed(block["text"])
            return []

        if kind == "content_block_delta":
            index = int(payload.get("index", 0))
            delta = payload.get("delta") or {}
            restorer = self._restorers.get(index)
            if restorer is None:
                restorer = self._restorers[index] = StreamRestorer(
                    self._vault, json_string=delta.get("type") == "input_json_delta"
                )
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                delta["text"] = restorer.feed(delta["text"])
            elif delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                delta["partial_json"] = restorer.feed(delta["partial_json"])
            return []

        if kind == "content_block_stop":
            index = int(payload.get("index", 0))
            restorer = self._restorers.get(index)
            if restorer is None:
                return []
            rest = restorer.flush()
            if not rest:
                return []
            # The stop event has to come after the remaining content, so the leftover
            # goes out as an extra delta and stop stays where it was.
            delta_type = "input_json_delta" if restorer._json_string else "text_delta"
            field_name = "partial_json" if restorer._json_string else "text"
            return [_event("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": delta_type, field_name: rest},
            })]

        return []

    def _collect(self, payload: dict | None) -> None:
        if payload is not None:
            usage = payload.get("usage") or (payload.get("message") or {}).get("usage")
            if isinstance(usage, dict):
                self.stats.usage.update(usage)
            return
        self.stats.restored = sum(r.restored for r in self._restorers.values())
        self.stats.orphans = sum(r.orphans for r in self._restorers.values())


def _event(name: str | None, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prefix = f"event: {name}\n" if name else ""
    return f"{prefix}data: {data}\n\n"
