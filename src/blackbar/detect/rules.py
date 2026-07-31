"""Layer 0: your own rules from rules.yaml - what no model can guess.

Client names, project codes, internal domains. Wins over the other layers when hits
overlap.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import Span

DEFAULT_RULES = """\
# blackbar custom rules. After editing: blackbar rules reload
#
# terms    - literal, case-insensitive match
# patterns - regular expressions (Python syntax)

terms:
  # - kind: company
  #   values:
  #     - "Acme Ltd"
  #     - "Northwind Traders"

patterns:
  # - kind: project_code
  #   regex: "PRJ-\\\\d{4}"
  # - kind: email
  #   regex: "[A-Za-z0-9._%+-]+@internal\\\\.example\\\\.com"
"""


class RulesDetector:
    layer = "rules"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._compiled: list[tuple[str, re.Pattern[str]]] = []
        self.error: str | None = None
        self.reload()

    def reload(self) -> None:
        self._compiled = []
        self.error = None
        if self.path is None or not self.path.exists():
            return
        try:
            import yaml

            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # a broken rules file must not kill the proxy
            self.error = f"{type(exc).__name__}: {exc}"
            return

        for entry in data.get("terms") or []:
            kind = str(entry.get("kind", "custom"))
            for value in entry.get("values") or []:
                if not str(value).strip():
                    continue
                self._compiled.append((kind, re.compile(re.escape(str(value)), re.IGNORECASE)))

        for entry in data.get("patterns") or []:
            kind = str(entry.get("kind", "custom"))
            raw = entry.get("regex")
            if not raw:
                continue
            try:
                self._compiled.append((kind, re.compile(str(raw))))
            except re.error as exc:
                self.error = f"bad regex for '{kind}': {exc}"

    @property
    def count(self) -> int:
        return len(self._compiled)

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for kind, pattern in self._compiled:
            for match in pattern.finditer(text):
                if match.group(0):
                    spans.append(Span(match.start(), match.end(), kind, match.group(0), self.layer))
        return spans


def write_default_rules(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_RULES, encoding="utf-8")
