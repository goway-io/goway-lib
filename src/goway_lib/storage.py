"""S3 object storage client.

Cloudflare R2 serves demo and production, SeaweedFS local development. Only the S3 subset both speak is used
(PUT, GET, HEAD, COPY, DELETE, presigned URLs), so the store is picked by configuration alone and no feature of one
store, such as versioning, is relied on.

The service reaches storage over ``endpoint``, but presigned URLs are signed for ``public_endpoint`` when that is
set: SigV4 signs the host, so a URL signed for ``http://seaweedfs:8333`` cannot be rewritten afterwards. On R2 there
is one host for both, the account's S3 endpoint.

Bucket setup needs admin rights the services do not hold, so it runs apart from them:
``python -m goway_lib.storage provision``.
"""

import argparse
import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

import botocore.session
from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from goway_lib.settings import StorageSettings

logger = logging.getLogger(__name__)

STORAGE_ERRORS = (BotoCoreError, ClientError, OSError)

_MISSING = ("404", "NoSuchKey", "NotFound")
_BUCKET_EXISTS = ("BucketAlreadyOwnedByYou", "BucketAlreadyExists")
_CHECKSUMS = {"request_checksum_calculation": "when_required", "response_checksum_validation": "when_required"}


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
            **_CHECKSUMS,
        )
        signing = Config(signature_version="s3v4", s3={"addressing_style": "path"}, **_CHECKSUMS)
        signing_endpoint = (public_endpoint or endpoint).rstrip("/")
        self._internal = {"endpoint_url": endpoint, "config": io, **credentials}
        self._signing = {"endpoint_url": signing_endpoint, "config": signing, **credentials}
        self._signer = None
        self._session = get_session()
        self._client = None
        self._client_scope: AsyncExitStack | None = None
        self._client_lock = asyncio.Lock()
        self.buckets = {StorageClass(k): v for k, v in buckets.items()}
        self._public_base = (public_base_url or f"{signing_endpoint}/{self.buckets[StorageClass.PUBLIC]}").rstrip("/")

    @classmethod
    def from_settings(cls, settings: StorageSettings) -> "Storage":
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

    async def __aenter__(self) -> "Storage":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client_scope is not None:
            scope, self._client_scope, self._client = self._client_scope, None, None
            await scope.aclose()

    async def _s3(self):
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    scope = AsyncExitStack()
                    self._client = await scope.enter_async_context(self._session.create_client("s3", **self._internal))
                    self._client_scope = scope
        return self._client

    def bucket(self, storage_class: StorageClass | str) -> str:
        return self.buckets[StorageClass(storage_class)]

    def _sign(self, operation: str, params: dict, expires_in: int) -> str:
        if self._signer is None:
            self._signer = botocore.session.get_session().create_client("s3", **self._signing)
        return self._signer.generate_presigned_url(operation, Params=params, ExpiresIn=expires_in)

    def presign_put(self, bucket: str, key: str, *, content_type: str, content_length: int, expires_in: int) -> str:
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
        s3 = await self._s3()
        try:
            response = await s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _MISSING:
                return None
            raise
        return ObjectInfo(size=response["ContentLength"], content_type=response.get("ContentType"))

    async def read_prefix(self, bucket: str, key: str, length: int = 64) -> bytes:
        s3 = await self._s3()
        response = await s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{length - 1}")
        async with response["Body"] as body:
            return await body.read()

    async def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        s3 = await self._s3()
        await s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    @asynccontextmanager
    async def stream(self, bucket: str, key: str) -> AsyncGenerator:
        s3 = await self._s3()
        response = await s3.get_object(Bucket=bucket, Key=key)
        async with response["Body"] as body:
            yield body

    async def copy(self, source_bucket: str, key: str, target_bucket: str) -> None:
        s3 = await self._s3()
        await s3.copy_object(Bucket=target_bucket, Key=key, CopySource={"Bucket": source_bucket, "Key": key})

    async def delete(self, bucket: str, key: str) -> None:
        s3 = await self._s3()
        await s3.delete_object(Bucket=bucket, Key=key)

    async def provision(self, *, cors_origins: Sequence[str] = ()) -> None:
        s3 = await self._s3()
        for storage_class, bucket in self.buckets.items():
            try:
                await s3.head_bucket(Bucket=bucket)
            except ClientError:
                try:
                    await s3.create_bucket(Bucket=bucket)
                except ClientError as exc:
                    if _error_code(exc) not in _BUCKET_EXISTS:
                        raise
            rule = {
                "ID": "abort-incomplete-uploads",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
            await s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [rule]})
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
            logger.info("provisioned bucket %s (%s)", bucket, storage_class)


class ProvisionSettings(StorageSettings):
    S3_CORS_ORIGINS: list[str] = []


async def _provision() -> None:
    settings = ProvisionSettings()
    async with Storage.from_settings(settings) as storage:
        await storage.provision(cors_origins=settings.S3_CORS_ORIGINS)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m goway_lib.storage",
        description="Bucket setup with admin credentials from the S3_* environment variables.",
    )
    parser.add_argument("command", choices=["provision"])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(_provision())


if __name__ == "__main__":
    main()
