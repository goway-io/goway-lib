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
