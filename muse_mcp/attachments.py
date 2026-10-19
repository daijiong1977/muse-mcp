"""Build chat items from local image files without logging their contents."""

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


MAX_IMAGES = 10
# Base64 plus the request envelope must fit the gateway's 256 transport chunks.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported image contents; use PNG, JPEG, GIF, or WebP")


def build_message_items(
    message: str, images: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Validate and encode local images before any connection or message send.

    File signatures determine MIME type, rather than an untrusted extension.
    The gateway still performs image decoding/validation. URLs are not fetched.
    """
    if not isinstance(message, str):
        raise ValueError("message must be a string")
    if images is None:
        images = []
    if not isinstance(images, (list, tuple)) or any(
        not isinstance(path, str) or not path.strip() for path in images
    ):
        raise ValueError("images must be an array of local file paths")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"At most {MAX_IMAGES} images can be attached")
    if not images and not message.strip():
        raise ValueError("Supply a non-empty message or at least one image")

    items: List[Dict[str, Any]] = []
    total = 0
    for filename in images:
        path = Path(filename).expanduser()
        try:
            if not path.is_file():
                raise ValueError(f"Image is not a regular local file: {path.name}")
            with path.open("rb") as source:
                data = source.read(MAX_IMAGE_BYTES - total + 1)
        except OSError:
            raise ValueError(f"Cannot read image: {path.name}") from None
        total += len(data)
        if total > MAX_IMAGE_BYTES:
            raise ValueError("Images must total at most 10 MiB before Base64 encoding")
        items.append({
            "type": "image",
            "mime_type": _image_mime(data),
            "data_base64": base64.b64encode(data).decode("ascii"),
            "filename": path.name,
        })
    if message.strip():
        items.append({"type": "text", "text": message})
    return items
