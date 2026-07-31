"""Streaming restoration - the most failure-prone part of the project."""

from __future__ import annotations

import json

from blackbar.sse import SSERewriter, StreamRestorer, pending_index
from blackbar.vault import Vault


def _vault_with(kind: str, value: str) -> tuple[Vault, str]:
    vault = Vault()
    return vault, vault.mask(kind, value)


def test_placeholder_in_one_chunk():
    vault, placeholder = _vault_with("email", "jan@example.com")
    restorer = StreamRestorer(vault)
    assert restorer.feed(f"write to {placeholder} today") == "write to jan@example.com today"


def test_placeholder_split_in_half():
    vault, placeholder = _vault_with("email", "jan@example.com")
    restorer = StreamRestorer(vault)
    head, tail = placeholder[:9], placeholder[9:]

    first = restorer.feed("write to " + head)
    assert first == "write to "  # tail held back: it could be the start of a placeholder

    second = restorer.feed(tail + " today")
    assert second == "jan@example.com today"


def test_placeholder_split_across_many_chunks():
    vault, placeholder = _vault_with("person", "Jan Kowalski")
    restorer = StreamRestorer(vault)
    out = "".join(restorer.feed(char) for char in f"contact: {placeholder}.")
    out += restorer.flush()
    assert out == "contact: Jan Kowalski."


def test_template_braces_do_not_stall_the_stream():
    vault = Vault()
    restorer = StreamRestorer(vault)
    # Vue/Jinja syntax is common in code and must not be held back.
    assert restorer.feed("{{ item.name }}") == "{{ item.name }}"
    assert restorer.feed("{{sensitive_data}}") == "{{sensitive_data}}"


def test_unfinished_placeholder_leaves_on_flush():
    vault, placeholder = _vault_with("email", "jan@example.com")
    restorer = StreamRestorer(vault)
    restorer.feed("cut off " + placeholder[:10])
    assert restorer.flush() == placeholder[:10]


def test_orphan_is_counted():
    vault = Vault()
    restorer = StreamRestorer(vault)
    out = restorer.feed("{{sensitive:email:deadbe}}")
    assert out == "{{sensitive:email:deadbe}}"
    assert restorer.orphans == 1


def test_value_in_json_delta_is_escaped():
    vault = Vault()
    placeholder = vault.mask("note", 'line1\nline2 "quoted"')
    restorer = StreamRestorer(vault, json_string=True)
    out = restorer.feed(f'{{"content":"{placeholder}"}}')
    # The result has to parse as JSON - that is the whole point of escaping.
    assert json.loads(out)["content"] == 'line1\nline2 "quoted"'


def test_pending_index_does_not_grow_forever():
    buf = "{{sensitive:" + "a" * 200
    assert pending_index(buf) == len(buf)  # kind too long, so this is not a placeholder


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode("utf-8")


def test_rewriter_restores_text_delta():
    vault = Vault()
    placeholder = vault.mask("person", "Jan Kowalski")
    rewriter = SSERewriter(vault)

    out = rewriter.feed(_sse([
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": f"hello {placeholder}"}}),
    ]))
    out += rewriter.close()
    assert "Jan Kowalski" in out.decode()
    assert rewriter.stats.restored == 1


def test_rewriter_reassembles_event_split_across_chunks():
    vault = Vault()
    placeholder = vault.mask("email", "jan@example.com")
    rewriter = SSERewriter(vault)
    raw = _sse([
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": placeholder}}),
    ])
    first = rewriter.feed(raw[:20])
    second = rewriter.feed(raw[20:])
    out = (first + second + rewriter.close()).decode()
    assert "jan@example.com" in out


def test_rewriter_sends_leftover_before_stop():
    vault = Vault()
    placeholder = vault.mask("email", "jan@example.com")
    rewriter = SSERewriter(vault)
    head, tail = placeholder[:8], placeholder[8:]

    out = rewriter.feed(_sse([
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": head}}),
    ]))
    assert head not in out.decode()

    out += rewriter.feed(_sse([
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": tail}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ]))
    out += rewriter.close()
    text = out.decode()
    assert "jan@example.com" in text
    assert text.index("jan@example.com") < text.index("content_block_stop")


def test_rewriter_collects_usage():
    vault = Vault()
    rewriter = SSERewriter(vault)
    rewriter.feed(_sse([
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 900}}}),
    ]))
    rewriter.close()
    assert rewriter.stats.usage["cache_read_input_tokens"] == 900


def test_rewriter_passes_unknown_events_through():
    vault = Vault()
    rewriter = SSERewriter(vault)
    out = rewriter.feed(b"event: ping\ndata: {\"type\":\"ping\"}\n\n")
    assert b"ping" in out
