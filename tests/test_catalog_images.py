from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.operations.service import _validate_catalog_image


@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("image/jpeg", b"\xff\xd8\xff" + b"x" * 32),
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 32),
        ("image/webp", b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"x" * 32),
    ],
)
def test_catalog_image_validator_accepts_supported_image_signatures(
    content_type: str,
    payload: bytes,
) -> None:
    _validate_catalog_image(payload, content_type)


def test_catalog_image_validator_rejects_spoofed_content_type() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_image(b"<html>not an image</html>", "image/png")

    assert exc.value.status_code == 422


def test_catalog_image_validator_rejects_unsupported_type() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_image(b"GIF89a", "image/gif")

    assert exc.value.status_code == 415


def test_catalog_image_validator_rejects_more_than_four_megabytes() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_image(b"\xff\xd8\xff" + b"x" * (4 * 1024 * 1024), "image/jpeg")

    assert exc.value.status_code == 413
