import base64

import pytest

from flowbridge.media import (
    TempFiles,
    UploadTooLarge,
    decode_base64,
    sniff,
    sniff_image,
    sniff_video,
)

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 16
WEBM = b"\x1a\x45\xdf\xa3" + b"0" * 16


def test_sniff_image_types():
    assert sniff(JPEG)[0] == "image/jpeg"
    assert sniff(PNG)[0] == "image/png"
    assert sniff_image(PNG)[1] == ".png"


def test_sniff_video_types():
    assert sniff_video(MP4)[0] == "video/mp4"
    assert sniff_video(WEBM)[0] == "video/webm"
    # an image is not a video: the video endpoint relies on this to refuse
    # returning a still frame as an "mp4"
    assert sniff_video(JPEG) == ("", "")


def test_sniff_unknown():
    assert sniff(b"not-media-at-all") == ("", "")


def test_decode_base64_accepts_data_uri():
    payload = base64.b64encode(JPEG).decode()
    assert decode_base64(f"data:image/jpeg;base64,{payload}") == JPEG
    assert decode_base64(payload) == JPEG


def test_temp_files_enforces_size_limit(tmp_path):
    store = TempFiles(tmp_path, max_bytes=24)
    with pytest.raises(UploadTooLarge):
        store.write(b"x" * 32, "in")
    path = store.write(JPEG, "in")
    assert path.exists()
    assert path.suffix == ".jpg"
    store.drop(path)
    assert not path.exists()


def test_temp_files_sweep(tmp_path):
    store = TempFiles(tmp_path, max_bytes=1024)
    path = store.write(JPEG, "in")
    old = path.stat().st_mtime - 10_000
    import os
    os.utime(path, (old, old))
    assert store.sweep(max_age_s=60) == 1
