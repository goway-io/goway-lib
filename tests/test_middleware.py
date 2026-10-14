from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from goway_lib.middleware import RequestIdMiddleware


async def test_one_request_id_is_seen_by_the_handler_and_returned_to_the_caller():
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/seen")
    async def seen(request: Request) -> dict:
        return {"id": request.headers.get("x-request-id")}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        minted = await client.get("/seen")
        given = await client.get("/seen", headers={"X-Request-Id": "abc-123"})

    assert minted.json()["id"] and minted.json()["id"] == minted.headers["x-request-id"]
    assert (given.json()["id"], given.headers["x-request-id"]) == ("abc-123", "abc-123")


async def test_a_doubled_empty_or_oversized_id_never_comes_back_as_two_or_as_junk():
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/seen")
    async def seen(request: Request) -> dict:
        return {"ids": request.headers.getlist("x-request-id")}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        doubled = await client.get("/seen", headers=[("X-Request-Id", "first"), ("X-Request-Id", "second")])
        empty = await client.get("/seen", headers={"X-Request-Id": ""})
        huge = await client.get("/seen", headers={"X-Request-Id": "x" * 5000})

    assert doubled.json()["ids"] == ["first"] and doubled.headers.get_list("x-request-id") == ["first"]
    for response in (empty, huge):
        [seen_id] = response.json()["ids"]
        assert seen_id and len(seen_id) <= 128 and response.headers.get_list("x-request-id") == [seen_id]
