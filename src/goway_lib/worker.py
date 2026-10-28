"""Runs a worker process until the orchestrator stops it, and lets it close what it holds on the way out."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def run_worker(main: Callable[[], Awaitable[None]], *, shutdown: Callable[[], Awaitable[None]] | None = None) -> None:
    """`asyncio.run(main())` with a difference: SIGTERM and SIGINT cancel `main` instead of killing the process, so
    its tasks unwind, and `shutdown` then runs to close the NATS connection and the database pool. Without this a
    stopped container leaves its Postgres connections open on the server until a TCP keepalive finds them dead."""

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(main())
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _stop, task, sig)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if shutdown is not None:
                try:
                    await asyncio.wait_for(shutdown(), timeout=5)
                except Exception:
                    logger.exception("worker shutdown failed")

    asyncio.run(runner())


def _stop(task: asyncio.Task, sig: signal.Signals) -> None:
    logger.info("worker stopping on %s", sig.name)
    task.cancel()
