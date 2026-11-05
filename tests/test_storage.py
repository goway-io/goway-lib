from urllib.parse import parse_qs, urlparse

from goway_lib.storage import Storage, StorageClass

BUCKETS = {StorageClass.PRIVATE: "p", StorageClass.MEDIA: "m", StorageClass.PUBLIC: "pub"}


def make_storage(**overrides):
    return Storage(
        endpoint="http://seaweedfs:8333",
        access_key="test",
        secret_key="test",
        buckets=BUCKETS,
        **overrides,
    )


def test_presign_put_is_signed_for_the_public_host_with_exact_length_and_type():
    storage = make_storage(public_endpoint="https://storage.example.uz")
    url = storage.presign_put("m", "auth/avatar/x", content_type="image/jpeg", content_length=1234, expires_in=900)
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "storage.example.uz", "/m/auth/avatar/x")
    signed = parse_qs(parsed.query)["X-Amz-SignedHeaders"][0].split(";")
    assert {"content-length", "content-type", "host"} <= set(signed)


def test_presign_get_forces_type_and_inline_disposition():
    storage = make_storage()
    url = storage.presign_get("m", "k", expires_in=600, content_type="image/png", filename="my photo.png")
    query = parse_qs(urlparse(url).query)
    assert url.startswith("http://seaweedfs:8333/m/k?")
    assert query["response-content-type"] == ["image/png"]
    assert query["response-content-disposition"] == ["inline; filename*=UTF-8''my%20photo.png"]


def test_public_url_uses_the_public_base_or_falls_back_to_the_bucket_path():
    assert make_storage(public_base_url="https://cdn.example.uz/").public_url("a/b.webp") == (
        "https://cdn.example.uz/a/b.webp"
    )
    assert make_storage(public_endpoint="https://storage.example.uz").public_url("a") == (
        "https://storage.example.uz/pub/a"
    )


def test_bucket_by_class_accepts_the_string_form():
    assert make_storage().bucket("private") == "p"


class FakeBody:
    def __init__(self, data: bytes):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self, size: int = -1) -> bytes:
        chunk, self.data = (self.data, b"") if size < 0 else (self.data[:size], self.data[size:])
        return chunk


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 - boto3 argument names
        self.objects[Bucket, Key] = (Body, ContentType)

    async def get_object(self, *, Bucket, Key):  # noqa: N803 - boto3 argument names
        return {"Body": FakeBody(self.objects[Bucket, Key][0])}


async def test_put_then_stream_reads_the_object_back_in_chunks(monkeypatch):
    storage = make_storage()
    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda: fake)

    await storage.put("p", "market/raw/doc", b"abcdefgh", content_type="application/pdf")
    async with storage.stream("p", "market/raw/doc") as body:
        chunks = [await body.read(3), await body.read(3), await body.read(3), await body.read(3)]

    assert fake.objects["p", "market/raw/doc"][1] == "application/pdf"
    assert chunks == [b"abc", b"def", b"gh", b""]
