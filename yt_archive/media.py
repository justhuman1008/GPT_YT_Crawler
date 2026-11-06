from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from .common import ArchiveError, Deferred, Skipped, epoch, private_dir, upload_time, video_url
from .thumbnails import collect_thumbnail


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class Downloader:
    def __init__(self, config):
        self.config = config
        self.capacity_check = None

    def options(self):
        options = {"quiet": True, "noprogress": True, "logger": QuietLogger(),
                   "cachedir": False, "socket_timeout": 30, "retries": 2,
                   "fragment_retries": 2, "extractor_retries": 2,
                   "js_runtimes": {self.config.js_runtime: {}},
                   "ffmpeg_location": shutil.which(self.config.ffmpeg) or self.config.ffmpeg}
        if self.config.cookies_file.is_file():
            options["cookiefile"] = str(self.config.cookies_file)
        return options

    def discover(self, database):
        import yt_dlp

        failures = []
        discovered_at = epoch()
        for channel in self.config.channels:
            for tab in channel.tabs:
                source = channel.url + "/" + tab
                full = database.full_scan_due(source, self.config.full_rescan_hours)
                options = {**self.options(), "extract_flat": "in_playlist", "lazy_playlist": True,
                           "skip_download": True, "ignoreerrors": False}
                if not full:
                    options["playlistend"] = self.config.recent_limit
                try:
                    with yt_dlp.YoutubeDL(options) as client:
                        result = client.extract_info(source, download=False)
                        if not result or "entries" not in result:
                            raise ArchiveError("채널 목록을 읽을 수 없습니다.")
                        for entry in result["entries"]:
                            if not entry:
                                continue
                            try:
                                video_id, url = video_url(entry.get("id", ""))
                            except ArchiveError:
                                continue
                            database.add(video_id, url, channel.url, str(entry.get("title") or ""),
                                         channel.members_only, channel.youtube, discovered_at=discovered_at)
                    if full:
                        database.scanned(source)
                except Exception:
                    # A failed/incomplete scan is never recorded as a completed full scan.
                    failures.append(source)
        for url in self.config.extra_urls:
            video_id, canonical = video_url(url)
            database.add(video_id, canonical)
        return failures

    def ensure_space(self, directory, estimated=0):
        needed = self.config.min_free_bytes + 3 * estimated
        if shutil.disk_usage(directory).free < needed:
            raise ArchiveError("디스크 여유 공간이 부족합니다. 기존 파일을 유지합니다.")

    def download(self, job, directory: Path):
        import yt_dlp

        private_dir(directory)
        self.ensure_space(directory)
        maximum = self.config.max_height
        fmt = (f"bv*[height<={maximum}]+ba/b[height<={maximum}]" if maximum else "bv*+ba/b")
        def space_hook(progress):
            if progress.get("status") in {"downloading", "finished"}:
                self.ensure_space(directory)

        options = {**self.options(), "noplaylist": True, "format": fmt,
                   "outtmpl": str(directory / "media.%(ext)s"), "merge_output_format": "mkv",
                   "writethumbnail": True,
                   "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}],
                   "updatetime": False, "progress_hooks": [space_hook]}
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(job["url"], download=False)
            if not info or info.get("id") != job["video_id"]:
                raise ArchiveError("다운로드 대상 영상 ID가 일치하지 않습니다.")
            if info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                raise Deferred("방송 종료 및 다시보기 처리를 기다립니다.")
            if job["members_only"] and info.get("availability") != "subscriber_only":
                raise Skipped("멤버십 전용 수집 설정에 해당하지 않는 영상입니다.")
            stamp, precision = upload_time(info)
            estimated = sum(part.get("filesize") or part.get("filesize_approx") or 0
                            for part in info.get("requested_formats", [info]))
            self.ensure_space(directory, estimated)
            if self.capacity_check:
                self.capacity_check(estimated)
            processed = client.process_ie_result(info, download=True)
            if isinstance(processed, dict):
                info = processed
        media = directory / "media.mkv"
        if not media.is_file() or media.stat().st_size == 0:
            raise ArchiveError("병합된 영상 파일을 확인할 수 없습니다.")
        validate_media(media, self.config.ffprobe)
        os.utime(media, (media.stat().st_atime, stamp))
        channel_id = info.get("channel_id") or "manual"
        metadata = {"video_id": job["video_id"], "source_url": job["url"],
                    "title": str(info.get("title") or job["video_id"]),
                    "description": str(info.get("description") or ""),
                    "channel_id": channel_id, "channel": str(info.get("channel") or ""),
                    "upload_timestamp": stamp, "time_precision": precision,
                    "duration": info.get("duration"), "availability": info.get("availability"),
                    "height": info.get("height"),
                    "thumbnail": collect_thumbnail(info, directory, stamp, self.config.ffmpeg)}
        return media, metadata


def validate_media(path: Path, ffprobe: str):
    import json

    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise ArchiveError("FFprobe 영상 검증에 실패했습니다.")
    try:
        info = json.loads(result.stdout)
        if not any(stream.get("codec_type") == "video" for stream in info.get("streams", [])):
            raise ValueError()
        if float(info.get("format", {}).get("duration", 0)) <= 0:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ArchiveError("영상 스트림 또는 재생 길이를 확인할 수 없습니다.") from None
