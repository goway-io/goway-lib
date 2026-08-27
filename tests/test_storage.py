import uuid

from goway_lib.storage import Storage


def make_storage():
    return Storage(
        endpoint="http://localhost:9000",
        access_key="test",
        secret_key="test",
        bucket="test-bucket",
    )


async def test_presign_upload_policy_enforces_limits():
    storage = make_storage()
    key = f"avatar/{uuid.uuid7()}"
    url, fields = await storage.presign_upload(key=key, content_type="image/jpeg", max_bytes=5_000_000)
    assert url.startswith("http://localhost:9000")
    assert fields["Content-Type"] == "image/jpeg"
    assert fields["key"] == key
    assert "policy" in fields and "x-amz-signature" in fields


async def test_presign_get_signed_url():
    storage = make_storage()
    url = await storage.presign_get("docs/x.pdf")
    assert "X-Amz-Signature" in url and "docs/x.pdf" in url
