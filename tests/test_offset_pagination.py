import pytest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from goway_lib.db import Base, BaseModel
from goway_lib.pagination import paginate_offset


class Row(BaseModel):
    __tablename__ = "offset_rows"
    label: Mapped[str] = mapped_column(String(20))


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        for n in range(7):
            session.add(Row(label=f"row-{n}"))
        await session.flush()
        yield session
    await engine.dispose()


async def test_a_page_knows_how_many_there_are(db):
    page = await paginate_offset(db, select(Row), number=1, size=3, order_by=Row.label)
    assert [row.label for row in page.items] == ["row-0", "row-1", "row-2"]
    assert (page.page.number, page.page.size, page.page.total, page.page.pages) == (1, 3, 7, 3)


async def test_the_last_page_is_short_not_empty(db):
    page = await paginate_offset(db, select(Row), number=3, size=3, order_by=Row.label)
    assert [row.label for row in page.items] == ["row-6"]
    assert page.page.pages == 3


async def test_asking_past_the_end_gives_nothing_but_still_reports_the_total(db):
    page = await paginate_offset(db, select(Row), number=9, size=3, order_by=Row.label)
    assert page.items == []
    assert page.page.total == 7


async def test_a_filter_narrows_the_total_too(db):
    page = await paginate_offset(db, select(Row).where(Row.label == "row-4"), order_by=Row.label)
    assert page.page.total == 1 and page.page.pages == 1


async def test_an_empty_table_reports_no_pages(db):
    page = await paginate_offset(db, select(Row).where(Row.label == "nothing"))
    assert (page.items, page.page.total, page.page.pages) == ([], 0, 0)


@pytest.mark.parametrize("asked,used", [(0, 1), (-5, 1), (1, 1)])
async def test_the_page_number_never_goes_below_one(db, asked, used):
    page = await paginate_offset(db, select(Row), number=asked, size=2, order_by=Row.label)
    assert page.page.number == used


async def test_the_page_size_is_capped(db):
    page = await paginate_offset(db, select(Row), size=5000, order_by=Row.label)
    assert page.page.size == 100


async def test_a_transformer_shapes_the_rows(db):
    page = await paginate_offset(db, select(Row), size=2, order_by=Row.label, transformer=lambda row: row.label)
    assert page.items == ["row-0", "row-1"]


async def test_a_whole_list_wraps_as_a_single_page():
    from goway_lib.pagination import OffsetPage

    page = OffsetPage.of(["a", "b"])
    assert (page.page.total, page.page.pages, page.page.number) == (2, 1, 1)
    assert OffsetPage.of([]).page.pages == 0


async def test_rows_tied_on_the_sort_column_keep_a_stable_place(db):
    from goway_lib.pagination import PageParams
    from goway_lib.repo import BaseRepository

    class Rows(BaseRepository[Row]):
        model = Row

    repo = Rows(db)
    seen = []
    for number in (1, 2, 3, 4):
        page = await repo.list_offset_page(pages=PageParams(number=number, size=2), order_by=Row.created_at.asc())
        seen += [row.label for row in page.items]
    assert sorted(seen) == sorted({f"row-{n}" for n in range(7)})
