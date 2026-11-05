"""S3 object storage client.

The service reaches storage over the internal endpoint, but presigned URLs are signed for the
public one: SigV4 signs the host, so a URL signed for ``http://seaweedfs:8333`` cannot be
rewritten to the public domain afterwards. Only the portable S3 subset is used — PUT, GET, HEAD,
DELETE, CORS, versioning, lifecycle — so any S3-compatible store works by changing configuration.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

import botocore.session
from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

STORAGE_ERRORS = (BotoCoreError, ClientError, OSError)

_MISSING = ("404", "NoSuchKey", "NotFound")
_BUCKET_EXISTS = ("BucketAlreadyOwnedByYou", "BucketAlreadyExists")
NONCURRENT_VERSION_DAYS = 30


class StorageClass(StrEnum):
    PRIVATE = "private"
    MEDIA = "media"
    PUBLIC = "public"


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class Storage:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        buckets: dict[StorageClass, str],
        region: str = "us-east-1",
        public_endpoint: str = "",
        public_base_url: str = "",
    ):
        credentials = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
        }
        io = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
            retries={"mode": "standard", "max_attempts": 3},
        )
        signing = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        signing_endpoint = (public_endpoint or endpoint).rstrip("/")
        self._internal = {"endpoint_url": endpoint, "config": io, **credentials}
        self._signing = {"endpoint_url": signing_endpoint, "config": signing, **credentials}
        self._signer = None
        self._session = get_session()
        self.buckets = {StorageClass(k): v for k, v in buckets.items()}
        self._public_base = (public_base_url or f"{signing_endpoint}/{self.buckets[StorageClass.PUBLIC]}").rstrip("/")

    @classmethod
    def from_settings(cls, settings) -> "Storage":
        return cls(
            endpoint=settings.S3_ENDPOINT,
            access_key=settings.S3_ACCESS_KEY,
            secret_key=settings.S3_SECRET_KEY,
            region=settings.S3_REGION,
            public_endpoint=settings.S3_PUBLIC_ENDPOINT,
            public_base_url=settings.S3_PUBLIC_BASE_URL,
            buckets={
                StorageClass.PRIVATE: settings.S3_BUCKET_PRIVATE,
                StorageClass.MEDIA: settings.S3_BUCKET_MEDIA,
                StorageClass.PUBLIC: settings.S3_BUCKET_PUBLIC,
            },
        )

    def bucket(self, storage_class: StorageClass | str) -> str:
        return self.buckets[StorageClass(storage_class)]

    def _client(self):
        return self._session.create_client("s3", **self._internal)

    def _sign(self, operation: str, params: dict, expires_in: int) -> str:
        """Presigning is pure computation, so one synchronous client serves every request."""
        if self._signer is None:
            self._signer = botocore.session.get_session().create_client("s3", **self._signing)
        return self._signer.generate_presigned_url(operation, Params=params, ExpiresIn=expires_in)

    def presign_put(self, bucket: str, key: str, *, content_type: str, content_length: int, expires_in: int) -> str:
        """The type and exact length are part of the signature: any other body is refused by storage."""
        params = {"Bucket": bucket, "Key": key, "ContentType": content_type, "ContentLength": content_length}
        return self._sign("put_object", params, expires_in)

    def presign_get(
        self, bucket: str, key: str, *, expires_in: int, content_type: str | None = None, filename: str | None = None
    ) -> str:
        disposition = "inline"
        if filename:
            disposition += f"; filename*=UTF-8''{quote(filename)}"
        params = {"Bucket": bucket, "Key": key, "ResponseContentDisposition": disposition}
        if content_type:
            params["ResponseContentType"] = content_type
        return self._sign("get_object", params, expires_in)

    def public_url(self, key: str) -> str:
        return f"{self._public_base}/{key}"

    async def head(self, bucket: str, key: str) -> ObjectInfo | None:
        async with self._client() as s3:
            try:
                response = await s3.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if _error_code(exc) in _MISSING:
                    return None
                raise
        return ObjectInfo(size=response["ContentLength"], content_type=response.get("ContentType"))

    async def read_prefix(self, bucket: str, key: str, length: int = 64) -> bytes:
        async with self._client() as s3:
            response = await s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{length - 1}")
            async with response["Body"] as body:
                return await body.read()

    async def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        """Writes an object the service produced itself, such as a fetched document; clients upload presigned."""
        async with self._client() as s3:
            await s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    @asynccontextmanager
    async def stream(self, bucket: str, key: str) -> AsyncIterator:
        """The object's body, read in chunks with `await body.read(size)`, for files too large to hold in memory."""
        async with self._client() as s3:
            response = await s3.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as body:
                yield body

    async def copy(self, source_bucket: str, key: str, target_bucket: str) -> None:
        async with self._client() as s3:
            await s3.copy_object(Bucket=target_bucket, Key=key, CopySource={"Bucket": source_bucket, "Key": key})

    async def delete(self, bucket: str, key: str) -> None:
        """Removes every version, so a delete on the versioned private bucket really frees the data."""
        async with self._client() as s3:
            if bucket == self.buckets[StorageClass.PRIVATE]:
                listing = await s3.list_object_versions(Bucket=bucket, Prefix=key)
                versions = [
                    v["VersionId"]
                    for v in listing.get("Versions", []) + listing.get("DeleteMarkers", [])
                    if v["Key"] == key
                ]
                for version_id in versions:
                    await s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
                if versions:
                    return
            await s3.delete_object(Bucket=bucket, Key=key)

    async def ensure_buckets(self, *, cors_origins: list[str] | tuple[str, ...] = ()) -> None:
        """Idempotent setup, safe on every start. Public read is granted by the store's own access
        config (SeaweedFS: the ``anonymous`` identity), not by a bucket policy."""
        async with self._client() as s3:
            for storage_class, bucket in self.buckets.items():
                try:
                    await s3.head_bucket(Bucket=bucket)
                except ClientError:
                    try:
                        await s3.create_bucket(Bucket=bucket)
                    except ClientError as exc:
                        if _error_code(exc) not in _BUCKET_EXISTS:
                            raise
                rules = [
                    {
                        "ID": "abort-incomplete-uploads",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
                if storage_class is StorageClass.PRIVATE:
                    await s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
                    rules.append(
                        {
                            "ID": "expire-old-versions",
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "NoncurrentVersionExpiration": {"NoncurrentDays": NONCURRENT_VERSION_DAYS},
                        }
                    )
                await s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": rules})
                if cors_origins and storage_class is not StorageClass.PUBLIC:
                    await s3.put_bucket_cors(
                        Bucket=bucket,
                        CORSConfiguration={
                            "CORSRules": [
                                {
                                    "AllowedOrigins": list(cors_origins),
                                    "AllowedMethods": ["PUT", "GET", "HEAD"],
                                    "AllowedHeaders": ["*"],
                                    "ExposeHeaders": ["ETag"],
                                    "MaxAgeSeconds": 3600,
                                }
                            ]
                        },
                    )
