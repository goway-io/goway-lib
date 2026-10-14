import logging
import time
import uuid
from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy import JSON, String, event, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from goway_lib.auth import Actor, Requires
from goway_lib.db import Base
from goway_lib.db import BaseModel as DbModel
from goway_lib.errors import BadRequest, problem_responses
from goway_lib.events import publish_event
from goway_lib.pagination import OffsetPage, PageParams, PageQuery, paginate_offset
from goway_lib.timeutils import now_utc

logger = logging.getLogger(__name__)

SUBJECT = "settings.changed"
DEFAULT_TTL_SECONDS = 30.0


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc, server_default=func.now())


class RuntimeSettingHistory(DbModel):
    __tablename__ = "runtime_setting_history"

    key: Mapped[str] = mapped_column(String(64), index=True)
    old_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(default=None)


class RuntimeSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


S = TypeVar("S", bound=RuntimeSchema)


class RuntimeFieldRead(BaseModel):
    key: str
    type: str
    description: str | None
    minimum: float | None = None
    maximum: float | None = None
    default: Any
    value: Any
    overridden: bool = Field(description="True when an admin override is in effect.")


class RuntimeSettingsRead(BaseModel):
    service: str
    values: dict[str, Any]
    fields: list[RuntimeFieldRead]


class RuntimeHistoryRead(BaseModel):
    id: uuid.UUID
    key: str
    old_value: Any
    new_value: Any
    changed_by: uuid.UUID | None
    changed_at: datetime

    @classmethod
    def from_model(cls, row: RuntimeSettingHistory) -> "RuntimeHistoryRead":
        return cls(
            id=row.id,
            key=row.key,
            old_value=row.old_value,
            new_value=row.new_value,
            changed_by=row.changed_by,
            changed_at=row.created_at,
        )


class RuntimeStore(Generic[S]):
    def __init__(
        self, schema: type[S], *, service: str, defaults: dict[str, Any] | None = None, ttl: float = DEFAULT_TTL_SECONDS
    ):
        self.schema = schema
        self.service = service
        self.defaults = schema(**(defaults or {}))
        self.ttl = ttl
        self._cached: S | None = None
        self._cached_at = 0.0
        self._generation = 0

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0
        self._generation += 1

    def _invalidate_on_commit(self, db: AsyncSession) -> None:
        """A request that read the old rows while the change was uncommitted must not keep them for a TTL."""
        self.invalidate()
        event.listen(db.sync_session, "after_commit", lambda _session: self.invalidate(), once=True)

    async def _overrides(self, db: AsyncSession) -> dict[str, Any]:
        rows = (await db.execute(select(RuntimeSetting))).scalars().all()
        known = set(self.schema.model_fields)
        return {row.key: row.value for row in rows if row.key in known}

    def _merge(self, overrides: dict[str, Any]) -> S:
        """Overlay overrides on defaults; a single bad override is dropped, never the whole set."""
        try:
            return self.schema(**{**self.defaults.model_dump(), **overrides})
        except ValueError:
            pass
        accepted = self.defaults.model_dump()
        for key, value in overrides.items():
            try:
                self.schema(**{**accepted, key: value})
            except ValueError:
                logger.warning("runtime setting %s.%s override %r is invalid, using default", self.service, key, value)
                continue
            accepted[key] = value
        return self.schema(**accepted)

    async def current(self, db: AsyncSession) -> S:
        if self._cached is not None and time.monotonic() - self._cached_at < self.ttl:
            return self._cached
        generation = self._generation
        value = self._merge(await self._overrides(db))
        if generation == self._generation:
            self._cached, self._cached_at = value, time.monotonic()
        return value

    async def describe(self, db: AsyncSession) -> RuntimeSettingsRead:
        """Read past the cache: the admin sees what is stored, including their own uncommitted change."""
        overrides = await self._overrides(db)
        current = self._merge(overrides)
        fields = []
        for name, info in self.schema.model_fields.items():
            fields.append(
                RuntimeFieldRead(
                    key=name,
                    type=getattr(info.annotation, "__name__", str(info.annotation)),
                    description=info.description,
                    minimum=next((getattr(m, "ge") for m in info.metadata if hasattr(m, "ge")), None),
                    maximum=next((getattr(m, "le") for m in info.metadata if hasattr(m, "le")), None),
                    default=getattr(self.defaults, name),
                    value=getattr(current, name),
                    overridden=name in overrides,
                )
            )
        return RuntimeSettingsRead(service=self.service, values=current.model_dump(), fields=fields)

    async def update(self, db: AsyncSession, changes: dict[str, Any], *, actor_id: uuid.UUID | None) -> S:
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            raise BadRequest("Nothing to change", code="SETTINGS_EMPTY", key="settings.empty")
        locked = select(RuntimeSetting).where(RuntimeSetting.key.in_(list(changes))).with_for_update()
        rows = {row.key: row.value for row in (await db.execute(locked)).scalars().all()}
        merged = self._merge({**await self._overrides(db), **changes})
        for key in changes:
            new_value = getattr(merged, key)
            if new_value != changes[key]:
                raise BadRequest("Invalid value", code="SETTINGS_INVALID", key="settings.invalid", params={"key": key})
            await db.execute(self._upsert(db, key, new_value, actor_id))
            db.add(RuntimeSettingHistory(key=key, old_value=rows.get(key), new_value=new_value, changed_by=actor_id))
        await db.flush()
        db.expire_all()
        self._announce(db, list(changes), actor_id)
        self._invalidate_on_commit(db)
        return merged

    async def reset(self, db: AsyncSession, key: str, *, actor_id: uuid.UUID | None) -> S:
        if key not in self.schema.model_fields:
            raise BadRequest("Unknown setting", code="SETTINGS_UNKNOWN", key="settings.unknown", params={"key": key})
        row = await db.get(RuntimeSetting, key)
        if row is not None:
            db.add(RuntimeSettingHistory(key=key, old_value=row.value, new_value=None, changed_by=actor_id))
            await db.delete(row)
            await db.flush()
            self._announce(db, [key], actor_id)
        self._invalidate_on_commit(db)
        return self._merge(await self._overrides(db))

    @staticmethod
    def _upsert(db: AsyncSession, key: str, value: Any, actor_id: uuid.UUID | None):
        dialect = sqlite if db.bind is not None and db.bind.dialect.name == "sqlite" else postgresql
        values = {"key": key, "value": value, "updated_by": actor_id, "updated_at": now_utc()}
        statement = dialect.insert(RuntimeSetting).values(**values)
        changed = {name: value for name, value in values.items() if name != "key"}
        return statement.on_conflict_do_update(index_elements=["key"], set_=changed)

    async def history(self, db: AsyncSession, pages: PageParams, transformer) -> OffsetPage:
        return await paginate_offset(
            db,
            select(RuntimeSettingHistory),
            number=pages.number,
            size=pages.size,
            order_by=[RuntimeSettingHistory.created_at.desc(), RuntimeSettingHistory.id.desc()],
            transformer=transformer,
        )

    def _announce(self, db: AsyncSession, keys: list[str], actor_id: uuid.UUID | None) -> None:
        publish_event(db, SUBJECT, {"service": self.service, "keys": keys, "by": str(actor_id) if actor_id else None})


