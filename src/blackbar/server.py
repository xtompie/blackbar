"""The proxy daemon: routing, redaction, passthrough, admin endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from .config import Config
from .detect import Redactor
from .detect.gliner_layer import GlinerDetector
from .detect.regexes import RegexDetector
from .detect.rules import RulesDetector
from .proxy import handle_attachments, redact_request, restore_all, restore_response, session_key
from .sse import SSERewriter
from .stats import RequestEvent, RequestLog, exchanges, read_lines, summary
from .vault import Vault

# Endpoints that carry a prompt and are therefore redacted. Anything else that could
# carry one is refused rather than guessed at - see _handle_unhandled.
REDACTED_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}

# Headers that must not be forwarded - they describe a single connection.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class ProxyState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.vault = Vault()
        self.log = RequestLog(config.requests_path)
        self.started_at = time.time()
        self.request_count = 0

        rules = RulesDetector(config.rules_path)
        gliner = None
        if "gliner" in config.layers:
            gliner = GlinerDetector(config.model, config.threshold)
        self.rules = rules
        self.gliner = gliner
        self.redactor = Redactor(
            self.vault,
            rules,
            RegexDetector() if "regex" in config.layers else _NullDetector(),
            gliner,
        )
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None, write=60.0))

    def sent(self, event: RequestEvent) -> None:
        """Written before the request goes upstream, so a slow one is visible while it
        is still running."""
        self.log.record_sent(event)

    def back(self, event: RequestEvent) -> None:
        self.log.record_back(event)


class _NullDetector:
    layer = "regex"

    def detect(self, text: str) -> list:
        return []


def create_app(config: Config) -> Starlette:
    state = ProxyState(config)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if state.gliner is not None:
            # Loading takes a while; do it in the background so the proxy accepts
            # traffic immediately.
            asyncio.get_running_loop().run_in_executor(None, state.gliner.load)
        yield
        await state.client.aclose()

    app = Starlette(routes=_routes(state), lifespan=lifespan)
    app.state.proxy = state
    return app


def _routes(state: ProxyState) -> list[Route]:
    async def messages(request: Request) -> Response:
        return await _handle_messages(state, request)

    async def passthrough(request: Request) -> Response:
        return await _handle_passthrough(state, request)

    async def unhandled(request: Request) -> Response:
        return _refuse(request, state)

    return [
        Route("/_admin/health", _admin_health(state), methods=["GET"]),
        Route("/_admin/status", _admin_status(state), methods=["GET"]),
        Route("/_admin/stats", _admin_stats(state), methods=["GET"]),
        Route("/_admin/last", _admin_last(state), methods=["GET"]),
        Route("/_admin/vault", _admin_vault(state), methods=["GET"]),
        Route("/_admin/vault/clear", _admin_vault_clear(state), methods=["POST"]),
        Route("/_admin/rules/reload", _admin_rules_reload(state), methods=["POST"]),
        Route("/_admin/test", _admin_test(state), methods=["POST"]),
        Route("/_admin/mask", _admin_mask(state), methods=["POST"]),
        Route("/_admin/allow/reload", _admin_allow_reload(state), methods=["POST"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", messages, methods=["POST"]),
        # Reads cannot carry a prompt in the body, so they pass through.
        Route("/{path:path}", passthrough, methods=["GET", "HEAD", "OPTIONS"]),
        # Anything else might carry one. We do not guess: we say we do not handle it.
        Route("/{path:path}", unhandled, methods=["POST", "PUT", "PATCH", "DELETE"]),
    ]


def _refuse(request: Request, state: ProxyState | None = None) -> Response:
    """An endpoint we do not redact must not carry data out behind our back."""
    if state is not None:
        state.sent(RequestEvent(
            status=501, refused="unhandled_endpoint",
            path=f"{request.method}:{request.url.path}",
        ))
    return JSONResponse(
        {"error": {
            "type": "blackbar_unhandled_endpoint",
            "message": (
                f"blackbar does not handle {request.method} {request.url.path}, so it cannot "
                f"redact it. Run `blackbar direct claude` to bypass the proxy for one session."
            ),
        }},
        status_code=501,
    )


def _refuse_attachment(state: ProxyState, event: RequestEvent, kinds: list[str]) -> Response:
    """A PDF or an image is base64, parsed on Anthropic's side - we cannot read it, so we
    cannot redact it, so it does not leave the machine through us."""
    event.status = 501
    event.refused = "attachment"
    state.sent(event)
    listed = ", ".join(sorted(set(kinds)))
    return JSONResponse(
        {"error": {
            "type": "blackbar_unredactable_attachment",
            "message": (
                f"blackbar cannot read this attachment ({listed}), so it cannot redact it and "
                f"will not send it. Add the type to attachments.allow in the config to send it "
                f"as-is and unredacted, or run `blackbar direct claude`."
            ),
        }},
        status_code=501,
    )


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    # httpx decompresses the response for us, so we do not ask for an encoding we
    # would not be able to forward as-is.
    headers.pop("accept-encoding", None)
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP and key.lower() != "content-encoding"
    }


async def _handle_messages(state: ProxyState, request: Request) -> Response:
    raw = await request.body()
    try:
        body: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse(request, state)

    started = time.perf_counter()
    event = RequestEvent(
        path=request.url.path,
        model=str(body.get("model") or ""),
        streaming=bool(body.get("stream")),
        session=session_key(body, request.headers.get("user-agent", "")),
    )

    # PDFs become text here; whatever is left is something we cannot read at all.
    opaque = handle_attachments(body, state.config.allow)
    if opaque:
        event.total_ms = (time.perf_counter() - started) * 1000
        return _refuse_attachment(state, event, opaque)

    detect_started = time.perf_counter()
    kinds, layers, masked, keys, scanned = await redact_request(body, state.redactor)
    event.detect_ms = (time.perf_counter() - detect_started) * 1000
    event.kinds = dict(kinds)
    event.layers = dict(layers)
    event.masked = masked
    event.keys = keys
    event.chars = scanned
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    url = f"{state.config.upstream}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = _forward_headers(request)
    state.request_count += 1
    state.sent(event)

    if body.get("stream"):
        return await _stream_response(state, url, headers, payload, event, started)

    try:
        upstream = await state.client.post(url, content=payload, headers=headers)
    except httpx.HTTPError as exc:
        event.status = 502
        event.total_ms = (time.perf_counter() - started) * 1000
        state.back(event)
        return JSONResponse({"error": {"type": "blackbar_upstream_error", "message": str(exc)}}, status_code=502)

    event.status = upstream.status_code
    content = upstream.content
    if upstream.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if upstream.status_code >= 400:
                # API errors can quote a fragment of the prompt back - restore there too.
                data, restored, orphans = restore_all(data, state.vault)
            else:
                restored, orphans = restore_response(data, state.vault)
            event.restored = restored
            event.orphans = orphans
            event.usage = data.get("usage") or {}
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")

    event.total_ms = (time.perf_counter() - started) * 1000
    state.back(event)
    return Response(content=content, status_code=upstream.status_code, headers=_response_headers(upstream))


async def _stream_response(
    state: ProxyState,
    url: str,
    headers: dict[str, str],
    payload: bytes,
    event: RequestEvent,
    started: float,
) -> Response:
    request_obj = state.client.build_request("POST", url, content=payload, headers=headers)
    try:
        upstream = await state.client.send(request_obj, stream=True)
    except httpx.HTTPError as exc:
        event.status = 502
        event.total_ms = (time.perf_counter() - started) * 1000
        state.back(event)
        return JSONResponse({"error": {"type": "blackbar_upstream_error", "message": str(exc)}}, status_code=502)

    event.status = upstream.status_code

    # Even with stream=true the upstream can answer with plain JSON - that is how API
    # errors come back. Without this, placeholders from the error message would reach
    # the client raw.
    if not upstream.headers.get("content-type", "").startswith("text/event-stream"):
        raw_body = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            data, restored, orphans = restore_all(data, state.vault)
            event.restored = restored
            event.orphans = orphans
            raw_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        event.total_ms = (time.perf_counter() - started) * 1000
        state.back(event)
        return Response(content=raw_body, status_code=upstream.status_code, headers=_response_headers(upstream))

    rewriter = SSERewriter(state.vault)

    async def body_stream():
        try:
            async for chunk in upstream.aiter_raw():
                out = rewriter.feed(chunk)
                if out:
                    yield out
            tail = rewriter.close()
            if tail:
                yield tail
        finally:
            await upstream.aclose()
            event.restored = rewriter.stats.restored
            event.orphans = rewriter.stats.orphans
            event.usage = rewriter.stats.usage
            event.total_ms = (time.perf_counter() - started) * 1000
            state.back(event)

    return StreamingResponse(
        body_stream(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


async def _handle_passthrough(state: ProxyState, request: Request) -> Response:
    raw = await request.body()
    return await _proxy_raw(state, request, raw)


async def _proxy_raw(state: ProxyState, request: Request, raw: bytes) -> Response:
    url = f"{state.config.upstream}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    try:
        upstream = await state.client.request(
            request.method, url, content=raw or None, headers=_forward_headers(request)
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"error": {"type": "blackbar_upstream_error", "message": str(exc)}}, status_code=502)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
    )



# --- admin endpoints ----------------------------------------------------------

def _admin_health(state: ProxyState):
    async def handler(request: Request) -> Response:
        ready = state.gliner is None or state.gliner.loaded or bool(state.gliner.error)
        return JSONResponse({
            "ok": True,
            "version": __version__,
            "ready": ready,
            "model_loaded": bool(state.gliner and state.gliner.loaded),
            "model_error": state.gliner.error if state.gliner else None,
        })
    return handler


def _admin_status(state: ProxyState):
    async def handler(request: Request) -> Response:
        from .attachments import supported_types

        entries = read_lines(state.config.requests_path, since=time.time() - 3600)
        recent = summary(entries)
        refusals: dict[str, int] = {}
        for entry in entries:
            if entry.get("refused"):
                refusals[entry["refused"]] = refusals.get(entry["refused"], 0) + 1
        last_entry = read_lines(state.config.requests_path, limit=1)
        since_start = read_lines(state.config.requests_path, since=state.started_at)
        return JSONResponse({
            "version": __version__,
            "uptime_s": round(time.time() - state.started_at, 1),
            "port": state.config.port,
            "requests": len(exchanges(since_start)),
            "model": state.config.model,
            "model_loaded": bool(state.gliner and state.gliner.loaded),
            "model_error": state.gliner.error if state.gliner else None,
            "layers": state.config.layers,
            "rules_count": state.rules.count,
            "rules_error": state.rules.error,
            "vault": state.vault.stats(),
            "sessions_last_hour": [s for s in recent["sessions"] if s["session"] not in ("-", None)],
            "requests_last_hour": recent["totals"]["requests"],
            "masked_last_hour": recent["totals"]["masked"],
            "orphans_last_hour": recent["totals"]["orphans"],
            "refusals_last_hour": refusals,
            "last_request_ts": last_entry[0]["ts"] if last_entry else None,
            "started_ts": state.started_at,
            "attachments_read": supported_types(),
            "attachments_allowed": state.config.allow,
            "endpoints": sorted(REDACTED_PATHS),
            "log_path": str(state.config.requests_path),
            "upstream": state.config.upstream,
        })
    return handler


def _admin_stats(state: ProxyState):
    async def handler(request: Request) -> Response:
        since = request.query_params.get("since")
        entries = read_lines(state.config.requests_path, since=float(since) if since else None)
        return JSONResponse(summary(entries))
    return handler


def _admin_last(state: ProxyState):
    async def handler(request: Request) -> Response:
        limit = int(request.query_params.get("n", 5))
        entries = exchanges(read_lines(state.config.requests_path))[-limit:]
        return JSONResponse({"requests": list(reversed(entries))})
    return handler


def _admin_vault(state: ProxyState):
    async def handler(request: Request) -> Response:
        if request.query_params.get("reveal") == "1":
            return JSONResponse({"entries": [
                {"kind": kind, "key": key, "value": value}
                for kind, key, value in state.vault.entries()
            ]})
        return JSONResponse({"counts": state.vault.stats()})
    return handler


def _admin_vault_clear(state: ProxyState):
    async def handler(request: Request) -> Response:
        state.vault.clear()
        return JSONResponse({"cleared": True})
    return handler


def _admin_rules_reload(state: ProxyState):
    async def handler(request: Request) -> Response:
        state.rules.reload()
        return JSONResponse({"count": state.rules.count, "error": state.rules.error})
    return handler


def _admin_allow_reload(state: ProxyState):
    """Re-reads attachments.allow from the config file, so the change is live."""
    async def handler(request: Request) -> Response:
        from .config import load

        state.config.allow = load(state.config.path).allow
        return JSONResponse({"allow": state.config.allow})
    return handler


def _admin_mask(state: ProxyState):
    """Redacts a piece of text the same way a request is redacted, keeping the mapping
    in the vault so it can be reversed later."""
    async def handler(request: Request) -> Response:
        payload = await request.json()
        text = str(payload.get("text") or "")
        masked, kinds, layers, keys = await state.redactor.redact(text)
        return JSONResponse({
            "text": masked,
            "kinds": dict(kinds),
            "layers": dict(layers),
            "replaced": sum(kinds.values()),
        })
    return handler


def _admin_test(state: ProxyState):
    """Detection dry run behind `blackbar test`.

    Computed in the daemon so it can use the already loaded model - otherwise the CLI
    would have to load GLiNER on every invocation.
    """
    async def handler(request: Request) -> Response:
        from .detect.base import apply_spans

        payload = await request.json()
        text = str(payload.get("text") or "")
        loop = asyncio.get_running_loop()
        spans = await loop.run_in_executor(None, state.redactor.detect_sync, text)
        masked = apply_spans(text, spans, lambda s: state.vault.mask(s.kind, s.text))
        return JSONResponse({
            "masked": masked,
            "spans": [
                {
                    "kind": span.kind,
                    "layer": span.layer,
                    "text": span.text,
                    "placeholder": state.vault.mask(span.kind, span.text),
                }
                for span in spans
            ],
        })
    return handler
