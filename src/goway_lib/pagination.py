import base64
import binascii
import uuid
from typing import Any, Generic, TypeVar

from pydantic import BaseModel as Schema
from sqlalchemy import Select
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


def encode_cursor(id_: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(id_.bytes).decode().rstrip("=")


def decode_cursor(cursor: str) -> uuid.UUID:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return uuid.UUID(bytes=base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise BadRequest("Invalid cursor", code="INVALID_CURSOR", key="common.invalid_cursor") from exc


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
