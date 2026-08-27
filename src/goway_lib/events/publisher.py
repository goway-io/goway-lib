import asyncio
import logging

import nats
from nats.aio.client import Client
from nats.js import JetStreamContext

logger = logging.getLogger(__name__)

_nc: Client | None = None
_js: JetStreamContext | None = None

STREAM = "GOWAY"


CONNECT_TIMEOUT_SECONDS = 5.0


async def connect(nats_url: str, *, subjects: list[str] | None = None) -> None:
    global _nc, _js
    _nc = await asyncio.wait_for(
        nats.connect(nats_url, max_reconnect_attempts=-1, connect_timeout=2), CONNECT_TIMEOUT_SECONDS
    )
    _js = _nc.jetstream()
    default_subjects = ["order.>", "offer.>", "wallet.>", "driver.>", "user.>", "device.>"]
    try:
        await _js.add_stream(name=STREAM, subjects=subjects or default_subjects)
    except Exception:
        logger.debug("stream %s already exists", STREAM)


def jetstream() -> JetStreamContext:
    if _js is None:
        raise RuntimeError("NATS not connected")
    return _js


def ready() -> bool:
    return _nc is not None and _nc.is_connected


async def publish(subject: str, payload: bytes, *, headers: dict[str, str] | None = None) -> None:
    if _js is None or not ready():
        raise RuntimeError("NATS not connected")
    await _js.publish(subject, payload, headers=headers)


async def close() -> None:
    global _nc, _js
    if _nc is not None:
        await _nc.drain()
    _nc = None
    _js = None
