from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import UUID, uuid4

from google.cloud import storage


MAX_CATALOG_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_CATALOG_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class CatalogMediaConfigurationError(RuntimeError):
    """Catalog media storage is not configured."""


class CatalogMediaValidationError(ValueError):
    """Catalog image did not pass safe validation."""


class CatalogMediaUploadError(RuntimeError):
    """Catalog image could not be persisted."""


@dataclass(frozen=True, slots=True)
class CatalogImageUpload:
    url: str
    object_name: str


def validate_catalog_image(*, content: bytes, content_type: str | None) -> str:
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    extension = ALLOWED_CATALOG_IMAGE_TYPES.get(normalized_type)
    if extension is None:
        raise CatalogMediaValidationError("Use uma imagem JPG, PNG ou WebP.")
    if not content:
        raise CatalogMediaValidationError("A imagem está vazia.")
    if len(content) > MAX_CATALOG_IMAGE_BYTES:
        raise CatalogMediaValidationError("A imagem deve ter no máximo 5 MB.")
    return extension


def upload_catalog_image(
    *,
    bucket_name: str,
    business_id: UUID,
    item_id: UUID,
    content: bytes,
    content_type: str | None,
) -> CatalogImageUpload:
    name = bucket_name.strip()
    if not name:
        raise CatalogMediaConfigurationError("CATALOG_MEDIA_BUCKET is not configured")

    extension = validate_catalog_image(content=content, content_type=content_type)
    object_name = str(
        PurePosixPath(
            "catalog",
            str(business_id),
            str(item_id),
            f"{uuid4().hex}{extension}",
        )
    )
    try:
        client = storage.Client()
        bucket = client.bucket(name)
        blob = bucket.blob(object_name)
        blob.cache_control = "public, max-age=31536000, immutable"
        blob.upload_from_string(content, content_type=(content_type or "").split(";", 1)[0])
    except Exception as exc:  # pragma: no cover - provider-specific failures
        raise CatalogMediaUploadError("Could not persist catalog image") from exc

    encoded_object = quote(object_name, safe="/")
    return CatalogImageUpload(
        url=f"https://storage.googleapis.com/{name}/{encoded_object}",
        object_name=object_name,
    )
