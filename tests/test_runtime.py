import time
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from goway_lib import auth
from goway_lib.db import Base
from goway_lib.errors import BadRequest
from goway_lib.events.outbox import OutboxEvent
from goway_lib.handlers import install_handlers
from goway_lib.pagination import PageParams
from goway_lib.runtime import RuntimeSchema, RuntimeSetting, RuntimeStore, runtime_settings_router


class Tunables(RuntimeSchema):
    loader_amount: int = Field(default=60_000, ge=0, le=1_000_000, description="Price per loader.")
    free_minutes: int = Field(default=15, ge=0, le=180)


@pytest.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_defaults_overrides_reset_and_history(maker):
    store = RuntimeStore(Tunables, service="loads", defaults={"loader_amount": 70_000}, ttl=0)
    async with maker() as db:
        assert (await store.current(db)).loader_amount == 70_000
        updated = await store.update(db, {"free_minutes": 20}, actor_id=uuid.uuid7())
        assert (updated.loader_amount, updated.free_minutes) == (70_000, 20)
        await db.commit()

        described = await store.describe(db)
        by_key = {f.key: f for f in described.fields}
        assert by_key["free_minutes"].overridden and not by_key["loader_amount"].overridden
        assert by_key["loader_amount"].maximum == 1_000_000

        with pytest.raises(BadRequest):
            await store.update(db, {"free_minutes": 999}, actor_id=None)
        with pytest.raises(BadRequest):
            await store.update(db, {}, actor_id=None)

        after_reset = await store.reset(db, "free_minutes", actor_id=None)
        assert after_reset.free_minutes == 15
        history = (await store.history(db, PageParams(), transformer=None)).items
        assert [(h.key, h.old_value, h.new_value) for h in history] == [
            ("free_minutes", 20, None),
            ("free_minutes", None, 20),
        ]
        events = (await db.execute(select(OutboxEvent))).scalars().all()
        assert {e.subject for e in events} == {"settings.changed"}


async def test_cache_serves_within_ttl(maker):
    store = RuntimeStore(Tunables, service="loads", ttl=60)
    async with maker() as db:
        assert (await store.current(db)).free_minutes == 15
        await store.update(db, {"free_minutes": 30}, actor_id=None)
        assert (await store.current(db)).free_minutes == 30
        store._cached = None
        assert (await store.current(db)).free_minutes == 30


async def test_a_read_racing_an_uncommitted_change_does_not_keep_the_old_values(maker):
    store = RuntimeStore(Tunables, service="loads", ttl=60)
    async with maker() as db:
        await store.update(db, {"free_minutes": 40}, actor_id=None)
        store._cached, store._cached_at = Tunables(), time.monotonic()
        await db.commit()
        assert (await store.current(db)).free_minutes == 40


async def test_a_read_that_spans_an_invalidation_is_not_cached(maker, monkeypatch):
    store = RuntimeStore(Tunables, service="loads", ttl=60)
    read = store._overrides

    async def overtaken(db):
        rows = await read(db)
        store.invalidate()
        return rows

    async with maker() as db:
        monkeypatch.setattr(store, "_overrides", overtaken)
        await store.current(db)
        assert store._cached is None


async def test_admin_router_validates_bounds_and_role(maker):
    auth.configure_auth(jwt_secret="j" * 32)
    store = RuntimeStore(Tunables, service="loads", ttl=0)
    app = FastAPI()
    install_handlers(app)

    async def get_db():
        async with maker() as db:
            yield db
            await db.commit()

    app.include_router(runtime_settings_router(store, db_dependency=get_db))

    def token(role):
        return auth.mint_access_token(user_id=uuid.uuid7(), org_id=uuid.uuid7(), role=role, role_id=uuid.uuid7(), rv=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        admin = {"Authorization": f"Bearer {token('superadmin')}"}
        assert (
            await client.get("/admin/settings", headers={"Authorization": f"Bearer {token('shipper')}"})
        ).status_code == 403
        r = await client.patch("/admin/settings", json={"free_minutes": 999}, headers=admin)
        assert r.status_code == 422
        r = await client.patch("/admin/settings", json={"unknown": 1}, headers=admin)
        assert r.status_code == 422
        r = await client.patch("/admin/settings", json={"free_minutes": 25}, headers=admin)
        assert r.status_code == 200 and r.json()["values"]["free_minutes"] == 25
        r = await client.delete("/admin/settings/free_minutes", headers=admin)
        assert r.json()["values"]["free_minutes"] == 15
        listed = (await client.get("/admin/settings/history", headers=admin)).json()
        assert len(listed["items"]) == 2 and listed["page"]["total"] == 2 and listed["page"]["pages"] == 1


async def test_bad_stored_override_is_dropped_alone(maker):
    store = RuntimeStore(Tunables, service="loads", ttl=0)
    async with maker() as db:
        await store.update(db, {"loader_amount": 90_000}, actor_id=None)
        db.add(RuntimeSetting(key="free_minutes", value=99_999))
        await db.flush()
        current = await store.current(db)
        assert (current.loader_amount, current.free_minutes) == (90_000, 15)


async def test_repeated_update_upserts(maker):
    store = RuntimeStore(Tunables, service="loads", ttl=0)
    async with maker() as db:
        await store.update(db, {"loader_amount": 1}, actor_id=None)
        await store.update(db, {"loader_amount": 2}, actor_id=None)
        rows = (await db.execute(select(RuntimeSetting))).scalars().all()
        assert [(r.key, r.value) for r in rows] == [("loader_amount", 2)]
