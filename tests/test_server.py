"""The whole path: request -> redaction -> upstream -> restore -> client.

The upstream is mounted over ASGI, so the test never touches the network and does not
need a free port.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from blackbar.config import Config
from blackbar.server import create_app
from blackbar.stats import read_lines

SEEN: dict = {}


async def _upstream_json(request):
    body = await request.json()
    SEEN["body"] = body
    text = body["messages"][0]["content"]
    if body.get("model") == "boom":
        # API errors come back as JSON even with stream=true, and can quote the prompt.
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": f"bad field: {text}"}},
            status_code=400,
        )
    if body.get("stream"):
        return await _upstream_stream(request)
    # The upstream echoes what it received, so the placeholder comes back in the reply.
    return JSONResponse({
        "id": "msg_1",
        "type": "message",
        "content": [{"type": "text", "text": f"i see {text}"}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 500},
    })


async def _upstream_stream(request):
    body = SEEN["body"]
    placeholder = body["messages"][0]["content"].split("contact: ")[1]

    async def gen():
        yield b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        yield b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        # Placeholder split across two events - the most common failure case.
        head, tail = placeholder[:7], placeholder[7:]
        for part in (f"writing to {head}", tail):
            payload = json.dumps({"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": part}})
            yield f"event: content_block_delta\ndata: {payload}\n\n".encode()
        yield b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _upstream_other(request):
    return JSONResponse({"ok": True, "path": request.url.path})


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    upstream = Starlette(routes=[
        Route("/v1/messages", _upstream_json, methods=["POST"]),
        Route("/v1/messages/count_tokens", _upstream_json, methods=["POST"]),
        Route("/{path:path}", _upstream_other, methods=["GET", "POST"]),
    ])

    config = Config(
        layers=["rules", "regex"],  # no GLiNER: keeps the test fast and download-free
        upstream="http://upstream.test",
    )
    app = create_app(config)
    app.state.proxy.client = httpx.AsyncClient(transport=httpx.ASGITransport(upstream))
    return app


async def _call(app, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        return await client.post(path, **kwargs)


async def test_upstream_never_sees_the_original_but_client_does(proxy):
    response = await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "write to jan@example.com"}],
    })
    assert response.status_code == 200

    sent = SEEN["body"]["messages"][0]["content"]
    assert "jan@example.com" not in sent
    assert "{{sensitive:email:" in sent

    assert "jan@example.com" in response.json()["content"][0]["text"]


async def test_streaming_reassembles_split_placeholder(proxy):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(proxy), base_url="http://proxy") as client:
        async with client.stream("POST", "/v1/messages", json={
            "model": "claude-opus-5",
            "stream": True,
            "messages": [{"role": "user", "content": "contact: jan@example.com"}],
        }) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    assert "jan@example.com" in body
    assert "{{sensitive:" not in body
    assert "jan@example.com" not in json.dumps(SEEN["body"])


async def test_api_error_is_restored_too(proxy):
    """An error with stream=true comes back as JSON - no placeholder may reach the client."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(proxy), base_url="http://proxy") as client:
        async with client.stream("POST", "/v1/messages", json={
            "model": "boom",
            "stream": True,
            "messages": [{"role": "user", "content": "contact: jan@example.com"}],
        }) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    assert response.status_code == 400
    assert "jan@example.com" in body
    assert "{{sensitive:" not in body


async def test_unhandled_post_is_refused_not_forwarded(proxy):
    """An endpoint we cannot redact must not carry data out behind our back."""
    SEEN.clear()
    response = await _call(proxy, "/v1/experimental/thing", json={
        "messages": [{"role": "user", "content": "write to jan@example.com"}],
    })
    assert response.status_code == 501
    assert response.json()["error"]["type"] == "blackbar_unhandled_endpoint"
    assert "body" not in SEEN


async def test_count_tokens_is_redacted(proxy):
    """Same payload as /v1/messages, so it goes through the same redaction."""
    await _call(proxy, "/v1/messages/count_tokens", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "write to jan@example.com"}],
    })
    assert "jan@example.com" not in json.dumps(SEEN["body"])


