from typing import Any

PROBLEM_TYPE_BASE = "https://api.goway.uz/problems/"


class Problem(Exception):
    status: int = 500
    code: str = "INTERNAL"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        key: str | None = None,
        params: dict[str, Any] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        if code is not None:
            self.code = code
        self.detail = detail
        self.key = key
        self.params = params or {}
        self.errors = errors or []

    def to_dict(self, instance: str | None = None, trace_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": PROBLEM_TYPE_BASE + self.code.lower().replace("_", "-"),
            "title": self.title,
            "status": self.status,
            "code": self.code,
        }
        if self.detail:
            body["detail"] = self.detail
        if instance:
            body["instance"] = instance
        if self.key:
            body["key"] = self.key
        if self.params:
            body["params"] = self.params
        if trace_id:
            body["trace_id"] = trace_id
        if self.errors:
            body["errors"] = self.errors
        return body


class BadRequest(Problem):
    status = 400
    code = "BAD_REQUEST"
    title = "Bad request"


class Unauthorized(Problem):
    status = 401
    code = "UNAUTHORIZED"
    title = "Missing or invalid credentials"


class Forbidden(Problem):
    status = 403
    code = "FORBIDDEN"
    title = "Insufficient permissions"


class NotFound(Problem):
    status = 404
    code = "NOT_FOUND"
    title = "Not found"


class Conflict(Problem):
    status = 409
    code = "CONFLICT"
    title = "Conflict"


class UnprocessableEntity(Problem):
    status = 422
    code = "VALIDATION_FAILED"
    title = "Request validation failed"


class TooManyRequests(Problem):
    status = 429
    code = "TOO_MANY_REQUESTS"
    title = "Too many requests"


class UpstreamUnavailable(Problem):
    status = 502
    code = "UPSTREAM_UNAVAILABLE"
    title = "Upstream service unavailable"


class FieldTooLong(BadRequest):
    code = "FIELD_TOO_LONG"
    title = "Field exceeds maximum length"

    def __init__(self, *, table: str, column: str, max_length: int) -> None:
        super().__init__(
            f"{column} exceeds {max_length} characters",
            key="common.field_too_long",
            params={"field": column, "max_length": max_length},
        )
        self.table = table


def or_404[T](value: T | None, resource: str) -> T:
    if value is None:
        raise NotFound(f"{resource} not found", key="common.not_found", params={"resource": resource})
    return value


_PROBLEM_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "code": {"type": "string"},
        "key": {"type": "string"},
        "params": {"type": "object"},
        "trace_id": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["type", "title", "status", "code"],
}

_STATUS_TITLES = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    422: "Validation failed",
    429: "Too many requests",
    502: "Upstream unavailable",
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {
        status: {
            "description": _STATUS_TITLES.get(status, "Error"),
            "content": {"application/problem+json": {"schema": _PROBLEM_SCHEMA}},
        }
        for status in statuses
    }
