import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from starlette.exceptions import HTTPException as StarletteHTTPException

from goway_lib.errors import Problem, UnprocessableEntity, problem_responses

_STATUS_PROBLEMS = {
    404: ("NOT_FOUND", "Not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
}

logger = logging.getLogger(__name__)

PROBLEM_MEDIA = "application/problem+json"


def _trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return None


def problem_response(request: Request, problem: Problem) -> JSONResponse:
    body = problem.to_dict(instance=str(request.url.path), trace_id=_trace_id())
    return JSONResponse(status_code=problem.status, content=body, media_type=PROBLEM_MEDIA)


def _document_validation_problems(app: FastAPI) -> None:
    """FastAPI documents 422 as its own HTTPValidationError body; the handler below sends problem+json."""
    build = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = build()
        validation = problem_responses(422)[422]
        for item in schema.get("paths", {}).values():
            for operation in item.values():
                response = operation.get("responses", {}).get("422") if isinstance(operation, dict) else None
                if response and PROBLEM_MEDIA not in response.get("content", {}):
                    response.update(validation)
        components = schema.get("components", {}).get("schemas", {})
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        return schema

    app.openapi = openapi


def install_handlers(app: FastAPI) -> None:
    _document_validation_problems(app)

    @app.exception_handler(Problem)
    async def handle_problem(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(loc) for loc in err.get("loc", [])), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return problem_response(request, UnprocessableEntity(errors=errors))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, title = _STATUS_PROBLEMS.get(exc.status_code, ("HTTP_ERROR", str(exc.detail)))
        problem = Problem(str(exc.detail), code=code)
        problem.status = exc.status_code
        problem.title = title
        return problem_response(request, problem)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return problem_response(request, Problem())
