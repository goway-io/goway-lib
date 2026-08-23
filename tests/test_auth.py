import uuid

import pytest

from goway_lib.auth import (
    Actor,
    configure_auth,
    enforce_staleness,
    mint_access_token,
    role_version_key,
    sign_identity,
    user_revocation_key,
    verify_access_token,
)
from goway_lib.errors import Unauthorized
from goway_lib.timeutils import unix_ts

SECRET = "test-secret-0123456789abcdef0123456789abcdef"


class FakeRedis:
    def __init__(self, data=None):
        self.data = data or {}

    async def get(self, key):
        return self.data.get(key)


def make_actor(role_id, rv=1, iat=None):
    return Actor(
        user_id=uuid.uuid7(),
        org_id=uuid.uuid7(),
        role="carrier",
        role_id=role_id,
        rv=rv,
        iat=iat if iat is not None else unix_ts(),
    )


def test_token_roundtrip():
    configure_auth(jwt_secret=SECRET)
    user_id, org_id, role_id = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    token = mint_access_token(user_id=user_id, org_id=org_id, role="driver", role_id=role_id, rv=3)
    actor = verify_access_token(token)
    assert (actor.user_id, actor.org_id, actor.role, actor.role_id, actor.rv) == (
        user_id,
        org_id,
        "driver",
        role_id,
        3,
    )


def test_bad_token_rejected():
    configure_auth(jwt_secret=SECRET)
    with pytest.raises(Unauthorized):
        verify_access_token("not-a-token")


async def test_stale_rv_rejected():
    role_id = uuid.uuid7()
    configure_auth(jwt_secret=SECRET, staleness_redis=FakeRedis({role_version_key(role_id): "2"}))
    with pytest.raises(Unauthorized) as exc:
        await enforce_staleness(make_actor(role_id, rv=1))
    assert exc.value.code == "TOKEN_STALE"
    await enforce_staleness(make_actor(role_id, rv=2))


async def test_revoked_token_rejected():
    actor = make_actor(uuid.uuid7(), iat=1000)
    configure_auth(jwt_secret=SECRET, staleness_redis=FakeRedis({user_revocation_key(actor.user_id): "2000"}))
    with pytest.raises(Unauthorized) as exc:
        await enforce_staleness(actor)
    assert exc.value.code == "TOKEN_REVOKED"


async def test_staleness_fails_open():
    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("down")

    configure_auth(jwt_secret=SECRET, staleness_redis=BrokenRedis())
    await enforce_staleness(make_actor(uuid.uuid7()))
    configure_auth(jwt_secret=SECRET)


def test_identity_signing_stable():
    configure_auth(jwt_secret=SECRET)
    raw, sig = sign_identity({"sub": "x", "org": "y", "role": "driver", "iat": 1, "rv": 0})
    raw2, sig2 = sign_identity({"rv": 0, "iat": 1, "role": "driver", "org": "y", "sub": "x"})
    assert raw == raw2 and sig == sig2
