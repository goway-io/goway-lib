import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from starlette.exceptions import HTTPException as StarletteHTTPException

from goway_lib.errors import Problem, UnprocessableEntity

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


def _respond(request: Request, problem: Problem) -> JSONResponse:
    body = problem.to_dict(instance=str(request.url.path), trace_id=_trace_id())
    return JSONResponse(status_code=problem.status, content=body, media_type=PROBLEM_MEDIA)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def handle_problem(request: Request, exc: Problem) -> JSONResponse:
        return _respond(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(loc) for loc in err.get("loc", [])), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return _respond(request, UnprocessableEntity(errors=errors))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, title = _STATUS_PROBLEMS.get(exc.status_code, ("HTTP_ERROR", str(exc.detail)))
        problem = Problem(str(exc.detail), code=code)
        problem.status = exc.status_code
        problem.title = title
        return _respond(request, problem)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return _respond(request, Problem())
