import pytest
from sqlalchemy.pool import StaticPool

from goway_lib import db
from goway_lib.db import engine_from_settings, make_engine
from goway_lib.settings import ServiceSettings


def settings(**overrides) -> ServiceSettings:
    base = {"SERVICE_NAME": "goway-x", "DATABASE_URL": "postgresql+asyncpg://u:p@db/x"}
    return ServiceSettings(**{**base, **overrides})


@pytest.fixture
def engine_options(monkeypatch):
    """What the engine is built with, without opening a database."""
    seen = {}

    def fake(url, **options):
        seen["url"] = url
        seen.update(options)
        return "engine"

    monkeypatch.setattr(db, "create_async_engine", fake)
    return seen


@pytest.mark.parametrize(("role", "size", "overflow"), [("api", 4, 4), ("worker", 2, 2)])
def test_each_process_role_gets_its_share_of_connections(engine_options, role, size, overflow):
    engine_from_settings(settings(), role=role)
    assert (engine_options["pool_size"], engine_options["max_overflow"]) == (size, overflow)
    assert engine_options["pool_use_lifo"] and engine_options["pool_pre_ping"]
    assert (engine_options["pool_timeout"], engine_options["pool_recycle"]) == (10, 1800)


def test_the_budget_comes_from_the_environment(engine_options):
    engine_from_settings(settings(DB_POOL_SIZE=8, DB_MAX_OVERFLOW=2, DB_POOL_TIMEOUT_SECONDS=3), role="api")
    assert (engine_options["pool_size"], engine_options["max_overflow"], engine_options["pool_timeout"]) == (8, 2, 3)


def test_the_connection_names_its_process_to_postgres(engine_options):
    engine_from_settings(settings(), role="worker")
    assert engine_options["connect_args"] == {"server_settings": {"application_name": "goway-x-worker"}, "timeout": 10}


def test_a_real_postgres_engine_carries_the_budget():
    engine = engine_from_settings(settings(), role="api")
    assert engine.pool.size() == 4 and engine.dialect.name == "postgresql"


def test_sqlite_in_tests_takes_no_pool_budget():
    engine = make_engine("sqlite+aiosqlite://", pool_size=99)
    assert isinstance(engine.pool, StaticPool)
