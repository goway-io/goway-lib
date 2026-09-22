import uuid
from collections.abc import Callable, Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import Field, create_model

from goway_lib.auth import CurrentActor
from goway_lib.errors import problem_responses
from goway_lib.files.kinds import FileKind
from goway_lib.files.schemas import FileRef, UploadRead
from goway_lib.files.service import FileService


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
