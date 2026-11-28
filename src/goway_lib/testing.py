"""Test support shared by the services: tests run against a real Postgres, as production does.

A service's conftest calls `prepare_database(url, metadata)` once per session (a clean `public` schema with every
table), opens a `fresh_engine(url)` per test and disposes it at the end, after `truncate_all(engine, metadata)`
has emptied every table, so each test starts clean without paying for a schema rebuild. `MemoryStorage` stands in
for object storage.

`TEST_DATABASE_URL` points at the server and database to use; without it the default is the local test server that
`make test-db` starts (port 55432), with one database per service so their suites can run side by side.
"""

import asyncio
import os

import asyncpg
from sqlalchemy import MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from goway_lib.storage import ObjectInfo, Storage, StorageClass

LOCAL_SERVER = "postgresql+asyncpg://postgres:postgres@localhost:55432"


def database_url(service: str) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"{LOCAL_SERVER}/goway_test_{service}"


def fresh_engine(url: str, *, pool_size: int | None = None) -> AsyncEngine:
    if pool_size is None:
        return create_async_engine(url, poolclass=NullPool)
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size * 4)


async def _ensure_database(url: str) -> None:
    target = make_url(url)
    server = target.set(drivername="postgresql", database="postgres")
    connection = await asyncpg.connect(server.render_as_string(hide_password=False))
    try:
        exists = await connection.fetchval("select 1 from pg_database where datname = $1", target.database)
        if not exists:
            await connection.execute(f'create database "{target.database}"')
    finally:
        await connection.close()


async def _rebuild_schema(url: str, metadata: MetaData) -> None:
    engine = fresh_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("drop schema if exists public cascade"))
            await connection.execute(text("create schema public"))
            await connection.run_sync(metadata.create_all)
    finally:
        await engine.dispose()


async def prepare_database_async(url: str, metadata: MetaData) -> None:
    await _ensure_database(url)
    await _rebuild_schema(url, metadata)


def prepare_database(url: str, metadata: MetaData) -> None:
    asyncio.run(prepare_database_async(url, metadata))


async def truncate_all(engine: AsyncEngine, metadata: MetaData) -> None:
    tables = ", ".join(f'"{table.name}"' for table in metadata.sorted_tables)
    if not tables:
        return
    async with engine.begin() as connection:
        await connection.execute(text(f"truncate {tables} restart identity cascade"))  # noqa: S608 - names from metadata


class MemoryStorage(Storage):
    def __init__(self, *, down: bool = False, endpoint: str = "http://storage.test", public_endpoint: str = ""):
        super().__init__(
            endpoint=endpoint,
            public_endpoint=public_endpoint,
            access_key="k",
            secret_key="s",  # noqa: S106 - a fake store's key
            buckets={StorageClass.PRIVATE: "p", StorageClass.MEDIA: "m", StorageClass.PUBLIC: "pub"},
        )
        self.objects: dict[tuple[str, str], bytes] = {}
        self.down = down
        self.fail_deletes = False

    def _reach(self) -> None:
        if self.down:
            raise ConnectionError("storage down")

    async def head(self, bucket: str, key: str) -> ObjectInfo | None:
        self._reach()
        body = self.objects.get((bucket, key))
        return None if body is None else ObjectInfo(size=len(body), content_type=None)

    async def read_prefix(self, bucket: str, key: str, length: int = 64) -> bytes:
        self._reach()
        return self.objects[bucket, key][:length]

    async def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        self._reach()
        self.objects[bucket, key] = data

    async def copy(self, source_bucket: str, key: str, target_bucket: str) -> None:
        self._reach()
        self.objects[target_bucket, key] = self.objects[source_bucket, key]

    async def delete(self, bucket: str, key: str) -> None:
        self._reach()
        if self.fail_deletes:
            raise ConnectionError("storage down")
        self.objects.pop((bucket, key), None)
