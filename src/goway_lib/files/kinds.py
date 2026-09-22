"""What a file kind allows and how long it lives. A kind is a storage policy, not a business
meaning: a passport and a tech passport are both ``identity_document``; the domain row that
references the file says which paper it is."""

from dataclasses import dataclass

from goway_lib.storage import StorageClass

IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
DOCUMENT_TYPES = IMAGE_TYPES | {"application/pdf"}


@dataclass(frozen=True)
class FileKind:
    code: str
    storage_class: StorageClass
    max_bytes: int
    types: frozenset[str]
    sensitive: bool = False
    retain_days: int | None = None


def kind_map(*kinds: FileKind) -> dict[str, FileKind]:
    return {kind.code: kind for kind in kinds}


def sniff(head: bytes) -> str | None:
    """The real type from the first bytes. Declared content types are never trusted for serving."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
    return None
