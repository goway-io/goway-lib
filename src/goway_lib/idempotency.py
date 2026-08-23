import hashlib
import json
import logging

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 60 * 60
HEADER = "Idempotency-Key"


def _storage_key(request: Request, key: str) -> str:
    actor = request.headers.get("authorization", "")
    digest = hashlib.sha256(f"{actor}:{request.method}:{request.url.path}:{key}".encode()).hexdigest()
    return f"idem:{digest}"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis: Redis):
        super().__init__(app)
        self.redis = redis

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get(HEADER)
        if request.method != "POST" or not key:
            return await call_next(request)

        skey = _storage_key(request, key)
        try:
            stored = await self.redis.get(skey)
        except Exception as exc:
            logger.warning("idempotency read failed: %s", exc)
            stored = None
        if stored:
            data = json.loads(stored)
            return Response(
                content=data["body"],
                status_code=data["status"],
                media_type=data["media_type"],
                headers={"Idempotency-Replayed": "true"},
            )

        response = await call_next(request)
        if 200 <= response.status_code < 300:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            try:
                await self.redis.setex(
                    skey,
                    TTL_SECONDS,
                    json.dumps(
                        {
                            "status": response.status_code,
                            "media_type": response.media_type or "application/json",
                            "body": body.decode(),
                        }
                    ),
                )
            except Exception as exc:
                logger.warning("idempotency store failed: %s", exc)
            return Response(
                content=body,
                status_code=response.status_code,
                media_type=response.media_type,
                headers=dict(response.headers),
            )
        return response
