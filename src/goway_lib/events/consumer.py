import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from goway_lib import metrics
from goway_lib.events import publisher
from goway_lib.events.outbox import ProcessedEvent

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("goway.events")

MAX_DELIVER = 5
NAK_DELAY_SECONDS = 5
PENDING_POLL_SECONDS = 30

Handler = Callable[[str, dict], Awaitable[None]]

_CONSUMER_TASKS: set[asyncio.Task] = set()


async def already_processed(db: AsyncSession, event_id: uuid.UUID) -> bool:
    return (await db.execute(select(ProcessedEvent.id).where(ProcessedEvent.event_id == event_id))).first() is not None


def mark_processed(db: AsyncSession, event_id: uuid.UUID, subject: str) -> None:
    db.add(ProcessedEvent(event_id=event_id, subject=subject))


async def run_consumer(*, durable: str, subjects: list[str], handler: Handler) -> None:
    js = publisher.jetstream()
    dlq_subject = f"dlq.{durable}"

    for subject in subjects:
        name = f"{durable}-{subject.replace('.', '-').replace('>', 'all').replace('*', 'any')}"
        sub = await js.subscribe(
            subject,
            durable=name,
            stream=publisher.STREAM,
            manual_ack=True,
            config=ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT,
                max_deliver=MAX_DELIVER,
                deliver_policy=DeliverPolicy.ALL,
                ack_wait=30,
            ),
        )

        async def _consume(sub=sub) -> None:
            async for msg in sub.messages:
                labels = {"durable": durable, "subject": msg.subject}
                delivered = msg.metadata.num_delivered if msg.metadata else 1
                if msg.metadata and msg.metadata.timestamp and delivered == 1:
                    waited = (datetime.now(UTC) - msg.metadata.timestamp).total_seconds()
                    metrics.consumer_lag.record(max(waited, 0.0), labels)
                started = time.perf_counter()
                span = tracer.start_span(
                    f"consume {msg.subject}",
                    context=propagate.extract(dict(msg.headers or {})),
                    kind=SpanKind.CONSUMER,
                    attributes={"messaging.system": "nats", "messaging.destination.name": msg.subject} | labels,
                )
                with trace.use_span(span, end_on_exit=True):
                    try:
                        payload = json.loads(msg.data.decode())
                        await handler(msg.subject, payload)
                        await msg.ack()
                        metrics.consumer_processed.add(1, labels | {"outcome": "ok"})
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR))
                        logger.exception("handler failed for %s", msg.subject)
                        if delivered >= MAX_DELIVER:
                            try:
                                await publisher.publish(dlq_subject, msg.data)
                                logger.error("message sent to %s after %d deliveries", dlq_subject, delivered)
                            except Exception:
                                logger.exception("DLQ publish failed for %s", msg.subject)
                            await msg.term()
                            metrics.consumer_processed.add(1, labels | {"outcome": "dead_lettered"})
                        else:
                            await msg.nak(delay=NAK_DELAY_SECONDS)
                            metrics.consumer_processed.add(1, labels | {"outcome": "retry"})
                    finally:
                        metrics.consumer_duration.record(time.perf_counter() - started, labels)

        async def _watch_pending(sub=sub, name=name) -> None:
            """How far this durable is behind, as JetStream counts it; a failed read keeps the last value."""
            while True:
                try:
                    metrics.CONSUMER_PENDING[name] = (await sub.consumer_info()).num_pending
                except Exception as exc:
                    logger.debug("consumer info for %s unavailable: %s", name, exc)
                await asyncio.sleep(PENDING_POLL_SECONDS)

        for job in (_consume(), _watch_pending()):
            task = asyncio.create_task(job)
            _CONSUMER_TASKS.add(task)
            task.add_done_callback(_CONSUMER_TASKS.discard)
    logger.info("consumer %s bound to %s", durable, subjects)