def _patch_model(schema: type[RuntimeSchema]) -> type[BaseModel]:
    fields = {}
    for name, info in schema.model_fields.items():
        annotated = Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation
        fields[name] = (annotated | None, Field(default=None, description=info.description))
    return create_model(f"{schema.__name__}Patch", __config__=ConfigDict(extra="forbid"), **fields)


def runtime_settings_router(store: RuntimeStore, *, db_dependency, role: str = "superadmin") -> APIRouter:
    """Admin endpoints for a service's runtime settings, mounted under ``/admin/settings``."""
    router = APIRouter(prefix="/admin/settings", tags=["Admin"], dependencies=[Depends(Requires(role))])
    Patch = _patch_model(store.schema)
    DbDep = Annotated[AsyncSession, Depends(db_dependency, scope="function")]
    Admin = Annotated[Actor, Depends(Requires(role))]

    @router.get("", summary="Runtime settings", responses=problem_responses(401, 403))
    async def read_settings(db: DbDep) -> RuntimeSettingsRead:
        """Current values with defaults, bounds and which keys are overridden."""
        return await store.describe(db)

    @router.patch(
        "",
        summary="Change runtime settings",
        description=(
            "Partial update. Values apply to new work within the cache window; existing snapshots are untouched."
        ),
        responses=problem_responses(400, 401, 403, 422),
    )
    async def patch_settings(body: Patch, db: DbDep, admin: Admin) -> RuntimeSettingsRead:
        """Override one or more settings."""
        await store.update(db, body.model_dump(exclude_unset=True), actor_id=admin.user_id)
        return await store.describe(db)

    @router.delete("/{key}", summary="Reset a setting", responses=problem_responses(400, 401, 403))
    async def reset_setting(key: str, db: DbDep, admin: Admin) -> RuntimeSettingsRead:
        """Drop the override so the default applies again."""
        await store.reset(db, key, actor_id=admin.user_id)
        return await store.describe(db)

    @router.get("/history", summary="Settings history", responses=problem_responses(401, 403))
    async def settings_history(db: DbDep, pages: PageQuery) -> OffsetPage[RuntimeHistoryRead]:
        """Who changed what, newest first."""
        return await store.history(db, pages, transformer=RuntimeHistoryRead.from_model)

    return router
