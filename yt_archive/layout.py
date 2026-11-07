"""Drive paths shared by video storage and management files."""
from __future__ import annotations

import re
import unicodedata

from .common import ArchiveError


ARCHIVE_DIRECTORY = "유튜브 동영상"
MANAGEMENT_DIRECTORY = "관리용 파일"
REPORTS_DIRECTORY = MANAGEMENT_DIRECTORY + "/로그"
COOKIES_RELATIVE_PATH = MANAGEMENT_DIRECTORY + "/youtube-cookies.txt"
YOUTUBE_TOKEN_RELATIVE_PATH = MANAGEMENT_DIRECTORY + "/youtube-token.json"


def component(value, fallback):
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")[:120].rstrip(" .")
    return text or fallback


def video_target(root, metadata, video_id):
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ArchiveError("잘못된 영상 ID입니다.")
    channel_id = metadata.get("channel_id") or "manual"
    if not isinstance(channel_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", channel_id):
        channel_id = "manual"
    channel = component(metadata.get("channel"), channel_id)
    # Channel IDs keep namesakes separate and protect the management directory.
    folder = channel if channel == channel_id else f"{channel} [{channel_id}]"
    title = component(metadata.get("title"), "영상")
    return f"{root}/{folder}/{title} [{video_id}].mkv"


def companion_target(target, name):
    if name not in {"metadata.json", "thumbnail.jpg", "thumbnail.png"}:
        raise ArchiveError("허용되지 않는 부가 파일 경로입니다.")
    directory, filename = target.rsplit("/", 1)
    # Resume old jobs at their recorded paths without moving archived files.
    if filename == "media.mkv":
        return directory + "/" + name
    return directory + "/" + filename.rsplit(".", 1)[0] + "." + name
