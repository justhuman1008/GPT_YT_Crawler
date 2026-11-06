from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .common import ArchiveError, sha256


MAX_THUMBNAIL_BYTES = 50_000_000
THUMBNAIL_TYPES = {"thumbnail.jpg": "image/jpeg", "thumbnail.png": "image/png"}
SOURCE_NAMES = {"media." + ext for ext in ("jpg", "jpeg", "png", "webp", "avif", "gif")}


def image_type(path: Path):
    with path.open("rb") as stream:
        header = stream.read(12)
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def collect_thumbnail(info, directory: Path, stamp, ffmpeg):
    """Keep the selected original image; convert unsupported formats losslessly to PNG."""
    source = None
    for item in reversed(info.get("thumbnails") or []):
        if item.get("filepath"):
            candidate = Path(item["filepath"])
            if candidate.is_symlink() or candidate.resolve().parent != directory.resolve() or candidate.name not in SOURCE_NAMES:
                raise ArchiveError("원본 썸네일 경로가 다운로드 폴더를 벗어났습니다.")
            if candidate.is_file():
                source = candidate
                break
    if source is None:
        return None
    if not 0 < source.stat().st_size <= MAX_THUMBNAIL_BYTES:
        raise ArchiveError("원본 썸네일 파일이 비어 있거나 YouTube 썸네일 용량 제한을 초과했습니다.")
    mime = image_type(source)
    if mime is None:
        converted = directory / "thumbnail.converting.png"
        if converted.is_symlink():
            raise ArchiveError("썸네일 변환 경로에 심볼릭 링크를 사용할 수 없습니다.")
        if converted.exists():
            if not converted.is_file():
                raise ArchiveError("썸네일 변환 경로가 일반 파일이 아닙니다.")
            converted.unlink()
        try:
            result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(source),
                                     "-frames:v", "1", str(converted)], capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise ArchiveError("원본 썸네일을 PNG로 변환하지 못했습니다.") from None
        if result.returncode or not converted.is_file() or image_type(converted) != "image/png":
            raise ArchiveError("원본 썸네일을 PNG로 변환하지 못했습니다.")
        source = converted
        mime = "image/png"
    target = directory / ("thumbnail.jpg" if mime == "image/jpeg" else "thumbnail.png")
    if target.is_symlink():
        raise ArchiveError("썸네일 저장 경로에 심볼릭 링크를 사용할 수 없습니다.")
    source.replace(target)
    if not 0 < target.stat().st_size <= MAX_THUMBNAIL_BYTES:
        raise ArchiveError("변환된 썸네일이 YouTube 썸네일 용량 제한을 초과했습니다.")
    os.utime(target, (target.stat().st_atime, stamp))
    return {"filename": target.name, "mime_type": mime, "file_size": target.stat().st_size, "sha256": sha256(target)}


def thumbnail_path(directory: Path, metadata):
    name = metadata.get("filename")
    if (name not in THUMBNAIL_TYPES or metadata.get("mime_type") != THUMBNAIL_TYPES[name] or
            type(metadata.get("file_size")) is not int or not 0 < metadata["file_size"] <= MAX_THUMBNAIL_BYTES):
        raise ArchiveError("저장된 원본 썸네일 정보가 올바르지 않습니다.")
    path = directory / name
    if path.is_symlink():
        raise ArchiveError("썸네일 파일에 심볼릭 링크를 사용할 수 없습니다.")
    return path


def verify_thumbnail(path: Path, metadata):
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != metadata["file_size"] or
            sha256(path) != metadata.get("sha256") or image_type(path) != metadata["mime_type"]):
        raise ArchiveError("원본 썸네일의 크기·형식·SHA-256 검증에 실패했습니다. 로컬 파일을 유지합니다.")
