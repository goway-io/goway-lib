import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from goway_lib.db import Base
from goway_lib.memberships import MembershipReplicaRepository, apply_event, sync

USER, CARRIER_ORG, SHIPPER_ORG = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
MEMBERSHIP = uuid.uuid7()


@pytest.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def state(at: str, *, side="carrier", org=CARRIER_ORG, membership=MEMBERSHIP) -> dict:
    return {
        "membership_id": str(membership),
        "user_id": str(USER),
        "org_id": str(org),
        "side": side,
        "role": side,
        "org_role": "owner",
        "created_at": at,
        "updated_at": at,
    }


async def test_events_keep_the_newest_state_and_a_removed_side_names_no_organization(maker):
    async with maker() as db:
        replicas = MembershipReplicaRepository(db)
        await apply_event(db, "membership.created", state("2026-10-22T09:00:00+00:00"))
        assert await replicas.org_of(USER, "carrier") == CARRIER_ORG
        assert await replicas.members_of(CARRIER_ORG) == [USER]

        await apply_event(db, "membership.updated", state("2026-10-22T08:00:00+00:00", side="shipper"))
        assert await replicas.org_of(USER, "carrier") == CARRIER_ORG, "an older state is dropped"

        removed = {k: v for k, v in state("").items() if k not in ("role", "org_role", "created_at", "updated_at")}
        await apply_event(db, "membership.removed", {**removed, "removed_at": "2026-10-22T10:00:00+00:00"})
        assert await replicas.org_of(USER, "carrier") is None and await replicas.members_of(CARRIER_ORG) == []

        await apply_event(db, "membership.created", state("2026-10-22T11:00:00+00:00"))
        assert await replicas.org_of(USER, "carrier") == CARRIER_ORG, "a restore brings the same membership back"


async def test_a_removal_that_arrives_first_leaves_a_tombstone(maker):
    async with maker() as db:
        payload = {k: v for k, v in state("").items() if k not in ("role", "org_role", "created_at", "updated_at")}
        await apply_event(db, "membership.removed", {**payload, "removed_at": "2026-10-22T10:00:00+00:00"})
        await apply_event(db, "membership.created", state("2026-10-22T09:00:00+00:00"))
        assert await MembershipReplicaRepository(db).org_of(USER, "carrier") is None


async def test_a_pass_over_auths_list_fills_both_sides_and_marks_ended_ones(maker):
    shipper = uuid.uuid7()
    pages = {
        None: {
            "items": [
                {**state("2026-10-22T09:00:00+00:00"), "removed_at": None},
            ],
            "page": {"next_cursor": "next"},
        },
        "next": {
            "items": [
                {
                    **state("2026-10-22T09:30:00+00:00", side="shipper", org=SHIPPER_ORG, membership=shipper),
                    "removed_at": "2026-10-22T09:30:00+00:00",
                },
            ],
            "page": {"next_cursor": None},
        },
    }

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/projections/memberships"
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://auth") as client:
        assert await sync(maker, client) == 2
        assert await sync(maker, client) == 0, "a second pass changes nothing"
    async with maker() as db:
        replicas = MembershipReplicaRepository(db)
        assert await replicas.org_of(USER, "carrier") == CARRIER_ORG
        assert await replicas.org_of(USER, "shipper") is None


async def test_a_pass_reports_auth_refusing(maker):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403)), base_url="http://auth"
    ) as client:
        assert await sync(maker, client) is None
