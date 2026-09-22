import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from goway_lib.files.models import FileStatus


class RejectReason(StrEnum):
    SIZE_MISMATCH = "size_mismatch"
    TYPE_MISMATCH = "type_mismatch"


class UploadRead(BaseModel):
    file_id: uuid.UUID = Field(description="Reference this id when attaching the file.")
    method: Literal["PUT"] = "PUT"
    url: str = Field(description="Presigned URL. Send the file body here, exactly `size_bytes` long.")
    headers: dict[str, str] = Field(description="Headers the PUT must carry, verbatim.")
    expires_at: datetime = Field(description="The URL stops working after this moment.")


class FileRef(BaseModel):
    id: uuid.UUID
    kind: str = Field(description="The service's file kind the upload was made as.", examples=["identity_document"])
    status: FileStatus = Field(description="Only `ready` files have a view URL.", examples=["ready"])
    content_type: str = Field(description="Detected from the bytes once ready.", examples=["image/jpeg"])
    size_bytes: int = Field(examples=[204800])
    name: str | None = Field(default=None, description="Original file name, if the client sent one.")
    width: int | None = Field(default=None, description="Pixels, once known.")
    height: int | None = Field(default=None, description="Pixels, once known.")
    url: str | None = Field(description="View URL once the file is ready; short-lived unless the file is public.")
    expires_at: datetime | None = Field(description="When `url` stops working; null for public files.")
    reject_reason: RejectReason | None = Field(default=None, description="Why a `rejected` upload was refused.")
