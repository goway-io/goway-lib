import base64
import binascii
import math
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel as Schema
from pydantic import Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from goway_lib.errors import BadRequest

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class PageInfo(Schema):
    next_cursor: str | None = None
    has_more: bool = False


class Page(Schema, Generic[T]):
    items: list[T]
    page: PageInfo


class OffsetInfo(Schema):
    """Page numbers and a total. Admin screens only: operators need to know how much there is and
    jump around, which a cursor cannot answer. Customer clients keep the cursor envelope."""

    number: int = Field(default=1, description="1-based index of this page.")
    size: int = Field(default=DEFAULT_PAGE_SIZE, description="Rows per page.")
    total: int = Field(default=0, description="Rows matching the filter, across all pages.")
    pages: int = Field(default=0, description="Number of pages at this size; 0 when nothing matches.")


class OffsetPage(Schema, Generic[T]):
    items: list[T]
    page: OffsetInfo

    @classmethod
    def of(cls, items: list[T]) -> "OffsetPage[T]":
        """A complete list in the numbered envelope: fixed small sets — roles, FAQ categories — that
        fit one screen but still need a count."""
        total = len(items)
        page = OffsetInfo(number=1, size=total or DEFAULT_PAGE_SIZE, total=total, pages=1 if total else 0)
        return cls(items=items, page=page)


#: The last page number a client may ask for, so page * size stays far inside Postgres's bigint OFFSET.
MAX_PAGE = 1_000_000


@dataclass(frozen=True)
class PageParams:
    number: int = 1
    size: int = DEFAULT_PAGE_SIZE


def page_params(
    page: Annotated[int, Query(ge=1, le=MAX_PAGE, description="1-based page number.")] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Rows per page.")] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    return PageParams(number=page, size=size)


PageQuery = Annotated[PageParams, Depends(page_params)]


def encode_cursor(id_: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(id_.bytes).decode().rstrip("=")


def decode_cursor(cursor: str) -> uuid.UUID:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return uuid.UUID(bytes=base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise BadRequest("Invalid cursor", code="INVALID_CURSOR", key="common.invalid_cursor") from exc


async def paginate_offset(
    db: AsyncSession,
    query: Select,
    *,
    number: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
    order_by: Any = None,
    options: list | None = None,
    transformer: Any = None,
) -> OffsetPage[Any]:
    """Counts first, then fetches one page. Two queries on purpose: the count is what makes page
    numbers possible, and admin tables are small enough that it is cheap. `options` are eager loads,
    applied to the row query only — a joined collection would multiply the count. `order_by` takes a
    clause or a list of them; end it with a unique column or tied rows shuffle between pages."""
    size = min(max(size, 1), MAX_PAGE_SIZE)
    number = max(number, 1)
    total = int((await db.execute(select(func.count()).select_from(query.order_by(None).subquery()))).scalar_one())
    pages = math.ceil(total / size) if total else 0
    for clause in order_by if isinstance(order_by, list) else [order_by] if order_by is not None else []:
        query = query.order_by(clause)
    for option in options or []:
        query = query.options(option)
    rows = list((await db.execute(query.offset((number - 1) * size).limit(size))).scalars().all())
    items = [transformer(row) for row in rows] if transformer else rows
    return OffsetPage(items=items, page=OffsetInfo(number=number, size=size, total=total, pages=pages))


async def paginate(
    db: AsyncSession,
    query: Select,
    model: type,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    transformer: Any = None,
) -> Page[Any]:
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    if cursor:
        query = query.where(model.id < decode_cursor(cursor))
    query = query.order_by(model.id.desc()).limit(limit + 1)
    rows = list((await db.execute(query)).scalars().all())

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].id) if has_more and rows else None
    items = [transformer(row) for row in rows] if transformer else rows
    return Page(items=items, page=PageInfo(next_cursor=next_cursor, has_more=has_more))
