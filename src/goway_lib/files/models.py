import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from goway_lib.db import BaseModel


class FileStatus(StrEnum):
    PENDING = "pending"
    UPLOADED = "uploaded"
    READY = "ready"
    REJECTED = "rejected"
    DELETED = "deleted"


class FileRecord(BaseModel):
    """One stored object and the facts about it. What the file means lives in the domain row that
    references it; ``ref_count`` counts those references so orphans can be swept."""

    __tablename__ = "files"

    org_id: Mapped[uuid.UUID] = mapped_column(index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    storage_class: Mapped[str] = mapped_column(String(10))
    bucket: Mapped[str] = mapped_column(String(63))
    key: Mapped[str] = mapped_column(String(255), unique=True)
    original_name: Mapped[str | None] = mapped_column(String(255), default=None)
    declared_type: Mapped[str] = mapped_column(String(100))
    detected_type: Mapped[str | None] = mapped_column(String(100), default=None)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(10), default=FileStatus.PENDING, server_default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(40), default=None)
    ref_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retain_until: Mapped[datetime | None] = mapped_column(default=None)
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    uploaded_at: Mapped[datetime | None] = mapped_column(default=None)
    ready_at: Mapped[datetime | None] = mapped_column(default=None)
    attached_at: Mapped[datetime | None] = mapped_column(default=None)
    detached_at: Mapped[datetime | None] = mapped_column(default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class FileAccessLog(BaseModel):
    """Who outside the owning organization was handed a view URL for a sensitive file."""

    __tablename__ = "file_access_log"

    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[uuid.UUID]
    actor_role: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(40))
