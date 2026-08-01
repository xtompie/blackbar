"""Two-way map between real values and placeholders.

Keys are deterministic for the lifetime of the daemon: the same value always gets the
same placeholder. That is a hard requirement for Anthropic's prompt caching - the whole
conversation history is resent with every request and must come out byte-identical
after redaction.

The salt is randomised at daemon start, so a placeholder tells nothing to anyone who
happens to know a dictionary of likely values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter

PLACEHOLDER_RE = re.compile(r"\{\{sensitive:([a-z0-9_]{1,32}):([0-9a-f]{4,16})\}\}")

# Longest possible placeholder - the streaming layer needs it to know how much of a
# chunk's tail has to be held back.
PLACEHOLDER_MAX_LEN = len("{{sensitive:") + 32 + 1 + 16 + len("}}")


def format_placeholder(kind: str, key: str) -> str:
    return "{{sensitive:%s:%s}}" % (kind, key)


class Vault:
    def __init__(self) -> None:
        self._salt = os.urandom(16)
        self._lock = threading.RLock()
        self._placeholder_by_value: dict[tuple[str, str], str] = {}
        self._value_by_key: dict[str, str] = {}
        self._kind_counts: Counter[str] = Counter()

    def mask(self, kind: str, value: str) -> str:
        """Return a stable placeholder for a value. Idempotent."""
        cache_key = (kind, value)
        with self._lock:
            existing = self._placeholder_by_value.get(cache_key)
            if existing is not None:
                return existing

            key = self._derive_key(value)
            # Hash collision across different values: extend the key until it is free.
            length = 6
            while key[:length] in self._value_by_key and self._value_by_key[key[:length]] != value:
                length += 2
                if length > 16:
                    raise RuntimeError("could not derive a unique vault key")
            key = key[:length]

            placeholder = format_placeholder(kind, key)
            self._placeholder_by_value[cache_key] = placeholder
            self._value_by_key[key] = value
            self._kind_counts[kind] += 1
            return placeholder

    def resolve(self, key: str) -> str | None:
        with self._lock:
            return self._value_by_key.get(key)

    def restore(self, text: str, *, json_string: bool = False) -> tuple[str, int, int]:
        """Turn placeholders back into the original values.

        Pass json_string=True when the text is the inside of a JSON string (that is
        what input_json_delta carries during streaming). The value has to be escaped
        there, otherwise a name with a quote or a file body with a newline breaks the
        JSON the client is reassembling.

        Returns (text, restored_count, orphan_count).
        """
        restored = 0
        orphans = 0

        def _sub(match: re.Match[str]) -> str:
            nonlocal restored, orphans
            value = self.resolve(match.group(2))
            if value is None:
                orphans += 1
                return match.group(0)
            restored += 1
            return json.dumps(value)[1:-1] if json_string else value

        return PLACEHOLDER_RE.sub(_sub, text), restored, orphans

    def known_spans(self, text: str) -> list[tuple[int, int, str, str]]:
        """Finds values already in the vault: (start, end, kind, value).

        Once something has been recognised as a name, it stays a name - even in a
        sentence where the model would miss it. Costs a string search, not an inference.
        """
        with self._lock:
            known = sorted(self._placeholder_by_value, key=lambda item: -len(item[1]))
        out: list[tuple[int, int, str, str]] = []
        for kind, value in known:
            if len(value) < 3:
                continue
            start = text.find(value)
            while start != -1:
                out.append((start, start + len(value), kind, value))
                start = text.find(value, start + len(value))
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._kind_counts)

    def entries(self) -> list[tuple[str, str, str]]:
        """(kind, key, value) - used only by `blackbar vault show`."""
        with self._lock:
            out = []
            for (kind, value), placeholder in self._placeholder_by_value.items():
                match = PLACEHOLDER_RE.fullmatch(placeholder)
                assert match is not None
                out.append((kind, match.group(2), value))
            return sorted(out)

    def clear(self) -> None:
        with self._lock:
            self._placeholder_by_value.clear()
            self._value_by_key.clear()
            self._kind_counts.clear()
            self._salt = os.urandom(16)

    def _derive_key(self, value: str) -> str:
        return hashlib.blake2b(value.encode("utf-8"), key=self._salt, digest_size=8).hexdigest()
