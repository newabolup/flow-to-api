"""Upload/download plumbing: temp files, size limits, magic-byte sniffing.

Fixes carried over from the old bridge:
  * content type was hardcoded to image/jpeg even when Flow returned a
    PNG -> clients that trust the header mis-decoded the file;
  * temp uploads were named `in-<ms>.jpg` inside the home directory, so
    two requests in the same millisecond could collide and a stale file
    could be picked up by the next job;
  * nothing bounded the upload size.
"""
from __future__ import annotations

import base64
import binascii
import os
import time
import uuid
from pathlib import Path

IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),  # refined below
    (b"BM", "image/bmp", ".bmp"),
)

VIDEO_SIGNATURES = (
    (b"\x1a\x45\xdf\xa3", "video/webm", ".webm"),
    (b"RIFF", "video/webm", ".webm"),
    (b"\x00\x00\x00\x18ftyp", "video/mp4", ".mp4"),
    (b"\x00\x00\x00\x20ftyp", "video/mp4", ".mp4"),
)


class UploadTooLarge(ValueError):
    pass


class UnsupportedMedia(ValueError):
    pass


def sniff(data: bytes) -> tuple[str, str]:
    """Return (mime, extension) for image or video bytes; ('','') if unknown."""
    if not data:
        return "", ""
    head = data[:32]
    for sig, mime, ext in IMAGE_SIGNATURES:
        if head.startswith(sig):
            if sig == b"RIFF" and head[8:12] != b"WEBP":
                continue
            return mime, ext
    # mp4 / mov: 'ftyp' at offset 4
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return "video/quicktime", ".mov"
        return "video/mp4", ".mp4"
    for sig, mime, ext in VIDEO_SIGNATURES:
        if head.startswith(sig):
            if sig == b"RIFF" and head[8:12] != b"WEBM":
                continue
            return mime, ext
    return "", ""


def sniff_image(data: bytes) -> tuple[str, str]:
    mime, ext = sniff(data)
    if mime.startswith("image/"):
        return mime, ext
    return "", ""


def sniff_video(data: bytes) -> tuple[str, str]:
    mime, ext = sniff(data)
    if mime.startswith("video/"):
        return mime, ext
    return "", ""


def decode_base64(value: str) -> bytes:
    raw = (value or "").strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise UnsupportedMedia("image_b64 is not valid base64") from exc


class TempFiles:
    """UUID-named temp files under <home>/tmp with an age sweep."""

    def __init__(self, directory: Path, max_bytes: int):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, data: bytes, prefix: str, suffix: str = "") -> Path:
        if len(data) > self.max_bytes:
            raise UploadTooLarge(
                f"upload is {len(data) / 1048576:.1f} MB, limit is "
                f"{self.max_bytes / 1048576:.0f} MB")
        if not suffix:
            _, suffix = sniff(data)
            suffix = suffix or ".bin"
        path = self.directory / f"{prefix}-{uuid.uuid4().hex}{suffix}"
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    @staticmethod
    def drop(path: Path | None) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def sweep(self, max_age_s: float = 3600) -> int:
        removed = 0
        cutoff = time.time() - max_age_s
        try:
            for entry in self.directory.iterdir():
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
                except Exception:
                    continue
        except FileNotFoundError:
            pass
        return removed
