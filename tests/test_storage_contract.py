"""The storage behaviour the services rely on, checked against a real S3 store instead of the in-memory fake.

Runs only when ``S3_CONTRACT_ENDPOINT`` is set, with ``S3_CONTRACT_ACCESS_KEY``, ``S3_CONTRACT_SECRET_KEY``,
``S3_CONTRACT_REGION`` (default ``auto``) and two existing buckets, ``S3_CONTRACT_BUCKET`` and
``S3_CONTRACT_OTHER_BUCKET``. Everything is written under a random ``contract/`` prefix and removed afterwards.
Point it at the local SeaweedFS (``make buckets`` in goway-infra) or at an R2 bucket pair.
"""

import os
import uuid

import httpx
import pytest

from goway_lib.storage import Storage, StorageClass

ENDPOINT = os.environ.get("S3_CONTRACT_ENDPOINT", "")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="S3_CONTRACT_ENDPOINT is not set")

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 96


@pytest.fixture
async def store():
    bucket, other = os.environ.get("S3_CONTRACT_BUCKET", ""), os.environ.get("S3_CONTRACT_OTHER_BUCKET", "")
    async with Storage(
        endpoint=ENDPOINT,
        access_key=os.environ.get("S3_CONTRACT_ACCESS_KEY", ""),
        secret_key=os.environ.get("S3_CONTRACT_SECRET_KEY", ""),
        region=os.environ.get("S3_CONTRACT_REGION", "auto"),
        buckets={StorageClass.PRIVATE: bucket, StorageClass.MEDIA: other, StorageClass.PUBLIC: other},
    ) as storage:
        written: list[tuple[str, str]] = []
        yield storage, bucket, other, written
        for b, key in written:
            await storage.delete(b, key)


@pytest.fixture
async def http():
    async with httpx.AsyncClient(timeout=20) as client:
        yield client


def new_key() -> str:
    return f"contract/{uuid.uuid4()}"


async def put(url: str, body: bytes, content_type: str) -> int:
    """One connection per upload, as a client makes it: SeaweedFS answers a refused PUT before reading the body, which
    leaves a reused keep-alive connection unusable."""
    async with httpx.AsyncClient(timeout=20) as client:
        return (await client.put(url, content=body, headers={"Content-Type": content_type})).status_code


async def test_presigned_put_accepts_only_the_signed_type_and_length(store):
    storage, bucket, _, written = store
    key = new_key()
    written.append((bucket, key))
    url = storage.presign_put(bucket, key, content_type="image/jpeg", content_length=len(JPEG), expires_in=300)

    assert await put(url, JPEG, "image/png") == 403
    assert await put(url, JPEG + b"x", "image/jpeg") == 403
    assert await put(url, JPEG[:50], "image/jpeg") == 403
    assert await put(url, JPEG, "image/jpeg") == 200

    info = await storage.head(bucket, key)
    assert info is not None and info.size == len(JPEG)
    assert await storage.read_prefix(bucket, key, 4) == JPEG[:4]


async def test_presigned_get_serves_the_type_and_inline_name_it_was_signed_with(store, http):
    storage, bucket, _, written = store
    key = new_key()
    written.append((bucket, key))
    await storage.put(bucket, key, JPEG, content_type="application/octet-stream")

    response = await http.get(
        storage.presign_get(bucket, key, expires_in=300, content_type="image/jpeg", filename="my photo.jpg")
    )

    assert response.status_code == 200 and response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"] == "inline; filename*=UTF-8''my%20photo.jpg"


async def test_copy_between_buckets_and_delete_are_idempotent(store):
    storage, bucket, other, written = store
    key = new_key()
    written.extend([(bucket, key), (other, key)])
    await storage.put(bucket, key, JPEG, content_type="image/jpeg")

    await storage.copy(bucket, key, other)
    copied = await storage.head(other, key)
    assert copied is not None and copied.size == len(JPEG) and copied.content_type == "image/jpeg"

    await storage.delete(bucket, key)
    await storage.delete(bucket, key)
    assert await storage.head(bucket, key) is None