async def test_other_paths_pass_through(proxy):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(proxy), base_url="http://proxy") as client:
        response = await client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["path"] == "/v1/models"


async def test_request_lands_in_the_log_file(proxy):
    """One line per request, readable with tail - and carrying no values."""
    await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "write to jan@example.com"}],
    })
    path = proxy.state.proxy.config.requests_path
    raw = path.read_text(encoding="utf-8").strip()
    assert "jan@example.com" not in raw

    latest = read_lines(path, limit=1)[0]
    assert latest["masked"] == 1
    assert latest["restored"] == 1
    assert latest["orphans"] == 0
    assert latest["cache_read"] == 500
    assert latest["kinds"] == {"email": 1}
    assert latest["keys"][0][0] == "email"


def _pdf(*lines: str) -> str:
    """A real one-page PDF, base64 - the same shape Claude Code sends."""
    import base64, io
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    page = canvas.Canvas(buf)
    for offset, line in enumerate(lines):
        page.drawString(72, 800 - offset * 20, line)
    page.showPage()
    page.save()
    return base64.b64encode(buf.getvalue()).decode()


async def test_pdf_is_read_locally_and_redacted(proxy):
    """The PDF never leaves as a PDF: its text is extracted here and masked."""
    SEEN.clear()
    response = await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "here is the invoice"},
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": _pdf("Invoice 2026/07", "Contact: jan@example.com")}},
            ]},
        ]}],
    })
    assert response.status_code == 200

    sent = json.dumps(SEEN["body"])
    assert "jan@example.com" not in sent          # the address was masked
    assert "{{sensitive:email:" in sent
    assert "Invoice 2026/07" in sent              # the rest of the text got through
    assert '"document"' not in sent               # and it went as text, not as a file


async def test_pdf_without_a_text_layer_is_refused(proxy):
    """A scan is a picture in a PDF wrapper - nothing to read, so nothing goes out."""
    SEEN.clear()
    response = await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": "bm90IGEgcGRm"}},
        ]}],
    })
    assert response.status_code == 501
    assert "no text to read" in response.json()["error"]["message"]
    assert "body" not in SEEN


async def test_a_type_can_be_allowed_knowingly(proxy):
    """Opt-in, per the config - and it still goes out unredacted."""
    proxy.state.proxy.config.allow = ["image/png"]
    SEEN.clear()
    response = await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"}},
        ]}],
    })
    proxy.state.proxy.config.allow = []
    assert response.status_code == 200
    assert "iVBOR" in json.dumps(SEEN["body"])


async def test_screenshot_is_refused(proxy):
    SEEN.clear()
    response = await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"}},
        ]}],
    })
    assert response.status_code == 501
    assert "body" not in SEEN


async def test_refusal_is_written_to_the_log(proxy):
    await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"}},
        ]}],
    })
    latest = read_lines(proxy.state.proxy.config.requests_path, limit=1)[0]
    assert latest["refused"] == "attachment"
    assert latest["status"] == 501


async def test_a_csv_attachment_is_read_and_redacted(proxy):
    """Any type we can turn into text follows the same path as a PDF."""
    import base64

    csv = base64.b64encode(b"name,email\nJan Kowalski,jan@example.com\n").decode()
    SEEN.clear()
    response = await _call(proxy, "/v1/messages", json={
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {
                "type": "base64", "media_type": "text/csv", "data": csv}},
        ]}],
    })
    assert response.status_code == 200
    sent = json.dumps(SEEN["body"])
    assert "jan@example.com" not in sent
    assert "{{sensitive:email:" in sent
    assert "name,email" in sent


async def test_status_reports_what_it_covers(proxy):
    data = await _admin(proxy, "/_admin/status")
    assert "PDF" in data["attachments_read"]
    assert any(item.startswith("text/*") for item in data["attachments_read"])
    assert data["attachments_allowed"] == []
    assert data["endpoints"] == ["/v1/messages", "/v1/messages/count_tokens"]
    assert data["started_ts"] > 0


async def _admin(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
        response = await client.get(path)
    return response.json()
