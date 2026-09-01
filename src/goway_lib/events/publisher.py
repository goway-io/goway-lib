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

DEFAULT_SUBJECTS = [
    "order.>",
    "offer.>",
    "wallet.>",
    "driver.>",
    "user.>",
    "device.>",
    "vehicle.>",
    "carrier.>",
    "region.>",
    "load.>",
    "booking.>",
    "dlq.>",
    "location.>",
    "dispatch.>",
]


async def connect(nats_url: str, *, subjects: list[str] | None = None) -> None:
    global _nc, _js
    _nc = await asyncio.wait_for(
        nats.connect(nats_url, max_reconnect_attempts=-1, connect_timeout=2), CONNECT_TIMEOUT_SECONDS
    )
    _js = _nc.jetstream()
    desired = subjects or DEFAULT_SUBJECTS
    try:
        info = await _js.stream_info(STREAM)
        current = list(info.config.subjects or [])
        merged = sorted(set(current) | set(desired))
        if merged != sorted(current):
            info.config.subjects = merged
            await _js.update_stream(info.config)
            logger.info("stream %s subjects extended to %s", STREAM, merged)
    except Exception:
        try:
            await _js.add_stream(name=STREAM, subjects=desired)
        except Exception:
            logger.warning("stream %s neither inspectable nor creatable", STREAM)


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
