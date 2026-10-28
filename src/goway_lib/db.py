import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, event, func
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from goway_lib.errors import FieldTooLong
from goway_lib.timeutils import now_utc


def make_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 4,
    max_overflow: int = 4,
    pool_timeout: float = 10,
    pool_recycle: int = 1800,
    application_name: str | None = None,
) -> AsyncEngine:
    """One pool per process with a fixed budget. LIFO checkout keeps a quiet process on one or two connections,
    so the rest sit idle long enough for the server's idle timeout to close them; pre-ping and recycling replace a
    connection the server dropped before a query sees it. On Postgres the connection carries `application_name`,
    so `pg_stat_activity` says which process holds which connection."""
    options: dict = {"echo": echo, "pool_pre_ping": True}
    if not database_url.startswith("sqlite"):
        options.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_use_lifo=True,
        )
    if database_url.startswith("postgresql+asyncpg") and application_name:
        options["connect_args"] = {"server_settings": {"application_name": application_name}, "timeout": 10}
    return create_async_engine(database_url, **options)


def engine_from_settings(settings, *, role: str = "api", echo: bool = False) -> AsyncEngine:
    """The process's engine from its settings: an API process gets the larger share, a worker the smaller."""
    worker = role == "worker"
    return make_engine(
        settings.DATABASE_URL,
        echo=echo,
        pool_size=settings.DB_WORKER_POOL_SIZE if worker else settings.DB_POOL_SIZE,
        max_overflow=settings.DB_WORKER_MAX_OVERFLOW if worker else settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        application_name=f"{settings.SERVICE_NAME}-{role}",
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}


class BaseModel(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    created_at: Mapped[datetime] = mapped_column(default=now_utc, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc, server_default=func.now())


class SoftDeleteModel(BaseModel):
    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(default=None, index=True)


class ReferenceModel(BaseModel):
    __abstract__ = True


def _enforce_string_lengths(mapper, connection, target) -> None:
    for col in mapper.columns:
        if not isinstance(col.type, String) or col.type.length is None:
            continue
        value = getattr(target, col.key, None)
        if isinstance(value, str) and len(value) > col.type.length:
            raise FieldTooLong(table=target.__tablename__, column=col.key, max_length=col.type.length)


event.listen(Base, "before_insert", _enforce_string_lengths, propagate=True)
event.listen(Base, "before_update", _enforce_string_lengths, propagate=True)
