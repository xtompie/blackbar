"""Redaction inside the Anthropic Messages API contract.

Only the parts that carry user data are scanned: `system`, message content and tool
results. Tool definitions (`tools[].description`) are left alone - they describe an
interface, not data, and rewriting them would break the model's understanding of its
own tools.

Unknown fields pass through untouched, which is why this does not have to keep up with
every API change.
"""

from __future__ import annotations

import hashlib
from collections import Counter

from .detect import Redactor
from .vault import Vault


async def redact_request(
    body: dict, redactor: Redactor
) -> tuple[Counter[str], Counter[str], int, list[tuple[str, str]]]:
    """Redacts the body in place.

    Returns (kinds, layers, replacement count, [(kind, vault key)]).
    """
    kinds: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    async def scan(text: str) -> str:
        masked, hit_kinds, hit_layers, hit_keys = await redactor.redact(text)
        kinds.update(hit_kinds)
        layers.update(hit_layers)
        for entry in hit_keys:
            if entry not in seen:
                seen.add(entry)
                keys.append(entry)
        return masked

    system = body.get("system")
    if isinstance(system, str):
        body["system"] = await scan(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block["text"] = await scan(block["text"])

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = await scan(content)
        elif isinstance(content, list):
            for block in content:
                await _scan_block(block, scan)

    return kinds, layers, sum(kinds.values()), keys


async def _scan_block(block: dict, scan) -> None:
    if not isinstance(block, dict):
        return
    block_type = block.get("type")

    if block_type == "text" and isinstance(block.get("text"), str):
        block["text"] = await scan(block["text"])

    elif block_type == "tool_result":
        # The main leak source: tool output, i.e. file contents and command results.
        content = block.get("content")
        if isinstance(content, str):
            block["content"] = await scan(content)
        elif isinstance(content, list):
            for inner in content:
                await _scan_block(inner, scan)

    elif block_type == "tool_use":
        # Arguments the model passed to a tool in an earlier turn - they come back as
        # history, so they have to be masked like everything else.
        block["input"] = await _scan_json(block.get("input"), scan)

    elif block_type == "thinking" and isinstance(block.get("thinking"), str):
        # Thinking blocks are signed by the API; rewriting them breaks the signature.
        return


async def _scan_json(value, scan):
    if isinstance(value, str):
        return await scan(value)
    if isinstance(value, list):
        return [await _scan_json(item, scan) for item in value]
    if isinstance(value, dict):
        return {key: await _scan_json(item, scan) for key, item in value.items()}
    return value


# Attachments are base64, not text. A PDF can be opened here and turned into text that
# the normal redaction path handles; a screenshot cannot be read at all.
def handle_attachments(body: dict, pdf_mode: str, image_mode: str) -> list[str]:
    """Converts PDFs to text in place. Returns what is left that we cannot redact.

    pdf_mode:   extract | block | send
    image_mode: block | send
    """
    from .pdf import extract_text

    blocked: list[str] = []

    def walk(content) -> None:
        if not isinstance(content, list):
            return
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")

            if kind == "tool_result":
                walk(block.get("content"))
                continue
            if kind not in ("document", "image"):
                continue

            source = block.get("source") or {}
            media = str(source.get("media_type") or kind)
            mode = pdf_mode if kind == "document" else image_mode

            if mode == "send":
                continue
            if kind == "document" and mode == "extract" and source.get("type") == "base64":
                text = extract_text(str(source.get("data") or ""))
                if text is not None:
                    # Replaced in place, so the text goes through the same scan as
                    # everything else in the request.
                    content[index] = {"type": "text", "text": text}
                    continue
                # No text layer: this is a scan, i.e. a picture in a PDF wrapper.
                media = f"{media} (no text layer)"
            blocked.append(media)

    for message in body.get("messages") or []:
        if isinstance(message, dict):
            walk(message.get("content"))
    return blocked


def restore_response(body: dict, vault: Vault) -> tuple[int, int]:
    """Restores originals in a non-streaming response. Returns (restored, orphans)."""
    restored = 0
    orphans = 0

    def restore(text: str) -> str:
        nonlocal restored, orphans
        out, count, missing = vault.restore(text)
        restored += count
        orphans += missing
        return out

    def walk(value):
        if isinstance(value, str):
            return restore(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            block["text"] = restore(block["text"])
        if block.get("type") == "tool_use" and "input" in block:
            block["input"] = walk(block["input"])

    return restored, orphans


def restore_all(value, vault: Vault) -> tuple[object, int, int]:
    """Restores originals in every string of a structure.

    Used for responses that are not a plain `content` array - above all API errors,
    where a placeholder can come back inside the error message.
    """
    restored = 0
    orphans = 0

    def walk(node):
        nonlocal restored, orphans
        if isinstance(node, str):
            out, count, missing = vault.restore(node)
            restored += count
            orphans += missing
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return walk(value), restored, orphans


def session_key(body: dict, user_agent: str = "") -> str:
    """Stable session id for the "traffic per window" metric.

    Claude Code's system prompt contains the working directory among other things and
    stays constant within a session, which makes it a usable fingerprint. Only the hash
    is ever stored.
    """
    system = body.get("system")
    if isinstance(system, list):
        seed = "".join(
            block.get("text", "") for block in system if isinstance(block, dict)
        )[:400]
    else:
        seed = str(system or "")[:400]
    return hashlib.blake2b((user_agent + seed).encode("utf-8"), digest_size=3).hexdigest()
