import asyncio
import os
import signal

from goway_lib.worker import run_worker


def test_a_stop_signal_unwinds_main_and_runs_shutdown():
    seen = []

    async def main() -> None:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    async def shutdown() -> None:
        seen.append("shutdown")

    run_worker(main, shutdown=shutdown)
    assert seen == ["cancelled", "shutdown"]


def test_a_main_that_returns_still_shuts_down():
    seen = []

    async def main() -> None:
        seen.append("ran")

    async def shutdown() -> None:
        seen.append("shutdown")

    run_worker(main, shutdown=shutdown)
    assert seen == ["ran", "shutdown"]
