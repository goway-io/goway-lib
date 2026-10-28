"""A replica of auth's memberships: which organization a person works in on each side of the market.

Every service that routes by organization keeps one, as its own `membership_replicas` table: `membership.*` events
keep it current, and a pass over auth's `/internal/projections/memberships` fills it on a cold start and heals what
events missed. A person holds at most one live membership per side, so `(user_id, side)` names one organization.
"""

import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import Boolean, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from goway_lib.db import BaseModel
from goway_lib.repo import BaseRepository

logger = logging.getLogger(__name__)

SUBJECTS = ["membership.created", "membership.updated", "membership.removed"]
PROJECTION_PATH = "/internal/projections/memberships"
PAGE_SIZE = 100
FETCH_ATTEMPTS = 10


class MembershipReplica(BaseModel):
    __tablename__ = "membership_replicas"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(index=True)
    side: Mapped[str] = mapped_column(String(10))
    role: Mapped[str] = mapped_column(String(30))
    org_role: Mapped[str] = mapped_column(String(10), default="owner")
    removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source_updated_at: Mapped[datetime | None] = mapped_column(default=None)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _stamp(value: str | None) -> datetime | None:
    return _aware(datetime.fromisoformat(value)) if value else None


class MembershipReplicaRepository(BaseRepository[MembershipReplica]):
    model = MembershipReplica

    async def apply(self, membership_id: uuid.UUID, fields: dict, source_updated_at: datetime | None) -> bool:
        """Keeps the newest state by the source's time; an older or equal one is dropped. True when it applied."""
        row = await self.get(membership_id)
        if row is None:
            await self.create(id=membership_id, source_updated_at=source_updated_at, **fields)
            return True
        current, incoming = _aware(row.source_updated_at), _aware(source_updated_at)
        if current and incoming and incoming <= current:
            return False
        await self.update(row, source_updated_at=source_updated_at, **fields)
        return True

    async def org_of(self, user_id: uuid.UUID, side: str) -> uuid.UUID | None:
        """The organization the person works in on that side, if they hold it."""
        q = select(MembershipReplica.org_id).where(
            MembershipReplica.user_id == user_id, MembershipReplica.side == side, MembershipReplica.removed.is_(False)
        )
        return (await self.db.execute(q)).scalar_one_or_none()

    async def members_of(self, org_id: uuid.UUID) -> list[uuid.UUID]:
        """The people working in the organization."""
        q = select(MembershipReplica.user_id).where(
            MembershipReplica.org_id == org_id, MembershipReplica.removed.is_(False)
        )
        return list((await self.db.execute(q)).scalars().all())


async def apply_event(db: AsyncSession, subject: str, payload: dict) -> None:
    """One `membership.*` event into the replica; the caller dedupes on `event_id` and commits."""
    replicas = MembershipReplicaRepository(db)
    membership_id = uuid.UUID(payload["membership_id"])
    if subject == "membership.removed":
        fields = {
            "user_id": uuid.UUID(payload["user_id"]),
            "org_id": uuid.UUID(payload["org_id"]),
            "side": payload["side"],
            "removed": True,
        }
        row = await replicas.get(membership_id)
        if row is None:
            fields |= {"role": "", "org_role": "owner"}
        await replicas.apply(membership_id, fields, _stamp(payload.get("removed_at")))
        return
    stamp = payload.get("updated_at") if subject == "membership.updated" else payload.get("created_at")
    await replicas.apply(membership_id, _state(payload, removed=False), _stamp(stamp))


def _state(item: dict, *, removed: bool) -> dict:
    return {
        "user_id": uuid.UUID(item["user_id"]),
        "org_id": uuid.UUID(item["org_id"]),
        "side": item["side"],
        "role": item["role"],
        "org_role": item.get("org_role") or "owner",
        "removed": removed,
    }


async def fill_page(session_factory: async_sessionmaker, items: list[dict]) -> int:
    """One page of auth's list in one transaction; returns how many rows it changed."""
    changed = 0
    async with session_factory() as db:
        replicas = MembershipReplicaRepository(db)
        for item in items:
            fields = _state(item, removed=item.get("removed_at") is not None)
            changed += await replicas.apply(uuid.UUID(item["membership_id"]), fields, _stamp(item.get("updated_at")))
        await db.commit()
    return changed


async def _fetch(client: httpx.AsyncClient, cursor: str | None) -> dict:
    params = {"limit": PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
    attempt = 0
    while True:
        try:
            response = await client.get(PROJECTION_PATH, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            attempt += 1
            failing = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500
            if not failing or attempt == FETCH_ATTEMPTS:
                raise
            # Jitter for the retry delay, not a secret.
            await asyncio.sleep(random.uniform(0, min(300.0, 2.0 * 2 ** (attempt - 1))))  # noqa: S311


async def sync(session_factory: async_sessionmaker, client: httpx.AsyncClient) -> int | None:
    """One full pass over auth's memberships; None when auth refused or stayed down, which is logged at ERROR."""
    changed, cursor = 0, None
    try:
        while True:
            page = await _fetch(client, cursor)
            changed += await fill_page(session_factory, page["items"])
            cursor = page.get("page", {}).get("next_cursor")
            if not cursor:
                break
    except httpx.HTTPError as exc:
        logger.error("membership list from auth failed: %s", exc)
        return None
    logger.info("membership replica in step with auth: %d changed", changed)
    return changed
