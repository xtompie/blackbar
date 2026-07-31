from __future__ import annotations

from blackbar.detect import Redactor
from blackbar.detect.base import Span, merge_spans
from blackbar.detect.regexes import RegexDetector
from blackbar.detect.rules import RulesDetector
from blackbar.vault import Vault


def kinds(text: str) -> set[str]:
    return {span.kind for span in RegexDetector().detect(text)}


def test_emails_and_keys():
    assert "email" in kinds("write to jan.kowalski@example.com")
    assert "aws_key" in kinds("AKIAIOSFODNN7EXAMPLE")
    assert "github_token" in kinds("ghp_" + "a" * 36)
    assert "anthropic_key" in kinds("sk-ant-api03-" + "x" * 30)


def test_password_in_database_url():
    assert "db_url" in kinds("postgres://admin:secret@db.example.com:5432/app")


def test_numbers_with_checksums():
    assert "credit_card" in kinds("card 4111 1111 1111 1111")
    assert "credit_card" not in kinds("number 1234 5678 1234 5678")
    assert "pesel" in kinds("PESEL 44051401359")
    assert "pesel" not in kinds("number 12345678901")


def test_local_addresses_are_left_alone():
    assert "ipv4" not in kinds("server on 127.0.0.1:8555")
    assert "ipv4" not in kinds("gateway 192.168.1.1")
    assert "ipv4" in kinds("host 51.83.12.9")


def test_overlapping_hits_longest_wins():
    spans = [
        Span(0, 10, "phone", "123456789", "regex"),
        Span(0, 20, "custom", "123456789 rest", "rules"),
    ]
    merged = merge_spans(spans)
    assert len(merged) == 1
    assert merged[0].kind == "custom"


def test_custom_rule_takes_precedence(tmp_path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        'terms:\n  - kind: company\n    values:\n      - "Acme Ltd"\n', encoding="utf-8"
    )
    detector = RulesDetector(rules_file)
    spans = detector.detect("invoice for acme ltd")
    assert len(spans) == 1
    assert spans[0].kind == "company"


def test_broken_regex_does_not_kill_the_layer(tmp_path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text('patterns:\n  - kind: x\n    regex: "([unclosed"\n', encoding="utf-8")
    detector = RulesDetector(rules_file)
    assert detector.error is not None
    assert detector.detect("anything") == []


async def test_redactor_replaces_and_counts(tmp_path):
    vault = Vault()
    redactor = Redactor(vault, RulesDetector(tmp_path / "missing.yaml"))
    masked, hit_kinds, hit_layers, _ = await redactor.redact("write to jan@example.com")
    assert "jan@example.com" not in masked
    assert hit_kinds["email"] == 1
    assert hit_layers["regex"] == 1


async def test_same_text_gives_same_result(tmp_path):
    """Determinism is what makes prompt caching survive redaction."""
    vault = Vault()
    redactor = Redactor(vault, RulesDetector(tmp_path / "missing.yaml"))
    first, *_ = await redactor.redact("contact: jan@example.com, tel 501 234 567")
    second, *_ = await redactor.redact("contact: jan@example.com, tel 501 234 567")
    assert first == second


async def test_restore_undoes_redaction(tmp_path):
    vault = Vault()
    redactor = Redactor(vault, RulesDetector(tmp_path / "missing.yaml"))
    original = "write to jan@example.com or anna@example.de"
    masked, *_ = await redactor.redact(original)
    restored, count, orphans = vault.restore(masked)
    assert restored == original
    assert count == 2
    assert orphans == 0
