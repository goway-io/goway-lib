import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from goway_lib.db import Base, SoftDeleteModel
from goway_lib.errors import FieldTooLong
from goway_lib.repo import BaseRepository


class Thing(SoftDeleteModel):
    __tablename__ = "things"
    name: Mapped[str] = mapped_column(String(10))


class ThingRepo(BaseRepository[Thing]):
    model = Thing


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def test_soft_delete_hidden_by_default(db):
    repo = ThingRepo(db)
    thing = await repo.create(name="a")
    await repo.soft_delete(thing)

    assert await repo.get(thing.id) is None
    assert (await repo.get(thing.id, include_deleted=True)) is not None

    await repo.restore(thing)
    assert await repo.get(thing.id) is not None


async def test_list_page_cursor(db):
    repo = ThingRepo(db)
    for i in range(5):
        await repo.create(name=f"t{i}")
    page1 = await repo.list_page(limit=2)
    assert len(page1.items) == 2
    assert page1.page.has_more
    page2 = await repo.list_page(cursor=page1.page.next_cursor, limit=2)
    assert len(page2.items) == 2
    ids1 = {t.id for t in page1.items}
    ids2 = {t.id for t in page2.items}
    assert not ids1 & ids2


async def test_string_length_guard(db):
    repo = ThingRepo(db)
    with pytest.raises(FieldTooLong):
        await repo.create(name="x" * 11)
        await db.flush()


def test_constraints_get_deterministic_names():
    fk = next(iter(Thing.metadata.tables["things"].foreign_keys), None)
    assert fk is None
    from sqlalchemy import ForeignKey
    from sqlalchemy.orm import mapped_column

    class Child(SoftDeleteModel):
        __tablename__ = "children"
        thing_id: Mapped[str] = mapped_column(ForeignKey("things.id"))
        name: Mapped[str] = mapped_column(String(10))

    fk_constraint = next(iter(Child.metadata.tables["children"].foreign_key_constraints))
    assert fk_constraint.name == "fk_children_thing_id"
