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
from .proxy import redact_request, restore_all, restore_response, session_key
from .sse import SSERewriter
from .stats import EventLog, RequestEvent
from .vault import Vault

# Headers that must not be forwarded - they describe a single connection.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class ProxyState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.vault = Vault()
        self.log = EventLog(config.db_path)
        self.paused = False
        self.started_at = time.time()
        self.subscribers: set[asyncio.Queue] = set()
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

    def publish(self, event: RequestEvent) -> None:
        payload = event.as_dict()
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def record(self, event: RequestEvent) -> None:
        self.log.record(event)
        self.publish(event)


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
        state.log.close()

    app = Starlette(routes=_routes(state), lifespan=lifespan)
    app.state.proxy = state
    return app


def _routes(state: ProxyState) -> list[Route]:
    async def messages(request: Request) -> Response:
        return await _handle_messages(state, request)

    async def passthrough(request: Request) -> Response:
        return await _handle_passthrough(state, request)

    return [
        Route("/_admin/health", _admin_health(state), methods=["GET"]),
        Route("/_admin/status", _admin_status(state), methods=["GET"]),
        Route("/_admin/stats", _admin_stats(state), methods=["GET"]),
        Route("/_admin/last", _admin_last(state), methods=["GET"]),
        Route("/_admin/watch", _admin_watch(state), methods=["GET"]),
        Route("/_admin/pause", _admin_pause(state, True), methods=["POST"]),
        Route("/_admin/resume", _admin_pause(state, False), methods=["POST"]),
        Route("/_admin/vault", _admin_vault(state), methods=["GET"]),
        Route("/_admin/vault/clear", _admin_vault_clear(state), methods=["POST"]),
        Route("/_admin/rules/reload", _admin_rules_reload(state), methods=["POST"]),
        Route("/_admin/test", _admin_test(state), methods=["POST"]),
        Route("/{provider}/v1/messages", messages, methods=["POST"]),
        Route("/{provider}/{path:path}", passthrough, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    ]


def _upstream(state: ProxyState, provider: str) -> str | None:
    entry = state.config.providers.get(provider)
    return entry.upstream if entry else None


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
    provider = request.path_params["provider"]
    upstream_base = _upstream(state, provider)
    if upstream_base is None:
        return JSONResponse({"error": f"unknown provider: {provider}"}, status_code=404)

    raw = await request.body()
    try:
        body: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return await _proxy_raw(state, request, upstream_base, raw)

    started = time.perf_counter()
    event = RequestEvent(
        provider=provider,
        model=str(body.get("model") or ""),
        streaming=bool(body.get("stream")),
        paused=state.paused,
        session=session_key(body, request.headers.get("user-agent", "")),
    )

    if state.paused:
        payload = raw
    else:
        detect_started = time.perf_counter()
        kinds, layers, masked = await redact_request(body, state.redactor)
        event.detect_ms = (time.perf_counter() - detect_started) * 1000
        event.kinds = dict(kinds)
        event.layers = dict(layers)
        event.pairs = _pairs(state, kinds, layers)
        event.masked = masked
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    url = f"{upstream_base}{request.url.path[len('/' + provider):]}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = _forward_headers(request)
    state.request_count += 1

    if body.get("stream"):
        return await _stream_response(state, url, headers, payload, event, started)

    try:
        upstream = await state.client.post(url, content=payload, headers=headers)
    except httpx.HTTPError as exc:
        event.status = 502
        event.total_ms = (time.perf_counter() - started) * 1000
        state.record(event)
        return JSONResponse({"error": {"type": "blackbar_upstream_error", "message": str(exc)}}, status_code=502)

    event.status = upstream.status_code
    content = upstream.content
    if upstream.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if not state.paused:
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
    state.record(event)
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
        state.record(event)
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
        if isinstance(data, dict) and not state.paused:
            data, restored, orphans = restore_all(data, state.vault)
            event.restored = restored
            event.orphans = orphans
            raw_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        event.total_ms = (time.perf_counter() - started) * 1000
        state.record(event)
        return Response(content=raw_body, status_code=upstream.status_code, headers=_response_headers(upstream))

    rewriter = SSERewriter(state.vault)

    async def body_stream():
        try:
            async for chunk in upstream.aiter_raw():
                if state.paused:
                    yield chunk
                    continue
                out = rewriter.feed(chunk)
                if out:
                    yield out
            if not state.paused:
                tail = rewriter.close()
                if tail:
                    yield tail
        finally:
            await upstream.aclose()
            event.restored = rewriter.stats.restored
            event.orphans = rewriter.stats.orphans
            event.usage = rewriter.stats.usage
            event.total_ms = (time.perf_counter() - started) * 1000
            state.record(event)

    return StreamingResponse(
        body_stream(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


async def _handle_passthrough(state: ProxyState, request: Request) -> Response:
    provider = request.path_params["provider"]
    upstream_base = _upstream(state, provider)
    if upstream_base is None:
        return JSONResponse({"error": f"unknown provider: {provider}"}, status_code=404)
    raw = await request.body()
    return await _proxy_raw(state, request, upstream_base, raw)


async def _proxy_raw(state: ProxyState, request: Request, upstream_base: str, raw: bytes) -> Response:
    provider = request.path_params["provider"]
    path = request.url.path[len("/" + provider) :]
    url = f"{upstream_base}{path}"
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


def _pairs(state: ProxyState, kinds, layers) -> dict[tuple[str, str], int]:
    """Approximation: with a single layer the attribution is exact, with several the
    kinds are attributed to the dominant layer."""
    if not kinds:
        return {}
    if len(layers) == 1:
        layer = next(iter(layers))
        return {(kind, layer): count for kind, count in kinds.items()}
    dominant = max(layers.items(), key=lambda item: item[1])[0] if layers else "unknown"
    return {(kind, dominant): count for kind, count in kinds.items()}


# --- admin endpoints ----------------------------------------------------------

def _admin_health(state: ProxyState):
    async def handler(request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, "pid_port": state.config.port})
    return handler


def _admin_status(state: ProxyState):
    async def handler(request: Request) -> Response:
        summary = state.log.summary(since=time.time() - 3600)
        return JSONResponse({
            "version": __version__,
            "paused": state.paused,
            "uptime_s": round(time.time() - state.started_at, 1),
            "port": state.config.port,
            "requests": state.request_count,
            "model": state.config.model,
            "model_loaded": bool(state.gliner and state.gliner.loaded),
            "model_error": state.gliner.error if state.gliner else None,
            "layers": state.config.layers,
            "rules_count": state.rules.count,
            "rules_error": state.rules.error,
            "vault": state.vault.stats(),
            "sessions_last_hour": summary["sessions"],
            "providers": {name: p.upstream for name, p in state.config.providers.items()},
        })
    return handler


def _admin_stats(state: ProxyState):
    async def handler(request: Request) -> Response:
        since = request.query_params.get("since")
        return JSONResponse(state.log.summary(float(since) if since else None))
    return handler


def _admin_last(state: ProxyState):
    async def handler(request: Request) -> Response:
        limit = int(request.query_params.get("n", 5))
        return JSONResponse({"requests": state.log.recent(limit)})
    return handler


def _admin_watch(state: ProxyState):
    async def handler(request: Request) -> Response:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        state.subscribers.add(queue)

        async def stream():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
            finally:
                state.subscribers.discard(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")
    return handler


def _admin_pause(state: ProxyState, paused: bool):
    async def handler(request: Request) -> Response:
        state.paused = paused
        return JSONResponse({"paused": state.paused})
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
