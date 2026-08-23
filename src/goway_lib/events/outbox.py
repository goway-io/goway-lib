import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from opentelemetry import propagate
from sqlalchemy import JSON, Index, Integer, String, Text, delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, Session, mapped_column

from goway_lib.db import BaseModel
from goway_lib.events import publisher
from goway_lib.timeutils import now_utc

logger = logging.getLogger(__name__)

_STAGED_KEY = "_goway_outbox_staged"
_INFLIGHT: set[asyncio.Task] = set()

RELAY_GRACE_SECONDS = 30
RELAY_BATCH = 200
RELAY_MAX_ATTEMPTS = 10
RELAY_INTERVAL_SECONDS = 5.0
CLEANUP_RETENTION_DAYS = 7
PROCESSED_RETENTION_DAYS = 30
CLEANUP_EVERY_TICKS = 720


class OutboxEvent(BaseModel):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_sweep", "status", "created_at"),)

    subject: Mapped[str] = mapped_column(String(120), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class ProcessedEvent(BaseModel):
    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    subject: Mapped[str] = mapped_column(String(120))


def publish_event(db: AsyncSession, subject: str, payload: dict) -> OutboxEvent:
    row = OutboxEvent(subject=subject, payload=payload, status="pending")
    db.add(row)
    db.sync_session.info.setdefault(_STAGED_KEY, []).append(row)
    return row


async def _deliver(subject: str, payload: dict, event_id: uuid.UUID) -> None:
    message = dict(payload)
    message.setdefault("event_id", str(event_id))
    headers: dict[str, str] = {}
    propagate.inject(headers)
    await publisher.publish(subject, json.dumps(message, default=str).encode(), headers=headers or None)


async def _mark_published(session_factory: async_sessionmaker, event_id: uuid.UUID) -> None:
    async with session_factory() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(status="published", published_at=now_utc(), attempts=OutboxEvent.attempts + 1)
        )
        await session.commit()


async def _publish_one(session_factory: async_sessionmaker, event_id: uuid.UUID, subject: str, payload: dict) -> None:
    try:
        await _deliver(subject, payload, event_id)
    except Exception as exc:
        logger.warning("outbox fast-path publish failed (relay will retry): %s %s", subject, exc)
        return
    try:
        await asyncio.shield(_mark_published(session_factory, event_id))
    except asyncio.CancelledError:
        logger.warning("outbox mark-published cancelled for %s (relay will republish)", event_id)
        raise
    except Exception:
        logger.exception("outbox mark-published failed for %s (relay will republish)", event_id)


def register_outbox_listeners(session_factory: async_sessionmaker) -> None:
    """Session events fire on the SYNC Session class that AsyncSession proxies —
    binding them to AsyncSession raises InvalidRequestError at boot."""

    @event.listens_for(Session, "after_commit")
    def _fast_path(session) -> None:
        staged = session.info.pop(_STAGED_KEY, [])
        if not staged:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for row in staged:
            task = loop.create_task(_publish_one(session_factory, row.id, row.subject, row.payload))
            _INFLIGHT.add(task)
            task.add_done_callback(_INFLIGHT.discard)

    @event.listens_for(Session, "after_rollback")
    def _drop(session) -> None:
        session.info.pop(_STAGED_KEY, None)


async def relay_once(session_factory: async_sessionmaker) -> int:
    if not publisher.ready():
        return 0
    cutoff = now_utc() - timedelta(seconds=RELAY_GRACE_SECONDS)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.status == "pending", OutboxEvent.created_at < cutoff)
                    .order_by(OutboxEvent.created_at)
                    .limit(RELAY_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        published = 0
        for row in rows:
            try:
                await _deliver(row.subject, row.payload, row.id)
            except Exception as exc:
                row.attempts += 1
                row.last_error = str(exc)[:500]
                if row.attempts >= RELAY_MAX_ATTEMPTS:
                    row.status = "dead"
                    logger.error("outbox row %s moved to dead after %d attempts", row.id, row.attempts)
                continue
            row.status = "published"
            row.published_at = now_utc()
            row.attempts += 1
            published += 1
        await session.commit()
        return published


async def cleanup_published(session_factory: async_sessionmaker) -> int:
    published_cutoff = now_utc() - timedelta(days=CLEANUP_RETENTION_DAYS)
    ledger_cutoff = now_utc() - timedelta(days=PROCESSED_RETENTION_DAYS)
    async with session_factory() as session:
        result = await session.execute(
            delete(OutboxEvent).where(OutboxEvent.status == "published", OutboxEvent.published_at < published_cutoff)
        )
        await session.execute(delete(ProcessedEvent).where(ProcessedEvent.created_at < ledger_cutoff))
        await session.commit()
        return result.rowcount or 0


async def run_outbox_relay(session_factory: async_sessionmaker, *, stop: asyncio.Event | None = None) -> None:
    logger.info("outbox relay started")
    tick = 0
    while stop is None or not stop.is_set():
        try:
            await relay_once(session_factory)
            if tick % CLEANUP_EVERY_TICKS == 0:
                await cleanup_published(session_factory)
        except Exception:
            logger.exception("outbox relay tick failed")
        tick += 1
        try:
            if stop is not None:
                await asyncio.wait_for(stop.wait(), timeout=RELAY_INTERVAL_SECONDS)
            else:
                await asyncio.sleep(RELAY_INTERVAL_SECONDS)
        except TimeoutError:
            pass
