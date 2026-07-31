"""Layer 2: GLiNER - entities no regex will catch (names, companies, addresses).

The model is loaded lazily and may be missing entirely: without GLiNER the proxy
degrades to rules + regex, it does not stop.

Inference is CPU-bound, so the caller MUST run it through an executor - otherwise it
blocks the event loop and stalls streaming for every other session.
"""

from __future__ import annotations

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
                if len(entity_text.strip()) < MIN_ENTITY_CHARS:
                    continue
                kind = self.labels.get(entity.get("label", ""), "entity")
                start = offset + int(entity["start"])
                end = offset + int(entity["end"])
                spans.append(Span(start, end, kind, text[start:end], self.layer))
        return spans


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
