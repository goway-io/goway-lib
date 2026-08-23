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
