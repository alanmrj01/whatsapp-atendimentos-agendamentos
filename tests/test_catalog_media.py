from __future__ import annotations

from pytest import raises

from app.operations.catalog_media import (
    MAX_CATALOG_IMAGE_BYTES,
    CatalogMediaValidationError,
    validate_catalog_image,
)


def test_catalog_image_accepts_supported_formats() -> None:
    assert validate_catalog_image(content=b"jpeg", content_type="image/jpeg") == ".jpg"
    assert validate_catalog_image(content=b"png", content_type="image/png") == ".png"
    assert validate_catalog_image(content=b"webp", content_type="image/webp") == ".webp"


def test_catalog_image_rejects_unsupported_or_empty_files() -> None:
    with raises(CatalogMediaValidationError, match="JPG, PNG ou WebP"):
        validate_catalog_image(content=b"gif", content_type="image/gif")

    with raises(CatalogMediaValidationError, match="vazia"):
        validate_catalog_image(content=b"", content_type="image/jpeg")


def test_catalog_image_rejects_files_larger_than_five_megabytes() -> None:
    with raises(CatalogMediaValidationError, match="5 MB"):
        validate_catalog_image(
            content=b"x" * (MAX_CATALOG_IMAGE_BYTES + 1),
            content_type="image/jpeg",
        )
