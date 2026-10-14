"""Auth dependencies shared by every service.

Two entry paths resolve to the same Actor:
  1. X-Internal-Identity — HMAC-signed header injected by the edge; staleness
     was already enforced there.
  2. Authorization: Bearer — direct access-token verification. Until the edge
     ships this path also enforces the two staleness checks (role rv, per-user
     revocation) against Redis, failing OPEN — Redis is never a hard auth
     dependency.

Access tokens are HS256 JWTs: sub, org, role, rid, rv, iat, exp.
"""

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Request
from redis.asyncio import Redis

from goway_lib.errors import Forbidden, Unauthorized
from goway_lib.timeutils import unix_ts

logger = logging.getLogger(__name__)

IDENTITY_HEADER = "x-internal-identity"
IDENTITY_SIG_HEADER = "x-internal-signature"
ACCESS_TOKEN_TTL_SECONDS = 10 * 60


def role_version_key(role_id: uuid.UUID) -> str:
    return f"role:{role_id}:version"


def user_revocation_key(user_id: uuid.UUID) -> str:
    return f"user:{user_id}:revoked_at"


@dataclass(frozen=True)
class Actor:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str
    role_id: uuid.UUID | None
    rv: int
    iat: int


_jwt_secret: str = ""
_identity_secret: str = ""
_staleness_redis: Redis | None = None


def configure_auth(*, jwt_secret: str, identity_secret: str = "", staleness_redis: Redis | None = None) -> None:
    global _jwt_secret, _identity_secret, _staleness_redis
    _jwt_secret = jwt_secret
    _identity_secret = identity_secret or jwt_secret
    _staleness_redis = staleness_redis


def mint_access_token(*, user_id: uuid.UUID, org_id: uuid.UUID, role: str, role_id: uuid.UUID, rv: int) -> str:
    now = unix_ts()
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "role": role,
        "rid": str(role_id),
        "rv": rv,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _jwt_secret, algorithm="HS256")


def _invalid_token() -> Unauthorized:
    return Unauthorized("Invalid token", code="TOKEN_INVALID", key="auth.token_invalid")


def _actor_from_claims(claims: dict) -> Actor:
    """A correctly signed token with missing or malformed claims is as invalid as a forged one."""
    try:
        rid = claims.get("rid")
        return Actor(
            user_id=uuid.UUID(claims["sub"]),
            org_id=uuid.UUID(claims["org"]),
            role=claims["role"],
            role_id=uuid.UUID(rid) if rid else None,
            rv=int(claims.get("rv", 0)),
            iat=int(claims["iat"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise _invalid_token() from exc


def verify_access_token(token: str) -> Actor:
    try:
        claims = jwt.decode(token, _jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Token expired", code="TOKEN_EXPIRED", key="auth.token_expired") from exc
    except jwt.PyJWTError as exc:
        raise _invalid_token() from exc
    return _actor_from_claims(claims)


async def enforce_staleness(actor: Actor) -> None:
    if _staleness_redis is None:
        return
    try:
        if actor.role_id is not None:
            current = await _staleness_redis.get(role_version_key(actor.role_id))
            if current is not None and int(current) != actor.rv:
                raise Unauthorized("Permissions changed, re-login", code="TOKEN_STALE", key="auth.token_stale")
        revoked_at = await _staleness_redis.get(user_revocation_key(actor.user_id))
        if revoked_at is not None and actor.iat < int(revoked_at):
            raise Unauthorized("Session revoked", code="TOKEN_REVOKED", key="auth.token_revoked")
    except Unauthorized:
        raise
    except Exception as exc:
        logger.warning("staleness check failed open: %s", exc)


def sign_identity(payload: dict) -> tuple[str, str]:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(_identity_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw, sig


def _verify_identity_header(raw: str, sig: str) -> Actor:
    expected = hmac.new(_identity_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise Unauthorized("Invalid identity signature", code="IDENTITY_INVALID")
    try:
        claims = json.loads(raw)
    except ValueError as exc:
        raise Unauthorized("Invalid identity signature", code="IDENTITY_INVALID") from exc
    return _actor_from_claims(claims)


def actor_from_headers(headers) -> tuple[Actor, bool]:
    """Who sent the request, by either entry path; the flag says whether staleness is still to be checked."""
    identity = headers.get(IDENTITY_HEADER)
    if identity:
        return _verify_identity_header(identity, headers.get(IDENTITY_SIG_HEADER, "")), False
    auth = headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise Unauthorized("Missing credentials", code="TOKEN_MISSING", key="auth.token_missing")
    return verify_access_token(auth.removeprefix("Bearer ")), True


async def current_actor(request: Request) -> Actor:
    actor, check_staleness = actor_from_headers(request.headers)
    if check_staleness:
        await enforce_staleness(actor)
    return actor


CurrentActor = Annotated[Actor, Depends(current_actor)]


class Requires:
    def __init__(self, role: str | None = None, *, any_of: tuple[str, ...] = ()):
        self.allowed = any_of or ((role,) if role else ())

    async def __call__(self, actor: CurrentActor) -> Actor:
        if self.allowed and actor.role not in self.allowed:
            raise Forbidden(key="auth.forbidden")
        return actor


_service_secret: str = ""


def configure_service_auth(secret: str) -> None:
    global _service_secret
    _service_secret = secret


async def verify_service_auth(request: Request) -> None:
    provided = request.headers.get("x-service-secret", "")
    if not _service_secret or not hmac.compare_digest(provided, _service_secret):
        raise Forbidden("Invalid service credentials", code="SERVICE_AUTH_INVALID")
