from urllib.parse import parse_qs, urlparse

from botocore.exceptions import ClientError

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
    """The calls a test needs from an aiobotocore S3 client, recorded in order."""

    def __init__(self, existing: set[str] = frozenset()):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.existing = set(existing)
        self.calls: list[tuple[str, dict]] = []

    async def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 - boto3 argument names
        self.objects[Bucket, Key] = (Body, ContentType)

    async def get_object(self, *, Bucket, Key):  # noqa: N803 - boto3 argument names
        return {"Body": FakeBody(self.objects[Bucket, Key][0])}

    async def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))

    async def head_bucket(self, *, Bucket):  # noqa: N803 - boto3 argument names
        if Bucket not in self.existing:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def __getattr__(self, name):
        async def record(**kwargs):
            self.calls.append((name, kwargs))

        return record


def with_fake(storage: Storage, fake: FakeS3) -> Storage:
    storage._client = fake
    return storage


async def test_put_then_stream_reads_the_object_back_in_chunks():
    fake = FakeS3()
    storage = with_fake(make_storage(), fake)

    await storage.put("p", "market/raw/doc", b"abcdefgh", content_type="application/pdf")
    async with storage.stream("p", "market/raw/doc") as body:
        chunks = [await body.read(3), await body.read(3), await body.read(3), await body.read(3)]

    assert fake.objects["p", "market/raw/doc"][1] == "application/pdf"
    assert chunks == [b"abc", b"def", b"gh", b""]


async def test_delete_is_one_plain_delete_without_versions():
    fake = FakeS3()
    await with_fake(make_storage(), fake).delete("p", "auth/doc/1")
    assert fake.calls == [("delete_object", {"Bucket": "p", "Key": "auth/doc/1"})]


def test_presigned_urls_carry_no_checksum_parameters():
    storage = make_storage()
    put = parse_qs(
        urlparse(storage.presign_put("m", "k", content_type="image/png", content_length=9, expires_in=60)).query
    )
    get = parse_qs(urlparse(storage.presign_get("m", "k", expires_in=60)).query)
    assert not [name for name in (*put, *get) if "checksum" in name.lower()]


async def test_one_client_serves_every_call_until_closed():
    async with make_storage() as storage:
        first = await storage._s3()
        assert await storage._s3() is first
    assert storage._client is None


async def test_provision_creates_missing_buckets_with_lifecycle_and_cors_only_where_browsers_upload():
    fake = FakeS3(existing={"p"})
    await with_fake(make_storage(), fake).provision(cors_origins=["https://app.example.uz"])

    names = [name for name, _ in fake.calls]
    assert ("create_bucket", {"Bucket": "m"}) in fake.calls and ("create_bucket", {"Bucket": "pub"}) in fake.calls
    assert ("create_bucket", {"Bucket": "p"}) not in fake.calls
    assert "put_bucket_versioning" not in names
    lifecycles = [kwargs for name, kwargs in fake.calls if name == "put_bucket_lifecycle_configuration"]
    assert [rule["ID"] for kwargs in lifecycles for rule in kwargs["LifecycleConfiguration"]["Rules"]] == [
        "abort-incomplete-uploads"
    ] * 3
    cors = {kwargs["Bucket"] for name, kwargs in fake.calls if name == "put_bucket_cors"}
    assert cors == {"p", "m"}
