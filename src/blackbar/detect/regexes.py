"""Layer 1: rigidly structured data - secrets, identifiers, contact details.

Anything a regex can catch is caught here: it is faster and more reliable than a model.
Patterns with a checksum (national IDs, cards) are verified on top of the match, so
random digit runs are not masked.
"""

from __future__ import annotations

import re

from .base import Span

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    # Password hashes: a secret by definition. Bare SHA/MD5 digests are deliberately not
    # here - those are git commits and lockfile checksums, and masking them would break
    # ordinary work with code.
    ("password_hash", re.compile(r"\$(?:2[aby]|argon2(?:id|i|d)|scrypt|6|5|1)\$[^\s\"']{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    ("stripe_key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{16,}\b")),
    ("db_url", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:[^\s:@/]+@\S+")),
    ("url_credentials", re.compile(r"\bhttps?://[^\s:@/]+:[^\s:@/]+@\S+")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Za-z0-9]{4}[ ]?){3,7}[A-Za-z0-9]{1,4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("pesel", re.compile(r"\b\d{11}\b")),
    ("nip", re.compile(r"\b\d{10}\b")),
    ("phone", re.compile(r"(?:\+\d{1,3}[ -]?)?(?:\d{3}[ -]?\d{3}[ -]?\d{3}|\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2})\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("mac_address", re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
]

# Addresses where masking only gets in the way: loopback, private ranges, broadcast.
# The model has to see 127.0.0.1 to be of any help with local configuration.
_IP_SKIP_PREFIXES = ("127.", "0.", "10.", "192.168.", "255.", "169.254.", "224.")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _pesel_ok(digits: str) -> bool:
    """Polish national identification number."""
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    checksum = sum(int(d) * w for d, w in zip(digits, weights)) % 10
    return (10 - checksum) % 10 == int(digits[10])


def _nip_ok(digits: str) -> bool:
    """Polish tax identification number."""
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    checksum = sum(int(d) * w for d, w in zip(digits, weights)) % 11
    return checksum != 10 and checksum == int(digits[9])


def _ipv4_ok(text: str) -> bool:
    parts = text.split(".")
    if any(not (part.isdigit() and 0 <= int(part) <= 255) for part in parts):
        return False
    return not text.startswith(_IP_SKIP_PREFIXES)


def _accept(kind: str, text: str) -> bool:
    if kind == "credit_card":
        digits = re.sub(r"[ -]", "", text)
        return len(digits) in (13, 14, 15, 16, 17, 18, 19) and _luhn_ok(digits)
    if kind == "pesel":
        return _pesel_ok(text)
    if kind == "nip":
        return _nip_ok(text)
    if kind == "ipv4":
        return _ipv4_ok(text)
    if kind == "iban":
        return len(re.sub(r"\s", "", text)) >= 15
    return True


class RegexDetector:
    layer = "regex"

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for kind, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                found = match.group(0)
                if _accept(kind, found):
                    spans.append(Span(match.start(), match.end(), kind, found, self.layer))
        return spans
