import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from goway_lib import idempotency
from goway_lib.auth import configure_auth, mint_access_token
from goway_lib.errors import Conflict
from goway_lib.handlers import install_handlers
from goway_lib.idempotency import IdempotencyMiddleware

SECRET = "test-secret-0123456789abcdef0123456789abcdef"


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("redis is down")

    async def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        self._check()
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self._check()
        self.data[key] = value

    async def delete(self, key):
        self._check()
        self.data.pop(key, None)


class Order(BaseModel):
    cargo: str


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
async def api(redis):
    configure_auth(jwt_secret=SECRET)
    app = FastAPI()
    install_handlers(app)
    app.add_middleware(IdempotencyMiddleware, redis=redis)
    app.state.runs = 0

    @app.post("/orders", status_code=201)
    async def create(order: Order):
        app.state.runs += 1
        if order.cargo == "forbidden":
            raise Conflict("No", code="CARGO_REFUSED")
        return {"number": app.state.runs, "cargo": order.cargo}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.app = app
        yield client


def bearer(user_id, rv=1):
    token = mint_access_token(user_id=user_id, org_id=uuid.uuid7(), role="shipper", role_id=uuid.uuid7(), rv=rv)
    return {"Authorization": f"Bearer {token}"}


async def test_a_retry_gets_the_first_answer_even_after_the_token_was_refreshed(api):
    user = uuid.uuid7()
    first = await api.post("/orders", json={"cargo": "apples"}, headers={**bearer(user), "Idempotency-Key": "k1"})
    again = await api.post("/orders", json={"cargo": "apples"}, headers={**bearer(user, rv=2), "Idempotency-Key": "k1"})

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json() and again.headers["Idempotency-Replayed"] == "true"
    assert api.app.state.runs == 1


async def test_another_user_with_the_same_key_runs_its_own_request(api):
    headers = {"Idempotency-Key": "k1"}
    mine = await api.post("/orders", json={"cargo": "apples"}, headers={**bearer(uuid.uuid7()), **headers})
    theirs = await api.post("/orders", json={"cargo": "apples"}, headers={**bearer(uuid.uuid7()), **headers})

    assert (mine.json()["number"], theirs.json()["number"]) == (1, 2)


async def test_the_same_key_with_another_body_is_refused(api):
    user = uuid.uuid7()
    await api.post("/orders", json={"cargo": "apples"}, headers={**bearer(user), "Idempotency-Key": "k1"})
    other = await api.post("/orders", json={"cargo": "pears"}, headers={**bearer(user), "Idempotency-Key": "k1"})

    assert other.status_code == 422 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert api.app.state.runs == 1


async def test_callers_without_a_token_are_told_apart_by_what_they_sent(api):
    headers = {"Idempotency-Key": "k1"}
    first = await api.post("/orders", json={"cargo": "apples"}, headers=headers)
    retry = await api.post("/orders", json={"cargo": "apples"}, headers=headers)
    stranger = await api.post("/orders", json={"cargo": "pears"}, headers=headers)

    assert retry.json() == first.json() and retry.headers["Idempotency-Replayed"] == "true"
    assert stranger.json()["number"] == 2, "another body is another caller, never a replay of someone's answer"


async def test_a_copy_arriving_while_the_first_still_runs_is_told_to_wait(api, redis):
    user = uuid.uuid7()
    headers = {**bearer(user), "Idempotency-Key": "k1"}
    await api.post("/orders", json={"cargo": "apples"}, headers=headers)
    [skey] = redis.data
    redis.data[skey] = idempotency.PENDING

    waiting = await api.post("/orders", json={"cargo": "apples"}, headers=headers)

    assert waiting.status_code == 409 and waiting.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert waiting.headers["Retry-After"] == "1" and api.app.state.runs == 1


async def test_a_refused_request_can_be_sent_again_with_the_same_key(api, redis):
    headers = {**bearer(uuid.uuid7()), "Idempotency-Key": "k1"}
    refused = await api.post("/orders", json={"cargo": "forbidden"}, headers=headers)
    again = await api.post("/orders", json={"cargo": "forbidden"}, headers=headers)

    assert (refused.status_code, again.status_code) == (409, 409)
    assert api.app.state.runs == 2 and redis.data == {}


async def test_redis_being_down_never_fails_the_request(api, redis):
    redis.down = True
    response = await api.post("/orders", json={"cargo": "apples"}, headers={"Idempotency-Key": "k1"})

    assert response.status_code == 201 and api.app.state.runs == 1
