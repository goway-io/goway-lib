import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from goway_lib import auth
from goway_lib.auth import Actor
from goway_lib.db import Base
from goway_lib.errors import BadRequest, Conflict, NotFound
from goway_lib.files import (
    DOCUMENT_TYPES,
    IMAGE_TYPES,
    FileAccessLog,
    FileKind,
    FileRecord,
    FileService,
    FileStatus,
    StorageClass,
    files_router,
    kind_map,
    sniff,
)
from goway_lib.handlers import install_handlers
from goway_lib.storage import ObjectInfo, Storage
from goway_lib.timeutils import now_utc

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 96
PDF = b"%PDF-1.7\n" + b"\0" * 91

KINDS = kind_map(
    FileKind("avatar", StorageClass.MEDIA, 5_000, IMAGE_TYPES),
    FileKind("identity_document", StorageClass.PRIVATE, 10_000, DOCUMENT_TYPES, sensitive=True),
    FileKind("trip_photo", StorageClass.MEDIA, 5_000, IMAGE_TYPES, retain_days=1095),
    FileKind("reference_image", StorageClass.PUBLIC, 5_000, IMAGE_TYPES),
)


class MemoryStorage(Storage):
    def __init__(self):
        super().__init__(
            endpoint="http://seaweedfs:8333",
            public_endpoint="https://storage.example.uz",
            access_key="k",
            secret_key="s",
            buckets={StorageClass.PRIVATE: "p", StorageClass.MEDIA: "m", StorageClass.PUBLIC: "pub"},
        )
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_deletes = False

    def put(self, record: FileRecord, body: bytes) -> None:
        self.objects[(record.bucket, record.key)] = body

    async def head(self, bucket, key):
        body = self.objects.get((bucket, key))
        return None if body is None else ObjectInfo(size=len(body), content_type=None)

    async def read_prefix(self, bucket, key, length=64):
        return self.objects[(bucket, key)][:length]

    async def delete(self, bucket, key):
        if self.fail_deletes:
            raise ConnectionError("storage down")
        self.objects.pop((bucket, key), None)

    async def copy(self, source_bucket, key, target_bucket):
        self.objects[(target_bucket, key)] = self.objects[(source_bucket, key)]


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def storage():
    return MemoryStorage()


@pytest.fixture
def files(db, storage):
    return FileService(db, storage, KINDS, service="auth")


ORG = uuid.uuid7()


async def uploaded(files, storage, kind="avatar", body=JPEG, content_type="image/jpeg", org=ORG) -> FileRecord:
    ticket = await files.create_upload(
        org_id=org, created_by=None, kind=kind, content_type=content_type, size_bytes=len(body), name="C:\\x\\me.jpg"
    )
    record = await files.db.get(FileRecord, ticket.file_id)
    storage.put(record, body)
    return record


