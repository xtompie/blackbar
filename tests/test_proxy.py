from __future__ import annotations

from pathlib import Path

from blackbar.detect import Redactor
from blackbar.detect.rules import RulesDetector
from blackbar.proxy import redact_request, restore_response, session_key
from blackbar.vault import Vault


def _redactor(tmp_path: Path) -> tuple[Vault, Redactor]:
    vault = Vault()
    return vault, Redactor(vault, RulesDetector(tmp_path / "missing.yaml"))


async def test_redacts_system_and_messages(tmp_path):
    vault, redactor = _redactor(tmp_path)
    body = {
        "model": "claude-opus-5",
        "system": "You are writing to jan@example.com",
        "messages": [
            {"role": "user", "content": "send the invoice to anna@example.de"},
        ],
    }
    kinds, layers, masked = await redact_request(body, redactor)
    assert masked == 2
    assert "jan@example.com" not in body["system"]
    assert "anna@example.de" not in body["messages"][0]["content"]


async def test_redacts_tool_result(tmp_path):
    """Tool output is the main leak source - `cat` on a file with client data."""
    vault, redactor = _redactor(tmp_path)
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "client: office@acme.com"}],
                    }
                ],
            }
        ]
    }
    await redact_request(body, redactor)
    text = body["messages"][0]["content"][0]["content"][0]["text"]
    assert "office@acme.com" not in text
    assert "{{sensitive:email:" in text


async def test_thinking_block_is_left_untouched(tmp_path):
    """Thinking blocks are signed by the API - rewriting breaks the signature."""
    vault, redactor = _redactor(tmp_path)
    body = {
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "the address is jan@example.com", "signature": "abc"}
            ]}
        ]
    }
    await redact_request(body, redactor)
    assert body["messages"][0]["content"][0]["thinking"] == "the address is jan@example.com"


async def test_tool_definitions_are_left_untouched(tmp_path):
    vault, redactor = _redactor(tmp_path)
    body = {
        "tools": [{"name": "send", "description": "sends mail to admin@example.com"}],
        "messages": [{"role": "user", "content": "ok"}],
    }
    await redact_request(body, redactor)
    assert body["tools"][0]["description"] == "sends mail to admin@example.com"


async def test_restore_in_response_and_in_tool_arguments(tmp_path):
    vault, redactor = _redactor(tmp_path)
    body = {"messages": [{"role": "user", "content": "save jan@example.com to a file"}]}
    await redact_request(body, redactor)
    placeholder = body["messages"][0]["content"].split("save ")[1].split(" to")[0]

    response = {
        "content": [
            {"type": "text", "text": f"saving {placeholder}"},
            {"type": "tool_use", "name": "Write", "input": {"content": f"mail: {placeholder}"}},
        ]
    }
    restored, orphans = restore_response(response, vault)
    assert restored == 2
    assert orphans == 0
    assert response["content"][0]["text"] == "saving jan@example.com"
    assert response["content"][1]["input"]["content"] == "mail: jan@example.com"


def test_orphan_in_response_is_reported():
    vault = Vault()
    response = {"content": [{"type": "text", "text": "result {{sensitive:email:deadbe}}"}]}
    restored, orphans = restore_response(response, vault)
    assert restored == 0
    assert orphans == 1


def test_session_key_is_stable_for_the_same_prompt():
    body = {"system": "You are Claude Code in /Users/neo/projects/aizen"}
    assert session_key(body, "claude-cli/2.0") == session_key(body, "claude-cli/2.0")
    other = {"system": "You are Claude Code in /Users/neo/projects/other"}
    assert session_key(body, "claude-cli/2.0") != session_key(other, "claude-cli/2.0")
