"""Layer 2: GLiNER - entities no regex will catch (names, companies, addresses).

The model is loaded lazily and may be missing entirely: without GLiNER the proxy
degrades to rules + regex, it does not stop.

Inference is CPU-bound, so the caller MUST run it through an executor - otherwise it
blocks the event loop and stalls streaming for every other session.
"""

from __future__ import annotations

import re
import threading

from .base import Span

# Zero-shot labels handed to the model, mapped onto our placeholder kinds.
DEFAULT_LABELS: dict[str, str] = {
    "person": "person",
    "organization": "company",
    "address": "address",
    "phone number": "phone",
    "email": "email",
}

# GLiNER has a limited window; long content (the output of `cat` on a file, say) is
# split with an overlap so an entity on a seam is not lost.
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200

# Entities shorter than this are almost always false positives (initials, code
# abbreviations).
MIN_ENTITY_CHARS = 3

# Words the model reads as a person because a prompt talks *about* people. Claude Code's
# system prompt alone says "user" dozens of times; masking those would rewrite the
# instructions and fill the vault with noise.
GENERIC = {
    "user", "users", "you", "your", "yours", "claude", "assistant", "agent", "human",
    "reader", "author", "owner", "admin", "administrator", "root", "system", "model",
    "team", "developer", "developers", "customer", "client", "someone", "anyone",
    "everyone", "people", "person", "name", "email", "address", "company",
    "organization", "anthropic", "openai", "github", "google", "microsoft", "apple",
    "code", "tool", "tools", "file", "files", "project", "repo", "repository",
}

# Punctuation that says "identifier", not "name": header-names, snake_case, paths, tags.
CODE_LIKE = ("_", "/", "\\", "{", "}", "<", ">", "$", "=", "(", ")", "[", "]")


class GlinerDetector:
    layer = "gliner"

    def __init__(self, model_name: str, threshold: float = 0.5, labels: dict[str, str] | None = None) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self.labels = labels or DEFAULT_LABELS
        self.error: str | None = None
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        """Load the model. Returns False and records the reason when it cannot."""
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            try:
                from gliner import GLiNER

                self._model = GLiNER.from_pretrained(self.model_name)
                self.error = None
                return True
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return False

    def detect(self, text: str) -> list[Span]:
        if self._model is None and not self.load():
            return []

        label_names = list(self.labels.keys())
        spans: list[Span] = []
        for offset, chunk in _chunks(text):
            try:
                found = self._model.predict_entities(chunk, label_names, threshold=self.threshold)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return spans
            for entity in found:
                entity_text = entity.get("text", "")
                kind = self.labels.get(entity.get("label", ""), "entity")
                if not _plausible(kind, entity_text):
                    continue
                start = offset + int(entity["start"])
                end = offset + int(entity["end"])
                spans.append(Span(start, end, kind, text[start:end], self.layer))
        return spans


def _plausible(kind: str, text: str) -> bool:
    """Rejects what the model calls an entity but a human would not."""
    value = text.strip()
    if len(value) < MIN_ENTITY_CHARS:
        return False
    if any(mark in value for mark in CODE_LIKE):
        return False

    words = [word for word in re.split(r"[\s,.]+", value) if word]
    if not words:
        return False
    # "user", "You", "Claude agent" - every word is a role, not a name.
    if all(word.lower().strip("'\"") in GENERIC for word in words):
        return False

    if kind in ("person", "company"):
        # Proper nouns start with a capital in every language this model covers.
        if not value[:1].isupper():
            return False
    if kind == "address":
        # Streets start with "ul." or a number as often as with a capital.
        if not (value[:1].isupper() or any(c.isdigit() for c in value)):
            return False
    if kind == "email" and "@" not in value:
        return False
    if kind == "phone" and sum(character.isdigit() for character in value) < 6:
        return False
    return True


def _chunks(text: str) -> list[tuple[int, str]]:
    if len(text) <= CHUNK_CHARS:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        out.append((start, text[start:end]))
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return out
