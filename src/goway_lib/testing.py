"""Test support shared by the services: tests run against a real Postgres, as production does.

A service's conftest calls `prepare_database(url, metadata)` once per session (a clean `public` schema with every
table), opens a `fresh_engine(url)` per test and disposes it at the end, after `truncate_all(engine, metadata)`
has emptied every table, so each test starts clean without paying for a schema rebuild.

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

LOCAL_SERVER = "postgresql+asyncpg://postgres:postgres@localhost:55432"


def database_url(service: str) -> str:
    """The database this service's tests use: `TEST_DATABASE_URL`, or `goway_test_<service>` on the local server."""
    return os.environ.get("TEST_DATABASE_URL") or f"{LOCAL_SERVER}/goway_test_{service}"


def fresh_engine(url: str, *, pool_size: int | None = None) -> AsyncEngine:
    """An engine for one test. Without `pool_size` it keeps no connections, which is safe when the app under test
    runs on another event loop (schemathesis serves requests from its own thread); with one, connections are reused
    within the test, which is faster because each new SCRAM login costs about a hundred times a query. Dispose it
    before the test's event loop ends."""
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
    """Creates the database if it is missing and rebuilds its schema from the models."""
    await _ensure_database(url)
    await _rebuild_schema(url, metadata)


def prepare_database(url: str, metadata: MetaData) -> None:
    """`prepare_database_async` for a synchronous session-scoped fixture, which runs before any test's loop."""
    asyncio.run(prepare_database_async(url, metadata))


async def truncate_all(engine: AsyncEngine, metadata: MetaData) -> None:
    """Empties every table in one statement and restarts their sequences, so the next test starts clean."""
    tables = ", ".join(f'"{table.name}"' for table in metadata.sorted_tables)
    if not tables:
        return
    async with engine.begin() as connection:
        await connection.execute(text(f"truncate {tables} restart identity cascade"))
