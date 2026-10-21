"""File lifecycle shared by every service that stores files.

    pending ──PUT + complete──► ready ──attach──► ref_count ≥ 1 ──detach──► ref_count = 0
       │                          │                                            │
       └─ never uploaded (24 h) ──┴─ rejected by the content check ────────────┴─► sweep ─► deleted

Storage is only touched outside the database transaction's critical path: a failed delete is
logged and retried by the next sweep instead of failing the request that caused it.

A public file is uploaded into the media bucket first and copied to the public bucket only once the content
check passes, so bytes nobody has checked are never served to the world, and a browser upload meets the
media bucket's CORS rules. Its key carries no org id, because the address is public.
"""

import logging
import uuid
from collections.abc import Awaitable, Iterable, Mapping
from datetime import UTC, timedelta
from typing import TypeVar

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from goway_lib.auth import Actor
from goway_lib.errors import BadRequest, Conflict, NotFound, UpstreamUnavailable, or_404
from goway_lib.files.kinds import FileKind, sniff
from goway_lib.files.models import FileAccessLog, FileRecord, FileStatus
from goway_lib.files.schemas import AdminFileRead, FileKindSummary, FileRef, UploadRead
from goway_lib.pagination import OffsetPage, PageParams, paginate_offset
from goway_lib.storage import STORAGE_ERRORS, Storage, StorageClass
from goway_lib.timeutils import now_utc

logger = logging.getLogger(__name__)

T = TypeVar("T")

UPLOAD_TTL_SECONDS = 900
VIEW_TTL_SECONDS = 600
UNUSED_AFTER = timedelta(hours=24)
DETACH_GRACE = timedelta(days=1)
SNIFF_BYTES = 64


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:255] or None


async def release(db: AsyncSession, *file_ids: uuid.UUID | None) -> None:
    """Drops one reference from each file. Needs only the database, so code without a storage
    client — account deletion, event handlers — can let go of files too."""
    ids = list({f for f in file_ids if f is not None})
    if not ids:
        return
    await db.execute(
        update(FileRecord)
        .where(FileRecord.id.in_(ids), FileRecord.ref_count > 0)
        .values(ref_count=FileRecord.ref_count - 1)
    )
    await db.execute(
        update(FileRecord)
        .where(FileRecord.id.in_(ids), FileRecord.ref_count == 0, FileRecord.detached_at.is_(None))
        .values(detached_at=now_utc())
    )


