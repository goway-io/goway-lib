"""Idempotency-Key for POST: a retried request gets the first answer instead of running twice.

A key belongs to its caller. A signed-in caller is the user, by whichever path the request carries (the
edge's identity header or a bearer token), so a retry sent after a token refresh still matches. Without one
it is the request body, so callers who have not signed in yet (verifying a code) can never be handed each
other's answers. JSON bodies are compared by meaning, not by bytes.

One key means one request: the same key with another body is refused, and a copy that arrives while the
first is still running is told to wait instead of running beside it. A response marked `Cache-Control:
no-store` (tokens) is never kept. Redis being down or slow never fails a request — it just runs without the
guarantee.
"""

import asyncio
import base64
import hashlib
import json
import logging

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from goway_lib.auth import actor_from_headers
from goway_lib.errors import Conflict, Problem, Unauthorized, UnprocessableEntity
from goway_lib.handlers import problem_response

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 60 * 60
PENDING_SECONDS = 60
REDIS_TIMEOUT_SECONDS = 0.25
HEADER = "Idempotency-Key"
PENDING = "pending"
NOT_REPLAYED = frozenset({"content-length", "date", "server", "x-request-id"})


def _fingerprint(body: bytes) -> str:
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except ValueError:
        canonical = body
    return hashlib.sha256(canonical).hexdigest()


def _caller(request: Request, fingerprint: str) -> str:
    try:
        actor, _ = actor_from_headers(request.headers)
    except Unauthorized:
        return f"body:{fingerprint}"
    return f"user:{actor.user_id}"


def _storage_key(request: Request, key: str, fingerprint: str) -> str:
    scope = f"{_caller(request, fingerprint)}:{request.method}:{request.url.path}:{key}"
    return f"idem:{hashlib.sha256(scope.encode()).hexdigest()}"


def _refusal(request: Request, problem: Problem, **headers: str) -> Response:
    response = problem_response(request, problem)
    response.headers.update(headers)
    return response


def _kept(response: Response) -> bool:
    return "no-store" not in response.headers.get("cache-control", "").lower()


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis: Redis):
        super().__init__(app)
        self.redis = redis

    async def _redis(self, call):
        return await asyncio.wait_for(call, REDIS_TIMEOUT_SECONDS)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get(HEADER)
        if request.method != "POST" or not key:
            return await call_next(request)

        fingerprint = _fingerprint(await request.body())
        skey = _storage_key(request, key, fingerprint)
        pending = json.dumps({"state": PENDING, "fingerprint": fingerprint})
        try:
            claimed = await self._redis(self.redis.set(skey, pending, nx=True, ex=PENDING_SECONDS))
            stored = None if claimed else await self._redis(self.redis.get(skey))
        except Exception as exc:
            logger.warning("idempotency unavailable, running without it: %s", exc)
            return await call_next(request)
        if not claimed:
            return self._answer_again(request, stored, fingerprint)

        try:
            response = await call_next(request)
            body = b"".join([chunk async for chunk in response.body_iterator])
        except BaseException:
            await self._forget(skey)
            raise
        if not 200 <= response.status_code < 300 or not _kept(response):
            await self._forget(skey)
            return self._rebuilt(response, body)

        record = {
            "status": response.status_code,
            "headers": [[name, value] for name, value in response.headers.items() if name not in NOT_REPLAYED],
            "body": base64.b64encode(body).decode(),
            "fingerprint": fingerprint,
        }
        try:
            await self._redis(self.redis.setex(skey, TTL_SECONDS, json.dumps(record)))
        except Exception as exc:
            logger.warning("idempotency store failed: %s", exc)
            await self._forget(skey)
        return self._rebuilt(response, body)

    @staticmethod
    def _rebuilt(response: Response, body: bytes) -> Response:
        rebuilt = Response(content=body, status_code=response.status_code)
        rebuilt.raw_headers = [(n, v) for n, v in response.raw_headers if n.lower() != b"content-length"]
        rebuilt.headers["content-length"] = str(len(body))
        return rebuilt

    def _answer_again(self, request: Request, stored: bytes | str | None, fingerprint: str) -> Response:
        if isinstance(stored, bytes):
            stored = stored.decode()
        data = json.loads(stored) if stored and stored != PENDING else {"state": PENDING}
        if data.get("fingerprint", fingerprint) != fingerprint:
            reused = UnprocessableEntity(
                "This key was already used for a different request",
                code="IDEMPOTENCY_KEY_REUSED",
                key="idempotency.key_reused",
            )
            return _refusal(request, reused)
        if data.get("state") == PENDING:
            running = Conflict(
                "The first request with this key is still running",
                code="IDEMPOTENCY_IN_PROGRESS",
                key="idempotency.in_progress",
            )
            return _refusal(request, running, **{"Retry-After": "1"})
        if "headers" in data:
            content, headers = base64.b64decode(data["body"]), data["headers"]
        else:
            content, headers = data["body"].encode(), [["content-type", data.get("media_type", "application/json")]]
        replay = Response(content=content, status_code=data["status"])
        for name, value in headers:
            replay.headers.append(name, value)
        replay.headers["Idempotency-Replayed"] = "true"
        return replay

    async def _forget(self, skey: str) -> None:
        try:
            await self._redis(self.redis.delete(skey))
        except Exception as exc:
            logger.warning("idempotency cleanup failed: %s", exc)
