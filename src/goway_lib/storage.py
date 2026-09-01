import uuid
from dataclasses import dataclass

from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from goway_lib.db import BaseModel


class FileRecordBase(BaseModel):
    __abstract__ = True

    org_id: Mapped[uuid.UUID] = mapped_column(index=True)
    created_by: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(40), index=True)
    bucket: Mapped[str] = mapped_column(String(63))
    key: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)


@dataclass(frozen=True)
class UploadSlot:
    file_id: uuid.UUID
    url: str
    fields: dict


class Storage:
    def __init__(self, *, endpoint: str, access_key: str, secret_key: str, bucket: str, region: str = "us-east-1"):
        self.endpoint = endpoint
        self.bucket = bucket
        self._client_kwargs = {
            "endpoint_url": endpoint,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "config": Config(signature_version="s3v4"),
        }
        self._session = get_session()

    def _client(self):
        return self._session.create_client("s3", **self._client_kwargs)

    async def ensure_bucket(self) -> None:
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self.bucket)
            except Exception:
                await s3.create_bucket(Bucket=self.bucket)

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        max_bytes: int,
        expires_in: int = 900,
    ) -> tuple[str, dict]:
        async with self._client() as s3:
            post = await s3.generate_presigned_post(
                Bucket=self.bucket,
                Key=key,
                Fields={"Content-Type": content_type},
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 1, max_bytes],
                ],
                ExpiresIn=expires_in,
            )
        return post["url"], post["fields"]

    async def presign_get(self, key: str, *, expires_in: int = 3600) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
            )

    async def object_exists(self, key: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

    async def delete_object(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)


@dataclass(frozen=True)
class FileKindSpec:
    max_bytes: int
    content_types: frozenset[str]


async def create_upload_slot(
    file_repo,
    storage: "Storage",
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    kind: str,
    content_type: str,
    size_bytes: int,
    kinds: dict[str, FileKindSpec],
) -> dict:

    from goway_lib.errors import BadRequest

    spec = kinds.get(kind)
    if spec is None:
        raise BadRequest("Unknown file kind", code="FILE_KIND_INVALID", key="files.kind_invalid")
    if content_type not in spec.content_types:
        raise BadRequest(
            "Content type not allowed for this kind",
            code="FILE_TYPE_INVALID",
            key="files.type_invalid",
            params={"allowed": sorted(spec.content_types)},
        )
    if not 0 < size_bytes <= spec.max_bytes:
        raise BadRequest(
            "File too large", code="FILE_TOO_LARGE", key="files.too_large", params={"max_bytes": spec.max_bytes}
        )
    key = f"{kind}/{org_id}/{uuid.uuid7()}"
    record = await file_repo.create(
        org_id=org_id,
        created_by=created_by,
        kind=kind,
        bucket=storage.bucket,
        key=key,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    url, fields = await storage.presign_upload(key=key, content_type=content_type, max_bytes=spec.max_bytes)
    return {"file_id": record.id, "url": url, "fields": fields}


async def require_uploaded(
    file_repo, storage: "Storage", file_id: uuid.UUID, org_id: uuid.UUID, *, kinds: tuple[str, ...]
) -> None:

    from goway_lib.errors import BadRequest, UpstreamUnavailable

    record = await file_repo.get_owned(file_id, org_id)
    if record is None or record.kind not in kinds:
        raise BadRequest("File not found for this account", code="FILE_INVALID", key="files.invalid")
    if record.status != "ready":
        try:
            uploaded = await storage.object_exists(record.key)
        except Exception as exc:
            raise UpstreamUnavailable("Storage unavailable", params={"service": "storage"}) from exc
        if not uploaded:
            raise BadRequest("File is not uploaded yet", code="FILE_NOT_UPLOADED", key="files.not_uploaded")
        await file_repo.update(record, status="ready")
