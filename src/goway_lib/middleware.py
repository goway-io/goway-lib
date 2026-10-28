import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

Message = dict
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

CANCEL_METHODS = frozenset({"GET", "HEAD"})
REQUEST_ID = re.compile(rb"[A-Za-z0-9._:-]{1,128}")


class RequestIdMiddleware:
    """One id per request: the caller's if it looks like an id, otherwise a new one; never two."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        given = next((value for name, value in headers if name == b"x-request-id"), None)
        request_id = given if given is not None and REQUEST_ID.fullmatch(given) else str(uuid.uuid7()).encode()
        scope["headers"] = [*(item for item in headers if item[0] != b"x-request-id"), (b"x-request-id", request_id)]

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [item for item in message.get("headers", []) if item[0].lower() != b"x-request-id"]
                message["headers"] = [*kept, (b"x-request-id", request_id)]
            await send(message)

        await self.app(scope, receive, send_with_id)


class CancelOnDisconnectMiddleware:
    """Cancels handler work when the client drops mid-request; answers 499.

    Mobile clients on flaky networks abandon requests constantly; without this
    the server keeps computing responses nobody will read.
    """

    def __init__(self, app, methods: Iterable[str] = CANCEL_METHODS):
        self.app = app
        self.methods = frozenset(m.upper() for m in methods)

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in self.methods:
            await self.app(scope, receive, send)
            return

        queue: asyncio.Queue[Message] = asyncio.Queue()
        disconnected = asyncio.Event()
        started = False
        completed = False

        async def pump() -> None:
            while True:
                message = await receive()
                await queue.put(message)
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return

        async def buffered_receive() -> Message:
            return await queue.get()

        async def tracking_send(message: Message) -> None:
            nonlocal started, completed
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            await send(message)

        pump_task = asyncio.create_task(pump())
        app_task = asyncio.create_task(self.app(scope, buffered_receive, tracking_send))
        watch_task = asyncio.create_task(disconnected.wait())
        try:
            done, _ = await asyncio.wait({app_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
            if app_task in done:
                app_task.result()
                return
            if completed:
                await app_task
                return
            app_task.cancel()
            try:
                await app_task
            except asyncio.CancelledError:
                pass
            logger.info("request cancelled on disconnect %s %s", scope.get("method"), scope.get("path"))
            if not started:
                try:
                    await send({"type": "http.response.start", "status": 499, "headers": []})
                    await send({"type": "http.response.body", "body": b""})
                except Exception:
                    logger.debug("client gone before the 499 could be sent", exc_info=True)
        finally:
            for task in (pump_task, watch_task):
                if not task.done():
                    task.cancel()


#: A NUL character as it can arrive: raw, as a JSON escape, or percent-encoded in a query or form.
_NUL_FORMS = (b"\x00", b"\\u0000", b"%00")
#: Bodies read whole to look for it: the JSON and form posts the APIs take. Uploads go straight to storage.
_INSPECTED_BODIES = (b"application/json", b"application/x-www-form-urlencoded")


class RejectNulMiddleware:
    """Postgres cannot store the NUL character in text, so a request carrying one used to fail deep inside an insert
    and answer 500. It is refused at the door instead, as a 422 validation problem: in the path, the query string
    or a JSON or form body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if "\x00" in scope.get("path", "") or _has_nul(scope.get("query_string", b"")):
            await _reject(scope, send, "query" if scope.get("query_string") else "path")
            return
        content_type = next((value for name, value in scope.get("headers", []) if name == b"content-type"), b"")
        if not content_type.lower().startswith(_INSPECTED_BODIES):
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, _replay([], message, receive), send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if _has_nul(body):
            await _reject(scope, send, "body")
            return
        await self.app(scope, _replay([body], None, receive), send)


def _has_nul(raw: bytes) -> bool:
    lowered = raw.lower()
    return any(form in lowered for form in _NUL_FORMS)


def _replay(bodies: list[bytes], pending: Message | None, receive: Receive) -> Receive:
    """The body already read, handed to the app once; after that the real channel (for the disconnect)."""
    queue: list[Message] = [{"type": "http.request", "body": body, "more_body": False} for body in bodies]
    if pending is not None:
        queue.append(pending)

    async def replayed() -> Message:
        if queue:
            return queue.pop(0)
        return await receive()

    return replayed


async def _reject(scope: Message, send: Send, where: str) -> None:
    from goway_lib.errors import UnprocessableEntity

    problem = UnprocessableEntity(errors=[{"field": where, "message": "Text must not contain the NUL character."}])
    body = json.dumps(problem.to_dict(instance=scope.get("path"))).encode()
    await send(
        {
            "type": "http.response.start",
            "status": problem.status,
            "headers": [(b"content-type", b"application/problem+json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
