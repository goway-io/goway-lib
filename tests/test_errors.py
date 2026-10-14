from goway_lib.errors import Conflict, NotFound, or_404, problem_responses


def test_problem_shape():
    problem = Conflict("Balance too low", key="wallet.below_floor", params={"minimum": 200000})
    body = problem.to_dict(instance="/v1/offers/x/accept", trace_id="abc")
    assert body["status"] == 409
    assert body["code"] == "CONFLICT"
    assert body["type"].endswith("/conflict")
    assert body["key"] == "wallet.below_floor"
    assert body["params"] == {"minimum": 200000}
    assert body["instance"] == "/v1/offers/x/accept"
    assert body["trace_id"] == "abc"


def test_or_404():
    assert or_404("value", "order") == "value"
    try:
        or_404(None, "order")
        raise AssertionError("should have raised")
    except NotFound as exc:
        assert exc.params == {"resource": "order"}


def test_problem_responses_openapi():
    responses = problem_responses(400, 404)
    assert set(responses) == {400, 404}
    assert "application/problem+json" in responses[404]["content"]


def test_every_published_subject_is_stream_covered():
    from goway_lib.events.publisher import DEFAULT_SUBJECTS

    published = [
        "order.created",
        "offer.accepted",
        "wallet.balance_changed",
        "driver.presence_changed",
        "user.created",
        "user.updated",
        "user.session_revoked",
        "device.upserted",
        "vehicle.upserted",
        "carrier.verification_changed",
        "region.updated",
        "load.posted",
        "booking.created",
    ]
    prefixes = tuple(s[:-1] for s in DEFAULT_SUBJECTS if s.endswith(">"))
    uncovered = [s for s in published if not s.startswith(prefixes)]
    assert uncovered == []


def test_validation_errors_documented_as_problems():
    from fastapi import FastAPI
    from pydantic import BaseModel

    from goway_lib.handlers import install_handlers

    class Body(BaseModel):
        name: str

    app = FastAPI()
    install_handlers(app)

    @app.post("/things")
    async def create(body: Body) -> Body:
        return body

    schema = app.openapi()
    assert set(schema["paths"]["/things"]["post"]["responses"]["422"]["content"]) == {"application/problem+json"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert app.openapi() is schema
