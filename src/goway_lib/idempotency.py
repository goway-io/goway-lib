"""Idempotency-Key for POST: a retried request gets the first answer instead of running twice.

A key belongs to its caller. With a valid bearer token that is the user, so a retry sent after a token
refresh still matches. Without one it is the exact request body, so callers who have not signed in yet
(verifying a code) can never be handed each other's answers.

One key means one request: the same key with another body is refused, and a copy that arrives while the
first is still running is told to wait instead of running beside it. Redis being down never fails a
request — it just runs without the guarantee.
"""

import hashlib
import json
import logging

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from goway_lib.auth import verify_access_token
from goway_lib.errors import Conflict, Problem, Unauthorized, UnprocessableEntity
from goway_lib.handlers import problem_response

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 60 * 60
PENDING_SECONDS = 60
HEADER = "Idempotency-Key"
PENDING = "pending"


def _caller(request: Request, fingerprint: str) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return f"user:{verify_access_token(token).user_id}"
        except Unauthorized:
            pass
    return f"body:{fingerprint}"


def _storage_key(request: Request, key: str, fingerprint: str) -> str:
    scope = f"{_caller(request, fingerprint)}:{request.method}:{request.url.path}:{key}"
    return f"idem:{hashlib.sha256(scope.encode()).hexdigest()}"


def _refusal(request: Request, problem: Problem, **headers: str) -> Response:
    response = problem_response(request, problem)
    response.headers.update(headers)
    return response


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis: Redis):
        super().__init__(app)
        self.redis = redis

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get(HEADER)
        if request.method != "POST" or not key:
            return await call_next(request)

        fingerprint = hashlib.sha256(await request.body()).hexdigest()
        skey = _storage_key(request, key, fingerprint)
        try:
            claimed = await self.redis.set(skey, PENDING, nx=True, ex=PENDING_SECONDS)
            stored = None if claimed else await self.redis.get(skey)
        except Exception as exc:
            logger.warning("idempotency unavailable, running without it: %s", exc)
            return await call_next(request)
        if not claimed:
            return self._answer_again(request, stored, fingerprint)

        try:
            response = await call_next(request)
        except BaseException:
            await self._forget(skey)
            raise
        if not 200 <= response.status_code < 300:
            await self._forget(skey)
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        record = {
            "status": response.status_code,
            "media_type": response.media_type or "application/json",
            "body": body.decode(),
            "fingerprint": fingerprint,
        }
        try:
            await self.redis.setex(skey, TTL_SECONDS, json.dumps(record))
        except Exception as exc:
            logger.warning("idempotency store failed: %s", exc)
            await self._forget(skey)
        return Response(
            content=body,
            status_code=response.status_code,
            media_type=response.media_type,
            headers=dict(response.headers),
        )

    def _answer_again(self, request: Request, stored: bytes | str | None, fingerprint: str) -> Response:
        if isinstance(stored, bytes):
            stored = stored.decode()
        if stored is None or stored == PENDING:
            running = Conflict(
                "The first request with this key is still running",
                code="IDEMPOTENCY_IN_PROGRESS",
                key="idempotency.in_progress",
            )
            return _refusal(request, running, **{"Retry-After": "1"})
        data = json.loads(stored)
        if data.get("fingerprint", fingerprint) != fingerprint:
            reused = UnprocessableEntity(
                "This key was already used for a different request",
                code="IDEMPOTENCY_KEY_REUSED",
                key="idempotency.key_reused",
            )
            return _refusal(request, reused)
        return Response(
            content=data["body"],
            status_code=data["status"],
            media_type=data["media_type"],
            headers={"Idempotency-Replayed": "true"},
        )

    async def _forget(self, skey: str) -> None:
        try:
            await self.redis.delete(skey)
        except Exception as exc:
            logger.warning("idempotency cleanup failed: %s", exc)
