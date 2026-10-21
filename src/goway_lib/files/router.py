import uuid
from collections.abc import Callable, Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import Field, create_model

from goway_lib.auth import CurrentActor, Requires
from goway_lib.errors import problem_responses
from goway_lib.files.kinds import FileKind
from goway_lib.files.models import FileStatus
from goway_lib.files.schemas import AdminFileRead, FileKindSummary, FileRef, UploadRead
from goway_lib.files.service import FileService
from goway_lib.pagination import OffsetPage, Page, PageInfo, PageQuery


def files_router(service_dependency: Callable[..., FileService], kinds: Mapping[str, FileKind]) -> APIRouter:
    """Upload endpoints under ``/v1/files``, identical in every service that stores files."""
    router = APIRouter(prefix="/v1/files", tags=["Files"])
    Files = Annotated[FileService, Depends(service_dependency)]
    UploadIn = create_model(
        "UploadIn",
        kind=(Literal[tuple(kinds)], Field(description="What the file is for; decides size, types and access.")),
        content_type=(str, Field(max_length=100, examples=["image/jpeg"])),
        size_bytes=(int, Field(gt=0, description="Exact length of the file in bytes.")),
        name=(str | None, Field(default=None, max_length=255, description="Original file name, optional.")),
    )

    @router.post(
        "/uploads",
        status_code=status.HTTP_201_CREATED,
        summary="Start an upload",
        description=(
            "Registers the file and returns a presigned PUT URL. Send the body there directly — it never passes "
            "through the API — with the returned headers and exactly `size_bytes` bytes, then call `complete`. "
            "Accepts `Idempotency-Key`: a retry gets the same file and URL."
        ),
        responses=problem_responses(400, 401, 422),
    )
    async def start_upload(body: UploadIn, actor: CurrentActor, files: Files) -> UploadRead:
        """Get a presigned PUT URL for a new file."""
        return await files.create_upload(
            org_id=actor.org_id,
            created_by=actor.user_id,
            kind=body.kind,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            name=body.name,
        )

    @router.post(
        "/{file_id}/complete",
        summary="Finish an upload",
        description=(
            "Checks the stored object: exact size and the real content type read from its first bytes. Returns the "
            "file as `ready`, or `rejected` with `reject_reason` (the object is removed). Idempotent."
        ),
        responses=problem_responses(400, 401, 404),
    )
    async def complete_upload(file_id: uuid.UUID, actor: CurrentActor, files: Files) -> FileRef:
        """Confirm an upload finished."""
        return files.ref(await files.complete(file_id, actor.org_id))

    @router.get(
        "/{file_id}",
        summary="Get file",
        description="The file with a fresh view URL. Only the owning organization.",
        responses=problem_responses(401, 404),
    )
    async def get_file(file_id: uuid.UUID, actor: CurrentActor, files: Files) -> FileRef:
        """Read one of the organization's files."""
        return files.ref(await files.get_owned(file_id, actor.org_id))

    @router.delete(
        "/{file_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete unused file",
        description="Removes an uploaded file that nothing references yet. Attached files answer 409 FILE_IN_USE.",
        responses=problem_responses(401, 404, 409),
    )
    async def delete_file(file_id: uuid.UUID, actor: CurrentActor, files: Files) -> None:
        """Delete a file that is not in use."""
        await files.delete_unattached(file_id, actor.org_id)

    return router


