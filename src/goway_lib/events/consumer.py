import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from goway_lib.events import publisher
from goway_lib.events.outbox import ProcessedEvent

logger = logging.getLogger(__name__)

MAX_DELIVER = 5
NAK_DELAY_SECONDS = 5

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
        sub = await js.subscribe(
            subject,
            durable=f"{durable}-{subject.replace('.', '-').replace('>', 'all').replace('*', 'any')}",
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
                try:
                    payload = json.loads(msg.data.decode())
                    await handler(msg.subject, payload)
                    await msg.ack()
                except Exception:
                    logger.exception("handler failed for %s", msg.subject)
                    delivered = msg.metadata.num_delivered if msg.metadata else 1
                    if delivered >= MAX_DELIVER:
                        try:
                            await publisher.publish(dlq_subject, msg.data)
                            logger.error("message sent to %s after %d deliveries", dlq_subject, delivered)
                        except Exception:
                            logger.exception("DLQ publish failed for %s", msg.subject)
                        await msg.term()
                    else:
                        await msg.nak(delay=NAK_DELAY_SECONDS)

        task = asyncio.create_task(_consume())
        _CONSUMER_TASKS.add(task)
        task.add_done_callback(_CONSUMER_TASKS.discard)
    logger.info("consumer %s bound to %s", durable, subjects)
