import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, event, func
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from goway_lib.errors import FieldTooLong


def make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


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
