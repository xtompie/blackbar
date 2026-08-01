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


async def test_a_value_seen_once_is_masked_everywhere_after(tmp_path):
    """The vault is consulted before the model: what was a name once stays a name,
    even in a sentence the model would read differently."""
    vault = Vault()
    redactor = Redactor(vault, RulesDetector(tmp_path / "missing.yaml"))
    vault.mask("person", "Jan Kowalski")

    masked, kinds, layers, _ = await redactor.redact("cc Jan Kowalski on the reply")
    assert "Jan Kowalski" not in masked
    assert layers["vault"] == 1


def test_password_hashes_are_secrets_but_git_hashes_are_not():
    assert "password_hash" in kinds("stored: $2b$12$K8Hc1Zq3Xk9lYw0pQeR5tOa7bC4dEfGh")
    assert "password_hash" in kinds("hash = $argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$abcd")
    # a commit sha and a lockfile checksum must survive untouched
    assert kinds("fix in 9bf1add4c3e2f1a0b7d6c5e4f3a2b1c0d9e8f7a6") == set()
    assert kinds('"integrity": "sha512-abc123def456ghi789jkl012mno345pqr678stu"') == set()


def test_digits_inside_urls_and_versions_are_not_data():
    """Every false positive found on a real repository had the same cause: biting a
    chunk out of a longer string."""
    assert kinds("https://support.grammarly.com/hc/en-us/articles/30916398193037-Intro") == set()
    assert kinds("Chrome/120.0.0.0 Safari/537.36") == set()
    assert kinds("set('repository', 'git@github.com:xtompie/aizen.git')") == set()
    assert kinds("20260425.@id-a3f9xk.@p2.@auth.@feature.termsy.md") == set()


def test_css_colours_are_not_phone_numbers():
    """`--color-l0: 255 255 255` has exactly the shape of a Polish phone number, and a
    stylesheet has hundreds of them."""
    assert kinds("--color-l0: 255 255 255;") == set()
    assert kinds("background: rgb(255 255 255 / 0.5);") == set()
    assert kinds("error_page 502 503 504 /maintenance.html;") == set()


def test_phone_numbers_are_still_caught():
    assert "phone" in kinds("zadzwon +48 501 234 567")
    assert "phone" in kinds("tel. 501 234 567")
    assert "phone" in kinds("numer 501-234-567")
    assert "phone" in kinds("kom: 501234567")


def test_tax_id_needs_the_word_next_to_it():
    """Ten digits on their own are just a number - 2147483646 is MAX_INT, and it passes
    the NIP checksum."""
    assert kinds("MAX_SAFE = 2147483646") == set()
    assert "nip" in kinds("NIP: 6472477787")
