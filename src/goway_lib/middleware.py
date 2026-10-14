import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

Message = dict
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

CANCEL_METHODS = frozenset({"GET", "HEAD"})


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = dict(scope.get("headers", [])).get(b"x-request-id")
        if request_id is None:
            request_id = str(uuid.uuid7()).encode()
            scope["headers"] = [*scope.get("headers", []), (b"x-request-id", request_id)]

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id))
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
                    pass
        finally:
            for task in (pump_task, watch_task):
                if not task.done():
                    task.cancel()
