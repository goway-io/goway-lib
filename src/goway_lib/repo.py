import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from goway_lib.db import SoftDeleteModel
from goway_lib.pagination import Page, paginate
from goway_lib.timeutils import now_utc

M = TypeVar("M")


class BaseRepository(Generic[M]):
    model: type[M]

    def __init__(self, db: AsyncSession):
        self.db = db

    def query(self, *, include_deleted: bool = False) -> Select:
        q = select(self.model)
        if not include_deleted and issubclass(self.model, SoftDeleteModel):
            q = q.where(self.model.deleted_at.is_(None))
        return q

    async def get(self, id_: uuid.UUID, *, include_deleted: bool = False, options: list | None = None) -> M | None:
        q = self.query(include_deleted=include_deleted).where(self.model.id == id_)
        for opt in options or []:
            q = q.options(opt)
        return (await self.db.execute(q)).scalar_one_or_none()

    async def list_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 25,
        where: list | None = None,
        options: list | None = None,
        transformer: Any = None,
    ) -> Page[Any]:
        q = self.query()
        for clause in where or []:
            q = q.where(clause)
        for opt in options or []:
            q = q.options(opt)
        return await paginate(self.db, q, self.model, cursor=cursor, limit=limit, transformer=transformer)

    async def create(self, **kwargs: Any) -> M:
        obj = self.model(**kwargs)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, obj: M, **kwargs: Any) -> M:
        for key, value in kwargs.items():
            setattr(obj, key, value)
        await self.db.flush()
        return obj

    async def soft_delete(self, obj: M) -> M:
        if not isinstance(obj, SoftDeleteModel):
            raise TypeError(f"{type(obj).__name__} is not soft-deletable")
        obj.deleted_at = now_utc()
        await self.db.flush()
        return obj

    async def restore(self, obj: M) -> M:
        if not isinstance(obj, SoftDeleteModel):
            raise TypeError(f"{type(obj).__name__} is not soft-deletable")
        obj.deleted_at = None
        await self.db.flush()
        return obj


class OwnedRepository(BaseRepository[M]):
    """Scopes every read to an owning organization. The model needs org_id."""

    def __init__(self, db: AsyncSession, org_id: uuid.UUID):
        super().__init__(db)
        self.org_id = org_id

    def query(self, *, include_deleted: bool = False) -> Select:
        return super().query(include_deleted=include_deleted).where(self.model.org_id == self.org_id)

    async def create(self, **kwargs: Any) -> M:
        kwargs.setdefault("org_id", self.org_id)
        return await super().create(**kwargs)
