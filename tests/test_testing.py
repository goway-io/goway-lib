import os

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text

from goway_lib import testing

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_DATABASE_URL (a Postgres to test against)")

metadata = MetaData()
items = Table("items", metadata, Column("id", Integer, primary_key=True), Column("name", String(20)))


def test_the_url_defaults_to_one_local_database_per_service(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert testing.database_url("loads") == "postgresql+asyncpg://postgres:postgres@localhost:55432/goway_test_loads"


@pytest.fixture(scope="module")
def schema():
    testing.prepare_database(URL, metadata)


async def test_tables_start_empty_and_are_emptied_after_use(schema):
    engine = testing.fresh_engine(URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(items.insert().values(name="first"))
            assert (await connection.execute(select(items.c.id))).scalar_one() == 1
        await testing.truncate_all(engine, metadata)
        async with engine.begin() as connection:
            assert (await connection.execute(text("select count(*) from items"))).scalar_one() == 0
            await connection.execute(items.insert().values(name="again"))
            assert (await connection.execute(select(items.c.id))).scalar_one() == 1, "sequences restart"
    finally:
        await engine.dispose()
