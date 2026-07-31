"""Detection layers combined into a single pass over the text."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter, OrderedDict

from ..vault import Vault
from .base import Span, apply_spans, merge_spans
from .gliner_layer import GlinerDetector
from .regexes import RegexDetector
from .rules import RulesDetector

__all__ = ["Redactor", "Span", "RegexDetector", "RulesDetector", "GlinerDetector", "merge_spans"]

# The conversation history is resent with every request and is identical between
# turns. Without this cache, GLiNER would chew through all of it again every time.
CACHE_SIZE = 1024


class Redactor:
    def __init__(
        self,
        vault: Vault,
        rules: RulesDetector,
        regex: RegexDetector | None = None,
        gliner: GlinerDetector | None = None,
    ) -> None:
        self.vault = vault
        self.rules = rules
        self.regex = regex if regex is not None else RegexDetector()
        self.gliner = gliner
        self._cache: OrderedDict[str, tuple[str, tuple[tuple[str, str], ...]]] = OrderedDict()

    def detect_sync(self, text: str) -> list[Span]:
        spans = self.rules.detect(text) + self.regex.detect(text)
        if self.gliner is not None:
            spans += self.gliner.detect(text)
        return merge_spans(spans)

    async def redact(self, text: str) -> tuple[str, Counter[str], Counter[str]]:
        """Returns (text with placeholders, hits per kind, hits per layer)."""
        if not text.strip():
            return text, Counter(), Counter()

        cache_key = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            masked, hits = cached
            return masked, Counter(k for k, _ in hits), Counter(l for _, l in hits)

        if self.gliner is not None and self.gliner.loaded:
            # GLiNER is CPU-bound: it must not run on the event loop.
            spans = await asyncio.get_running_loop().run_in_executor(None, self.detect_sync, text)
        else:
            spans = merge_spans(self.rules.detect(text) + self.regex.detect(text))

        masked = apply_spans(text, spans, lambda s: self.vault.mask(s.kind, s.text))
        hits = tuple((s.kind, s.layer) for s in spans)

        self._cache[cache_key] = (masked, hits)
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

        return masked, Counter(k for k, _ in hits), Counter(l for _, l in hits)
