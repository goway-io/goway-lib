import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from goway_lib.db import Base
from goway_lib.events.outbox import _dropped, _released, publish_event


@pytest.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def released():
    seen: list[str] = []

    def record(session):
        seen.extend(row.subject for row in _released(session))

    event.listen(Session, "after_commit", record)
    event.listen(Session, "after_rollback", _dropped)
    yield seen
    event.remove(Session, "after_commit", record)
    event.remove(Session, "after_rollback", _dropped)


async def test_a_savepoint_does_not_let_go_of_the_events_of_the_transaction_around_it(maker, released):
    async with maker() as db:
        publish_event(db, "haul.before", {})
        async with db.begin_nested():
            publish_event(db, "haul.inside", {})
        assert released == [], "the outer transaction can still roll back"
        await db.commit()
    assert released == ["haul.before", "haul.inside"]


async def test_events_staged_in_a_savepoint_that_rolled_back_are_never_let_go(maker, released):
    async with maker() as db:
        with pytest.raises(ValueError):
            async with db.begin_nested():
                publish_event(db, "haul.lost", {})
                raise ValueError("refused")
        await db.commit()
    assert "haul.lost" not in released