def files_admin_router(service_dependency: Callable[..., FileService], kinds: Mapping[str, FileKind]) -> APIRouter:
    """Every file a service stores, for the operators' Files page, under ``/admin/files``. Superadmin only."""
    router = APIRouter(prefix="/admin/files", tags=["Admin"], dependencies=[Depends(Requires("superadmin"))])
    Files = Annotated[FileService, Depends(service_dependency)]
    Kind = Literal[tuple(kinds)]

    @router.get(
        "/summary",
        summary="Files by kind",
        description="Each kind the service stores: whether it is a personal document, how many files, their size.",
        responses=problem_responses(401, 403),
    )
    async def summary(files: Files) -> Page[FileKindSummary]:
        """Count and size per file kind."""
        return Page(items=await files.admin_summary(), page=PageInfo())

    @router.get(
        "",
        summary="List files",
        description=(
            "Files newest first, deleted ones only when `status=deleted`. Each has a view `url` except personal "
            "documents (`sensitive`), whose address comes only from `GET /admin/files/{id}` so the view is logged."
        ),
        responses=problem_responses(401, 403, 422),
    )
    async def list_files(
        files: Files,
        pages: PageQuery,
        kind: Annotated[Kind | None, Query(description="One file kind.")] = None,
        status_filter: Annotated[FileStatus | None, Query(alias="status", description="One upload state.")] = None,
        org_id: Annotated[uuid.UUID | None, Query(description="One organization's files.")] = None,
        attached: Annotated[bool | None, Query(description="Used by a record (true) or by nothing (false).")] = None,
    ) -> OffsetPage[AdminFileRead]:
        """One page of files with what an operator needs to find one."""
        return await files.admin_page(pages, kind=kind, status=status_filter, org_id=org_id, attached=attached)

    AdminUploadIn = create_model(
        "AdminUploadIn",
        org_id=(uuid.UUID, Field(description="The organization the file is uploaded for; it owns the file.")),
        kind=(Kind, Field(description="What the file is for; decides size, types and access.")),
        content_type=(str, Field(max_length=100, examples=["image/jpeg"])),
        size_bytes=(int, Field(gt=0, description="Exact length of the file in bytes.")),
        name=(str | None, Field(default=None, max_length=255, description="Original file name, optional.")),
    )
    HoldIn = create_model(
        "HoldIn", hold=(bool, Field(description="Keep the file past its retention (true) or let it go (false)."))
    )

    @router.post(
        "/uploads",
        status_code=status.HTTP_201_CREATED,
        summary="Upload for an organization",
        description=(
            "An operator's upload on someone's behalf: the file belongs to `org_id`, the operator is its author. "
            "Then PUT the bytes to `url` and call `complete`, as in the apps. Accepts `Idempotency-Key`."
        ),
        responses=problem_responses(400, 401, 403, 422),
    )
    async def operator_upload(body: AdminUploadIn, actor: CurrentActor, files: Files) -> UploadRead:
        """Get a presigned PUT URL for a file uploaded on an organization's behalf."""
        return await files.create_upload(
            org_id=body.org_id,
            created_by=actor.user_id,
            kind=body.kind,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            name=body.name,
        )

    @router.post(
        "/{file_id}/complete",
        summary="Finish an operator's upload",
        description="Checks the stored object like the apps' `complete`: `ready`, or `rejected` with the reason.",
        responses=problem_responses(400, 401, 403, 404),
    )
    async def operator_complete(file_id: uuid.UUID, actor: CurrentActor, files: Files) -> AdminFileRead:
        """Confirm an operator's upload finished."""
        return await files.admin_complete(file_id, actor)

    @router.delete(
        "/{file_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete file",
        description=(
            "Removes a file nothing uses. `FILE_IN_USE` (409) when a record uses it; `FILE_RETAINED` (409) while it "
            "is kept as evidence (`params.legal_hold`, `params.retain_until`). Logged with the operator."
        ),
        responses=problem_responses(401, 403, 404, 409, 502),
    )
    async def delete_file(file_id: uuid.UUID, actor: CurrentActor, files: Files) -> None:
        """Delete a file nothing uses."""
        await files.admin_delete(file_id, actor)

    @router.put(
        "/{file_id}/hold",
        summary="Hold or release",
        description=(
            "A legal hold keeps the file past its retention and from any deletion until an operator releases it. "
            "Logged with the operator. `FILE_DELETED` (409) for a file already gone."
        ),
        responses=problem_responses(401, 403, 404, 409, 422),
    )
    async def hold_file(file_id: uuid.UUID, body: HoldIn, actor: CurrentActor, files: Files) -> AdminFileRead:
        """Put a legal hold on a file or release it."""
        return await files.admin_hold(file_id, actor, hold=body.hold)

    @router.get(
        "/{file_id}",
        summary="Open file",
        description="The file with a fresh view URL. Opening someone's personal document is logged.",
        responses=problem_responses(401, 403, 404),
    )
    async def open_file(file_id: uuid.UUID, actor: CurrentActor, files: Files) -> AdminFileRead:
        """One file with its address."""
        return await files.admin_view(file_id, actor)

    return router