class FileService:
    def __init__(self, db: AsyncSession, storage: Storage, kinds: Mapping[str, FileKind], *, service: str):
        self.db = db
        self.storage = storage
        self.kinds = kinds
        self.service = service

    def kind(self, code: str) -> FileKind:
        spec = self.kinds.get(code)
        if spec is None:
            raise BadRequest(
                "Unknown file kind",
                code="FILE_KIND_INVALID",
                key="files.kind_invalid",
                params={"allowed": sorted(self.kinds)},
            )
        return spec

    async def create_upload(
        self,
        *,
        org_id: uuid.UUID,
        created_by: uuid.UUID | None,
        kind: str,
        content_type: str,
        size_bytes: int,
        name: str | None = None,
    ) -> UploadRead:
        spec = self.kind(kind)
        if content_type not in spec.types:
            raise BadRequest(
                "Content type not allowed for this kind",
                code="FILE_TYPE_INVALID",
                key="files.type_invalid",
                params={"allowed": sorted(spec.types)},
            )
        if not 0 < size_bytes <= spec.max_bytes:
            raise BadRequest(
                "File too large", code="FILE_TOO_LARGE", key="files.too_large", params={"max_bytes": spec.max_bytes}
            )
        now = now_utc()
        file_id = uuid.uuid7()
        public = spec.storage_class is StorageClass.PUBLIC
        bucket = self.storage.bucket(StorageClass.MEDIA if public else spec.storage_class)
        owner = "" if public else f"{org_id}/"
        key = f"{self.service}/{spec.code}/{owner}{now:%Y/%m}/{file_id}"
        self.db.add(
            FileRecord(
                id=file_id,
                org_id=org_id,
                created_by=created_by,
                kind=spec.code,
                storage_class=spec.storage_class,
                bucket=bucket,
                key=key,
                original_name=_clean_name(name),
                declared_type=content_type,
                size_bytes=size_bytes,
            )
        )
        await self.db.flush()
        url = self.storage.presign_put(
            bucket, key, content_type=content_type, content_length=size_bytes, expires_in=UPLOAD_TTL_SECONDS
        )
        return UploadRead(
            file_id=file_id,
            url=url,
            headers={"Content-Type": content_type},
            expires_at=now + timedelta(seconds=UPLOAD_TTL_SECONDS),
        )

    async def get_owned(self, file_id: uuid.UUID, org_id: uuid.UUID) -> FileRecord:
        record = await self.db.get(FileRecord, file_id)
        if record is None or record.org_id != org_id or record.status == FileStatus.DELETED:
            raise NotFound("file not found", key="common.not_found", params={"resource": "file"})
        return record

    async def complete(self, file_id: uuid.UUID, org_id: uuid.UUID) -> FileRecord:
        """Checks the stored object and returns the file as ``ready`` or ``rejected``. A rejection is
        a result, not an error, so it is committed with the request instead of rolled back."""
        record = await self.get_owned(file_id, org_id)
        if record.status in (FileStatus.UPLOADED, FileStatus.READY, FileStatus.REJECTED):
            return record
        info = await self._storage(self.storage.head(record.bucket, record.key))
        if info is None:
            raise BadRequest("File is not uploaded yet", code="FILE_NOT_UPLOADED", key="files.not_uploaded")
        spec = self.kinds.get(record.kind)
        detected = sniff(await self._storage(self.storage.read_prefix(record.bucket, record.key, SNIFF_BYTES)))
        now = now_utc()
        if info.size != record.size_bytes:
            reason = "size_mismatch"
        elif spec is None or detected not in spec.types:
            reason = "type_mismatch"
        else:
            reason = None
        if reason is not None:
            await self._remove_object(record)
            record.status = FileStatus.REJECTED
            record.reject_reason = reason
        else:
            await self._place(record, record.storage_class)
            record.status = FileStatus.READY
            record.detected_type = detected
            record.uploaded_at = record.ready_at = now
        await self.db.flush()
        return record

    async def _place(self, record: FileRecord, storage_class: StorageClass) -> None:
        """Moves the object into the bucket of its class; the old copy goes only after the new one exists."""
        target = self.storage.bucket(storage_class)
        if record.bucket != target:
            source = record.bucket
            await self._storage(self.storage.copy(source, record.key, target))
            record.bucket = target
            try:
                await self.storage.delete(source, record.key)
            except STORAGE_ERRORS:
                logger.warning("could not remove %s/%s after moving it", source, record.key, exc_info=True)
        record.storage_class = storage_class

    async def move_to_declared_class(self, kind: str, *, limit: int = 500) -> int:
        """For a kind whose storage class changed: moves its ready files where the kind now says they live."""
        spec = self.kind(kind)
        query = (
            select(FileRecord)
            .where(
                FileRecord.kind == spec.code,
                FileRecord.status == FileStatus.READY,
                FileRecord.storage_class != spec.storage_class,
            )
            .order_by(FileRecord.created_at)
            .limit(limit)
        )
        moved = 0
        for record in (await self.db.execute(query)).scalars().all():
            await self._place(record, spec.storage_class)
            moved += 1
        await self.db.flush()
        return moved

    async def attach(self, file_id: uuid.UUID, org_id: uuid.UUID, *, kinds: Iterable[str]) -> FileRecord:
        """Counts one more domain reference to the file. Completes a pending upload on the way, so a
        client that skipped ``complete`` still gets the content check."""
        record = await self.db.get(FileRecord, file_id)
        if record is None or record.org_id != org_id or record.kind not in set(kinds):
            raise BadRequest(
                "File not found for this account",
                code="FILE_INVALID",
                key="files.invalid",
                params={"file_id": str(file_id)},
            )
        if record.status == FileStatus.PENDING:
            await self.complete(file_id, org_id)
        if record.status != FileStatus.READY:
            raise BadRequest(
                "File is not usable",
                code="FILE_INVALID",
                key="files.invalid",
                params={"file_id": str(file_id), "status": record.status, "reason": record.reject_reason},
            )
        values = {"ref_count": FileRecord.ref_count + 1, "detached_at": None}
        if record.attached_at is None:
            now = now_utc()
            values["attached_at"] = now
            spec = self.kinds.get(record.kind)
            if spec is not None and spec.retain_days:
                values["retain_until"] = now + timedelta(days=spec.retain_days)
        await self.db.execute(update(FileRecord).where(FileRecord.id == file_id).values(**values))
        await self.db.refresh(record)
        return record

    async def detach(self, *file_ids: uuid.UUID | None) -> None:
        await release(self.db, *file_ids)

    async def replace(
        self, current: Iterable[uuid.UUID], wanted: Iterable[uuid.UUID], org_id: uuid.UUID, *, kinds: Iterable[str]
    ) -> list[uuid.UUID]:
        """Moves a set of references from ``current`` to ``wanted``, keeping order and dropping repeats."""
        kinds = tuple(kinds)
        current = list(current)
        wanted = list(dict.fromkeys(wanted))
        for file_id in wanted:
            if file_id not in current:
                await self.attach(file_id, org_id, kinds=kinds)
        await self.detach(*[f for f in current if f not in wanted])
        return wanted

    def ref(self, record: FileRecord) -> FileRef:
        url = expires_at = None
        content_type = record.detected_type or record.declared_type
        if record.status == FileStatus.READY:
            if record.storage_class == StorageClass.PUBLIC:
                url = self.storage.public_url(record.key)
            else:
                url = self.storage.presign_get(
                    record.bucket,
                    record.key,
                    expires_in=VIEW_TTL_SECONDS,
                    content_type=content_type,
                    filename=record.original_name,
                )
                expires_at = now_utc() + timedelta(seconds=VIEW_TTL_SECONDS)
        return FileRef(
            id=record.id,
            kind=record.kind,
            status=record.status,
            content_type=content_type,
            size_bytes=record.size_bytes,
            name=record.original_name,
            width=record.width,
            height=record.height,
            url=url,
            expires_at=expires_at,
            reject_reason=record.reject_reason,
        )

    async def refs(
        self, file_ids: Iterable[uuid.UUID | None], *, viewer: Actor | None = None, reason: str = "view"
    ) -> dict[uuid.UUID, FileRef]:
        """Batch view refs. A sensitive file handed to someone outside its organization is logged."""
        ids = list({f for f in file_ids if f is not None})
        if not ids:
            return {}
        records = (await self.db.execute(select(FileRecord).where(FileRecord.id.in_(ids)))).scalars().all()
        result = {}
        for record in records:
            result[record.id] = self.ref(record)
            spec = self.kinds.get(record.kind)
            if (
                viewer is not None
                and spec is not None
                and spec.sensitive
                and record.org_id != viewer.org_id
                and record.status == FileStatus.READY
            ):
                self.db.add(
                    FileAccessLog(file_id=record.id, actor_id=viewer.user_id, actor_role=viewer.role, reason=reason)
                )
        return result

    def admin_ref(self, record: FileRecord, *, with_url: bool) -> AdminFileRead:
        """The file for an operator. A personal document keeps its address out of lists: it is handed over only
        when the operator opens that one file, so the access log records views, not page loads."""
        spec = self.kinds.get(record.kind)
        sensitive = bool(spec and spec.sensitive)
        ref = self.ref(record)
        if sensitive and not with_url:
            ref = ref.model_copy(update={"url": None, "expires_at": None})
        return AdminFileRead(
            **ref.model_dump(),
            org_id=record.org_id,
            created_by=record.created_by,
            created_at=record.created_at,
            attached=record.ref_count > 0,
            sensitive=sensitive,
            retain_until=record.retain_until,
            legal_hold=record.legal_hold,
        )

    async def admin_page(
        self,
        pages: PageParams,
        *,
        kind: str | None = None,
        status: FileStatus | None = None,
        org_id: uuid.UUID | None = None,
        attached: bool | None = None,
    ) -> OffsetPage[AdminFileRead]:
        """Every file the service stores, newest first; deleted ones only when asked for by status."""
        where = [FileRecord.status == status] if status else [FileRecord.status != FileStatus.DELETED]
        if kind:
            where.append(FileRecord.kind == kind)
        if org_id:
            where.append(FileRecord.org_id == org_id)
        if attached is not None:
            where.append(FileRecord.ref_count > 0 if attached else FileRecord.ref_count == 0)
        return await paginate_offset(
            self.db,
            select(FileRecord).where(*where),
            number=pages.number,
            size=pages.size,
            order_by=[FileRecord.created_at.desc(), FileRecord.id.desc()],
            transformer=lambda record: self.admin_ref(record, with_url=False),
        )

    async def admin_view(self, file_id: uuid.UUID, viewer: Actor) -> AdminFileRead:
        """One file with its address. Opening someone's personal document is logged, as on the review page."""
        record = or_404(await self.db.get(FileRecord, file_id), "file")
        spec = self.kinds.get(record.kind)
        if spec and spec.sensitive and record.status == FileStatus.READY and record.org_id != viewer.org_id:
            self.db.add(
                FileAccessLog(file_id=record.id, actor_id=viewer.user_id, actor_role=viewer.role, reason="admin_files")
            )
        return self.admin_ref(record, with_url=True)

    def _operator_did(self, record: FileRecord, viewer: Actor, what: str) -> None:
        """An operator's hand on someone's file, kept beside the views of it."""
        self.db.add(FileAccessLog(file_id=record.id, actor_id=viewer.user_id, actor_role=viewer.role, reason=what))

    async def admin_complete(self, file_id: uuid.UUID, viewer: Actor) -> AdminFileRead:
        """Finishes an upload an operator made for an organization, whose org the file belongs to."""
        record = or_404(await self.db.get(FileRecord, file_id), "file")
        record = await self.complete(record.id, record.org_id)
        if record.status == FileStatus.READY:
            self._operator_did(record, viewer, "admin_upload")
        return self.admin_ref(record, with_url=False)

    async def admin_delete(self, file_id: uuid.UUID, viewer: Actor) -> None:
        """Removes a file nothing uses. One a record uses, or one kept as evidence, stays."""
        record = or_404(await self.db.get(FileRecord, file_id), "file")
        if record.status == FileStatus.DELETED:
            return
        if record.ref_count > 0:
            raise Conflict("File is in use", code="FILE_IN_USE", key="files.in_use")
        until = record.retain_until
        until = until.replace(tzinfo=UTC) if until is not None and until.tzinfo is None else until
        retained = until is not None and until > now_utc()
        if record.legal_hold or retained:
            raise Conflict(
                "The file is kept as evidence",
                code="FILE_RETAINED",
                key="files.retained",
                params={
                    "legal_hold": record.legal_hold,
                    "retain_until": until.isoformat() if retained and until else None,
                },
            )
        if not await self._remove_object(record):
            raise UpstreamUnavailable("Storage unavailable", params={"service": "storage"})
        record.status = FileStatus.DELETED
        record.deleted_at = now_utc()
        self._operator_did(record, viewer, "admin_delete")
        await self.db.flush()

    async def admin_hold(self, file_id: uuid.UUID, viewer: Actor, *, hold: bool) -> AdminFileRead:
        """Keeps a file past its retention, whatever happens to the record that uses it, until released."""
        record = or_404(await self.db.get(FileRecord, file_id), "file")
        if record.status == FileStatus.DELETED:
            raise Conflict("The file is deleted", code="FILE_DELETED", key="files.deleted")
        if record.legal_hold != hold:
            record.legal_hold = hold
            self._operator_did(record, viewer, "admin_hold" if hold else "admin_release")
            await self.db.flush()
        return self.admin_ref(record, with_url=False)

    async def admin_summary(self) -> list[FileKindSummary]:
        """Each kind the service stores, with how many files of it are kept and how much room they take."""
        q = (
            select(FileRecord.kind, func.count(), func.coalesce(func.sum(FileRecord.size_bytes), 0))
            .where(FileRecord.status != FileStatus.DELETED)
            .group_by(FileRecord.kind)
        )
        found = {kind: (count, int(size)) for kind, count, size in await self.db.execute(q)}
        return [
            FileKindSummary(
                kind=code, sensitive=spec.sensitive, count=found.get(code, (0, 0))[0], bytes=found.get(code, (0, 0))[1]
            )
            for code, spec in self.kinds.items()
        ]

    async def delete_unattached(self, file_id: uuid.UUID, org_id: uuid.UUID) -> None:
        record = await self.get_owned(file_id, org_id)
        if record.ref_count > 0:
            raise Conflict("File is in use", code="FILE_IN_USE", key="files.in_use")
        if not await self._remove_object(record):
            raise UpstreamUnavailable("Storage unavailable", params={"service": "storage"})
        record.status = FileStatus.DELETED
        record.deleted_at = now_utc()
        await self.db.flush()

    async def sweep(self, *, limit: int = 500) -> dict:
        """Removes files nobody references: never used within a day, or released more than a day ago
        and past any retention. Legal holds are never touched."""
        now = now_utc()
        query = (
            select(FileRecord)
            .where(
                FileRecord.status != FileStatus.DELETED,
                FileRecord.ref_count == 0,
                FileRecord.legal_hold.is_(False),
                or_(FileRecord.retain_until.is_(None), FileRecord.retain_until <= now),
                or_(
                    and_(FileRecord.attached_at.is_(None), FileRecord.created_at <= now - UNUSED_AFTER),
                    FileRecord.detached_at <= now - DETACH_GRACE,
                ),
            )
            .order_by(FileRecord.created_at)
            .limit(limit)
        )
        removed = 0
        for record in (await self.db.execute(query)).scalars().all():
            if await self._remove_object(record):
                record.status = FileStatus.DELETED
                record.deleted_at = now
                removed += 1
        await self.db.flush()
        return {"removed": removed}

    async def purge_org(self, org_id: uuid.UUID, *, keep_retained: bool = True) -> int:
        """Hard delete of an organization's files, objects and rows. Callers remove the domain rows
        that reference the files first. Retained and held files stay until their time is up."""
        conditions = [FileRecord.org_id == org_id, FileRecord.legal_hold.is_(False)]
        if keep_retained:
            conditions.append(or_(FileRecord.retain_until.is_(None), FileRecord.retain_until <= now_utc()))
        records = (await self.db.execute(select(FileRecord).where(*conditions))).scalars().all()
        purged = 0
        for record in records:
            if record.status != FileStatus.DELETED:
                await self._remove_object(record)
            await self.db.delete(record)
            purged += 1
        await self.db.flush()
        return purged

    async def _remove_object(self, record: FileRecord) -> bool:
        try:
            await self.storage.delete(record.bucket, record.key)
        except STORAGE_ERRORS:
            logger.warning("could not remove %s/%s from storage", record.bucket, record.key, exc_info=True)
            return False
        return True

    @staticmethod
    async def _storage(call: Awaitable[T]) -> T:
        try:
            return await call
        except STORAGE_ERRORS as exc:
            raise UpstreamUnavailable("Storage unavailable", params={"service": "storage"}) from exc
