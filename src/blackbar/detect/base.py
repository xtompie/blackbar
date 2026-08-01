"""Shared types for the detection layers.

Every layer returns offsets (start, end) in the text, never "a list of words".
Replacing by offset is exact; replacing by substring falls apart on inflected forms
("Janem Kowalskim" is not a substring of "Jan Kowalski").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Lower number wins when hits overlap.
LAYER_PRIORITY = {"rules": 0, "vault": 1, "regex": 2, "gliner": 3}


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    kind: str
    text: str
    layer: str

    @property
    def length(self) -> int:
        return self.end - self.start


class Detector(Protocol):
    layer: str

    def detect(self, text: str) -> list[Span]: ...


def merge_spans(spans: list[Span]) -> list[Span]:
    """Drop overlapping hits and return the rest sorted by position.

    The tie-breaking order has to be fully deterministic: the same conversation
    history is scanned on every request and must produce an identical result,
    otherwise prompt caching falls apart.
    """
    ordered = sorted(
        spans,
        key=lambda s: (-s.length, LAYER_PRIORITY.get(s.layer, 99), s.start, s.kind),
    )
    chosen: list[Span] = []
    for span in ordered:
        if any(span.start < other.end and other.start < span.end for other in chosen):
            continue
        chosen.append(span)
    return sorted(chosen, key=lambda s: s.start)


def apply_spans(text: str, spans: list[Span], replace) -> str:
    """Replace from the end, so the earlier offsets stay valid."""
    out = text
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        out = out[: span.start] + replace(span) + out[span.end :]
    return out