def test_sniff_reads_real_types():
    assert sniff(JPEG) == "image/jpeg"
    assert sniff(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff(PDF) == "application/pdf"
    assert sniff(b"\0\0\0\x18ftypheic") == "image/heic"
    assert sniff(b"<svg xmlns=") is None


async def test_create_upload_validates_and_lays_out_the_key(files):
    with pytest.raises(BadRequest) as kind:
        await files.create_upload(org_id=ORG, created_by=None, kind="x", content_type="image/jpeg", size_bytes=1)
    assert kind.value.code == "FILE_KIND_INVALID"
    with pytest.raises(BadRequest) as ctype:
        await files.create_upload(
            org_id=ORG, created_by=None, kind="avatar", content_type="image/svg+xml", size_bytes=1
        )
    assert ctype.value.code == "FILE_TYPE_INVALID"
    with pytest.raises(BadRequest) as big:
        await files.create_upload(
            org_id=ORG, created_by=None, kind="avatar", content_type="image/jpeg", size_bytes=9_999
        )
    assert big.value.code == "FILE_TOO_LARGE"

    ticket = await files.create_upload(
        org_id=ORG, created_by=None, kind="identity_document", content_type="application/pdf", size_bytes=100
    )
    record = await files.db.get(FileRecord, ticket.file_id)
    assert record.bucket == "p" and record.status == FileStatus.PENDING
    assert record.key.startswith(f"auth/identity_document/{ORG}/") and record.key.endswith(str(record.id))
    assert ticket.url.startswith("https://storage.example.uz/p/auth/identity_document/")
    assert ticket.headers == {"Content-Type": "application/pdf"}


async def test_complete_checks_presence_size_and_real_type(files, storage):
    missing = await files.create_upload(
        org_id=ORG, created_by=None, kind="avatar", content_type="image/jpeg", size_bytes=10
    )
    with pytest.raises(BadRequest) as not_there:
        await files.complete(missing.file_id, ORG)
    assert not_there.value.code == "FILE_NOT_UPLOADED"

    good = await uploaded(files, storage)
    done = await files.complete(good.id, ORG)
    assert (done.status, done.detected_type, done.original_name) == (FileStatus.READY, "image/jpeg", "me.jpg")
    assert (await files.complete(good.id, ORG)).status == FileStatus.READY

    disguised = await uploaded(files, storage, body=b"<html>" + b"\0" * 94)
    rejected = await files.complete(disguised.id, ORG)
    assert (rejected.status, rejected.reject_reason) == (FileStatus.REJECTED, "type_mismatch")
    assert (disguised.bucket, disguised.key) not in storage.objects

    with pytest.raises(NotFound):
        await files.complete(good.id, uuid.uuid7())


async def test_attach_counts_references_and_starts_retention(files, storage):
    record = await uploaded(files, storage, kind="trip_photo")
    with pytest.raises(BadRequest):
        await files.attach(record.id, ORG, kinds=("avatar",))
    with pytest.raises(BadRequest):
        await files.attach(record.id, uuid.uuid7(), kinds=("trip_photo",))

    attached = await files.attach(record.id, ORG, kinds=("trip_photo",))
    assert (attached.status, attached.ref_count) == (FileStatus.READY, 1)
    assert attached.retain_until - attached.attached_at == timedelta(days=1095)
    await files.attach(record.id, ORG, kinds=("trip_photo",))
    await files.detach(record.id)
    await files.db.refresh(record)
    assert (record.ref_count, record.detached_at) == (1, None)
    await files.detach(record.id)
    await files.db.refresh(record)
    assert record.ref_count == 0 and record.detached_at is not None


async def test_attach_refuses_rejected_files(files, storage):
    record = await uploaded(files, storage, body=PDF, content_type="image/jpeg")
    with pytest.raises(BadRequest) as exc:
        await files.attach(record.id, ORG, kinds=("avatar",))
    assert exc.value.params["reason"] == "type_mismatch"


async def test_replace_attaches_new_and_detaches_dropped(files, storage):
    a, b, c = [await uploaded(files, storage) for _ in range(3)]
    kept = await files.replace([], [a.id, b.id, a.id], ORG, kinds=("avatar",))
    assert kept == [a.id, b.id]
    await files.replace(kept, [b.id, c.id], ORG, kinds=("avatar",))
    for record, count in ((a, 0), (b, 1), (c, 1)):
        await files.db.refresh(record)
        assert record.ref_count == count


async def test_refs_sign_views_and_log_sensitive_access_by_outsiders(files, storage):
    document = await uploaded(files, storage, kind="identity_document", body=PDF, content_type="application/pdf")
    public = await uploaded(files, storage, kind="reference_image")
    pending = await files.create_upload(
        org_id=ORG, created_by=None, kind="avatar", content_type="image/jpeg", size_bytes=5
    )
    for record in (document, public):
        await files.complete(record.id, ORG)

    owner = Actor(user_id=uuid.uuid7(), org_id=ORG, role="carrier", role_id=None, rv=1, iat=0)
    admin = Actor(user_id=uuid.uuid7(), org_id=uuid.uuid7(), role="superadmin", role_id=None, rv=1, iat=0)
    refs = await files.refs([document.id, public.id, pending.file_id, None], viewer=owner)
    assert refs[document.id].url.startswith("https://storage.example.uz/p/") and refs[document.id].expires_at
    assert refs[public.id].url == f"https://storage.example.uz/pub/{public.key}" and refs[public.id].expires_at is None
    assert refs[pending.file_id].url is None
    assert (await files.db.execute(select(FileAccessLog))).scalars().all() == []

    await files.refs([document.id], viewer=admin, reason="verification_review")
    logged = (await files.db.execute(select(FileAccessLog))).scalars().all()
    assert [(row.actor_id, row.reason) for row in logged] == [(admin.user_id, "verification_review")]


async def test_sweep_removes_orphans_but_not_used_retained_or_held_files(files, storage):
    old = now_utc() - timedelta(days=2)
    stale_pending = await uploaded(files, storage)
    never_attached = await uploaded(files, storage)
    in_use = await uploaded(files, storage)
    released = await uploaded(files, storage)
    retained = await uploaded(files, storage, kind="trip_photo")
    held = await uploaded(files, storage)
    fresh = await uploaded(files, storage)
    for record in (never_attached, in_use, released, retained, held, fresh):
        await files.complete(record.id, ORG)
    for record in (in_use, released, retained):
        await files.attach(record.id, ORG, kinds=(record.kind,))
    await files.detach(released.id, retained.id)
    for record in (stale_pending, never_attached, in_use, released, retained, held):
        record.created_at = old
    released.detached_at = retained.detached_at = old
    held.legal_hold = True
    await files.db.flush()

    assert await files.sweep() == {"removed": 3}
    statuses = {r.id: r.status for r in (await files.db.execute(select(FileRecord))).scalars()}
    assert [statuses[r.id] for r in (stale_pending, never_attached, released)] == [FileStatus.DELETED] * 3
    assert all(statuses[r.id] != FileStatus.DELETED for r in (in_use, retained, held, fresh))
    assert (released.bucket, released.key) not in storage.objects


async def test_sweep_keeps_the_row_when_storage_is_down(files, storage):
    record = await uploaded(files, storage)
    record.created_at = now_utc() - timedelta(days=2)
    storage.fail_deletes = True
    assert await files.sweep() == {"removed": 0}
    assert record.status == FileStatus.PENDING


async def test_purge_org_deletes_everything_except_retained_and_held(files, storage):
    plain = await uploaded(files, storage)
    retained = await uploaded(files, storage, kind="trip_photo")
    other_org = await uploaded(files, storage, org=uuid.uuid7())
    await files.complete(retained.id, ORG)
    await files.attach(retained.id, ORG, kinds=("trip_photo",))
    assert await files.purge_org(ORG) == 1
    remaining = {r.id for r in (await files.db.execute(select(FileRecord))).scalars()}
    assert remaining == {retained.id, other_org.id}
    assert await files.purge_org(ORG, keep_retained=False) == 1
    assert (plain.bucket, plain.key) not in storage.objects


async def test_delete_unattached_refuses_files_in_use(files, storage):
    record = await uploaded(files, storage)
    await files.attach(record.id, ORG, kinds=("avatar",))
    with pytest.raises(Conflict):
        await files.delete_unattached(record.id, ORG)
    await files.detach(record.id)
    await files.delete_unattached(record.id, ORG)
    assert record.status == FileStatus.DELETED
    with pytest.raises(NotFound):
        await files.get_owned(record.id, ORG)


async def test_router_upload_complete_get_and_delete(db, storage):
    auth.configure_auth(jwt_secret="j" * 32)
    app = FastAPI()
    install_handlers(app)
    service = FileService(db, storage, KINDS, service="auth")
    app.include_router(files_router(lambda: service, KINDS))
    token = auth.mint_access_token(user_id=uuid.uuid7(), org_id=ORG, role="carrier", role_id=uuid.uuid7(), rv=1)
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        bad = await client.post(
            "/v1/files/uploads", json={"kind": "nope", "content_type": "image/jpeg", "size_bytes": 1}, headers=headers
        )
        assert bad.status_code == 422
        started = await client.post(
            "/v1/files/uploads",
            json={"kind": "avatar", "content_type": "image/jpeg", "size_bytes": len(JPEG), "name": "a.jpg"},
            headers=headers,
        )
        assert started.status_code == 201 and started.json()["method"] == "PUT"
        file_id = started.json()["file_id"]
        storage.put(await db.get(FileRecord, uuid.UUID(file_id)), JPEG)
        done = await client.post(f"/v1/files/{file_id}/complete", headers=headers)
        assert done.json()["status"] == "ready" and done.json()["url"]
        assert (await client.get(f"/v1/files/{file_id}", headers=headers)).json()["name"] == "a.jpg"
        assert (await client.delete(f"/v1/files/{file_id}", headers=headers)).status_code == 204
        assert (await client.get(f"/v1/files/{file_id}", headers=headers)).status_code == 404


async def test_a_public_file_reaches_the_public_bucket_only_after_its_check(files, storage):
    good = await uploaded(files, storage, kind="reference_image")
    bad = await uploaded(files, storage, kind="reference_image", body=b"<svg>" + b"\0" * 95)

    assert (good.bucket, bad.bucket) == ("m", "m"), "uploads land in the media bucket, not where anyone can read"
    assert str(ORG) not in good.key

    await files.complete(good.id, ORG)
    await files.complete(bad.id, ORG)

    assert good.status == FileStatus.READY and good.bucket == "pub"
    assert storage.objects.get(("pub", good.key)) == JPEG and ("m", good.key) not in storage.objects
    assert bad.status == FileStatus.REJECTED and ("pub", bad.key) not in storage.objects
    assert files.ref(good).url == f"https://storage.example.uz/pub/{good.key}"


async def test_files_of_a_kind_that_became_public_are_moved_there(files, storage, db):
    old = await uploaded(files, storage, kind="avatar")
    await files.complete(old.id, ORG)
    assert old.bucket == "m" and files.ref(old).expires_at is not None

    public = FileService(
        db, storage, {**KINDS, "avatar": FileKind("avatar", StorageClass.PUBLIC, 5_000, IMAGE_TYPES)}, service="auth"
    )
    assert await public.move_to_declared_class("avatar") == 1
    assert await public.move_to_declared_class("avatar") == 0

    assert (old.bucket, old.storage_class) == ("pub", StorageClass.PUBLIC)
    assert storage.objects.get(("pub", old.key)) == JPEG and ("m", old.key) not in storage.objects
    assert public.ref(old).expires_at is None


async def test_operators_list_every_file_but_open_a_personal_document_one_at_a_time(files, storage):
    from goway_lib.pagination import PageParams

    document = await uploaded(files, storage, kind="identity_document", body=PDF, content_type="application/pdf")
    photo = await uploaded(files, storage, kind="trip_photo")
    other_org = await uploaded(files, storage, kind="avatar", org=uuid.uuid7())
    for record in (document, photo):
        await files.complete(record.id, ORG)
    await files.complete(other_org.id, other_org.org_id)
    await files.attach(photo.id, ORG, kinds=("trip_photo",))
    admin = Actor(user_id=uuid.uuid7(), org_id=uuid.uuid7(), role="superadmin", role_id=None, rv=1, iat=0)

    listed = await files.admin_page(PageParams())
    rows = {row.id: row for row in listed.items}
    assert listed.page.total == 3 and set(rows) == {document.id, photo.id, other_org.id}
    assert rows[document.id].sensitive and rows[document.id].url is None, "a list never hands out a document"
    assert rows[photo.id].url and rows[photo.id].attached and not rows[document.id].attached
    assert (await files.db.execute(select(FileAccessLog))).scalars().all() == []

    assert [row.id for row in (await files.admin_page(PageParams(), kind="trip_photo")).items] == [photo.id]
    assert [row.id for row in (await files.admin_page(PageParams(), org_id=ORG, attached=False)).items] == [document.id]

    opened = await files.admin_view(document.id, admin)
    assert opened.url and opened.url.startswith("https://storage.example.uz/p/")
    logged = (await files.db.execute(select(FileAccessLog))).scalars().all()
    assert [(row.file_id, row.reason) for row in logged] == [(document.id, "admin_files")]
    await files.admin_view(photo.id, admin)
    assert len((await files.db.execute(select(FileAccessLog))).scalars().all()) == 1, "only documents are logged"

    summary = {row.kind: row for row in await files.admin_summary()}
    assert set(summary) == set(KINDS)
    assert (summary["trip_photo"].count, summary["trip_photo"].bytes) == (1, len(JPEG))
    assert summary["identity_document"].sensitive and summary["reference_image"].count == 0


async def test_the_files_page_api_is_for_operators_only(db, storage):
    from goway_lib.files import files_admin_router

    auth.configure_auth(jwt_secret="j" * 32)
    app = FastAPI()
    install_handlers(app)
    service = FileService(db, storage, KINDS, service="auth")
    app.include_router(files_admin_router(lambda: service, KINDS))
    photo = await uploaded(service, storage, kind="trip_photo")
    await service.complete(photo.id, ORG)

    def headers(role: str) -> dict:
        token = auth.mint_access_token(user_id=uuid.uuid7(), org_id=uuid.uuid7(), role=role, role_id=uuid.uuid7(), rv=1)
        return {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/admin/files", headers=headers("carrier"))).status_code == 403
        operator = headers("superadmin")
        summary = await client.get("/admin/files/summary", headers=operator)
        assert summary.status_code == 200 and {row["kind"] for row in summary.json()["items"]} == set(KINDS)
        listed = await client.get("/admin/files", params={"kind": "trip_photo"}, headers=operator)
        assert listed.status_code == 200 and [row["id"] for row in listed.json()["items"]] == [str(photo.id)]
        assert (await client.get("/admin/files", params={"kind": "nope"}, headers=operator)).status_code == 422
        opened = await client.get(f"/admin/files/{photo.id}", headers=operator)
        assert opened.status_code == 200 and opened.json()["url"]
