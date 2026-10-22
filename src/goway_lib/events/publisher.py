import asyncio
import logging

import nats
from nats.aio.client import Client
from nats.js import JetStreamContext

logger = logging.getLogger(__name__)

_nc: Client | None = None
_js: JetStreamContext | None = None

STREAM = "GOWAY"

#: How long the stream keeps an event. Consumers ack within seconds; the window is for a worker that is down, and a
#: week covers a long weekend with room to spare. goway-lib-rs keeps the same value, or the two would fight over it.
STREAM_MAX_AGE_SECONDS = 7 * 86_400

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
    "settings.>",
    "notification.>",
    "rating.>",
    "support.>",
    "org.>",
    "membership.>",
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
        if merged != sorted(current) or info.config.max_age != STREAM_MAX_AGE_SECONDS:
            info.config.subjects = merged
            info.config.max_age = STREAM_MAX_AGE_SECONDS
            await _js.update_stream(info.config)
            logger.info("stream %s set to %s, kept %ss", STREAM, merged, STREAM_MAX_AGE_SECONDS)
    except Exception:
        try:
            await _js.add_stream(name=STREAM, subjects=desired, max_age=STREAM_MAX_AGE_SECONDS)
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
