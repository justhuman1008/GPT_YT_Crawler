from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .common import ArchiveError, video_url
from .layout import ARCHIVE_DIRECTORY


@dataclass(frozen=True)
class Channel:
    name: str
    url: str
    tabs: tuple[str, ...]
    members_only: bool = False
    youtube: bool = True


@dataclass(frozen=True)
class Config:
    path: Path
    state_dir: Path
    cookies_file: Path
    rclone_config: Path
    drive_remote: str
    youtube_enabled: bool
    youtube_existing: bool
    youtube_client: Path
    youtube_token: Path
    youtube_channel: str
    channels: tuple[Channel, ...]
    extra_urls: tuple[str, ...]
    min_free_bytes: int = 5 * 1024**3
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    rclone: str = "rclone"
    js_runtime: str = "deno"
    max_height: int = 0
    sleep_seconds: float = 10
    recent_limit: int = 100
    full_rescan_hours: int = 168
    max_jobs: int = 20
    retry_minutes: int = 30
    category_id: str = "22"
    chunk_bytes: int = 8 * 1024**2
    max_youtube_uploads: int = 10
    reports_enabled: bool = True
    reports_drive: bool = True
    cookies_from_drive: bool = False

    def wants_youtube(self, job: dict) -> bool:
        allowed = next((channel.youtube for channel in self.channels if channel.url == job["source"]),
                       bool(job["youtube_allowed"]))
        return self.youtube_enabled and allowed and (
            not job["completed_at"] or self.youtube_existing or bool(job["youtube_session"]) or bool(job["youtube_id"]))


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        root = path.parent
        storage, download, drive, youtube, schedule = [data.get(key, {}) for key in
                                                       ("storage", "download", "drive", "youtube", "schedule")]
        def location(value):
            item = Path(value).expanduser()
            return (root / item).resolve() if not item.is_absolute() else item.resolve()

        def flag(section, key, default):
            value = section.get(key, default)
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
            return value

        channels = []
        for item in data.get("channels", []):
            parts = urlsplit(item["url"])
            if parts.scheme != "https" or parts.hostname not in {"www.youtube.com", "youtube.com"}:
                raise ValueError("channel URL")
            channel_path = parts.path.rstrip("/")
            if not re.fullmatch(r"/(?:@[^/?#]+|channel/UC[A-Za-z0-9_-]{22}|c/[^/?#]+|user/[^/?#]+)", channel_path):
                raise ValueError("channel URL must be a channel root")
            tabs = tuple(item.get("tabs", ["membership", "videos", "streams"]))
            if not tabs or set(tabs) - {"membership", "videos", "streams", "shorts"}:
                raise ValueError("tabs")
            channels.append(Channel(str(item.get("name", channel_path)),
                                    "https://www.youtube.com" + channel_path, tabs,
                                    flag(item, "members_only", False), flag(item, "youtube", True)))
        remote = drive.get("remote", "gdrive:" + ARCHIVE_DIRECTORY).rstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+:[^\r\n]*", remote) or ".." in remote.split("/"):
            raise ValueError("drive.remote")
        if drive.get("verification", "readback") != "readback":
            raise ValueError("verification must be readback")
        state = location(storage.get("state_dir", "data"))
        if state == root or state == Path(state.anchor):
            raise ValueError("state_dir must be a dedicated directory")
        # extra_urls is top-level; accepting schedule.extra_urls helps older examples.
        extras = tuple(video_url(value)[1] for value in data.get("extra_urls", schedule.get("extra_urls", [])))
        result = Config(
            path=path, state_dir=state,
            cookies_file=location(download.get("cookies_file", "secrets/youtube-cookies.txt")),
            rclone_config=location(drive.get("config_file", "secrets/rclone.conf")), drive_remote=remote,
            youtube_enabled=flag(youtube, "enabled", False), youtube_existing=flag(youtube, "upload_existing", False),
            youtube_client=location(youtube.get("client_secrets_file", "secrets/youtube-client.json")),
            youtube_token=location(youtube.get("token_file", "secrets/youtube-token.json")),
            youtube_channel=str(youtube.get("channel_id", "")), channels=tuple(channels), extra_urls=extras,
            min_free_bytes=int(float(storage.get("min_free_gib", 5)) * 1024**3),
            ffmpeg=str(download.get("ffmpeg", "ffmpeg")), ffprobe=str(download.get("ffprobe", "ffprobe")),
            rclone=str(drive.get("rclone", "rclone")), js_runtime=str(download.get("js_runtime", "deno")),
            max_height=int(download.get("max_height", 0)), sleep_seconds=float(download.get("sleep_seconds", 10)),
            recent_limit=int(schedule.get("recent_limit", 100)), full_rescan_hours=int(schedule.get("full_rescan_hours", 168)),
            max_jobs=int(schedule.get("max_jobs_per_run", 20)), retry_minutes=int(schedule.get("retry_minutes", 30)),
            category_id=str(youtube.get("category_id", "22")), chunk_bytes=int(youtube.get("chunk_mib", 8)) * 1024**2,
            max_youtube_uploads=int(youtube.get("max_uploads_per_run", 10)),
            reports_enabled=flag(data.get("reports", {}), "enabled", True),
            reports_drive=flag(data.get("reports", {}), "upload_to_drive", True),
            cookies_from_drive=flag(download, "cookies_from_drive", False))
        if min(result.min_free_bytes, result.max_height, result.sleep_seconds) < 0 or min(
            result.recent_limit, result.full_rescan_hours, result.max_jobs, result.retry_minutes,
            result.chunk_bytes, result.max_youtube_uploads) <= 0 or result.chunk_bytes > 64 * 1024**2:
            raise ValueError("numeric settings")
        if result.youtube_enabled and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", result.youtube_channel):
            raise ValueError("youtube.channel_id")
        return result
    except ArchiveError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ArchiveError("설정 파일을 확인하세요. 경로·채널 URL·숫자·boolean 및 YouTube 채널 ID가 올바른지 확인하세요.") from None
