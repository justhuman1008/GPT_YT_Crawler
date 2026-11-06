from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import time

from .common import (ArchiveError, Deferred, ReconcileRequired, Skipped, atomic_json,
                     epoch, private_dir, safe_error, sha256)
from .youtube import YouTube
from .thumbnails import thumbnail_path, verify_thumbnail
from .layout import companion_target, video_target


class Runner:
    def __init__(self, config, database, downloader, drive, youtube_factory=YouTube):
        self.config, self.db, self.downloader, self.drive = config, database, downloader, drive
        self.youtube_factory = youtube_factory
        self.youtube = None
        self.youtube_attempts = 0
        self.download_root = config.state_dir / "downloads"
        if self.download_root.is_symlink():
            raise ArchiveError("다운로드 루트에 심볼릭 링크를 사용할 수 없습니다.")
        private_dir(self.download_root)

    def directory(self, video_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ArchiveError("잘못된 영상 ID입니다.")
        directory = self.download_root / video_id
        # Reject symlink escapes even if someone changed the persistent state.
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.download_root.resolve()):
            raise ArchiveError("다운로드 경로가 작업 폴더를 벗어났습니다.")
        private_dir(directory)
        return directory

    def local_media(self, job, directory):
        path = directory / "media.mkv"
        if job["local_path"] and Path(job["local_path"]).resolve() != path.resolve():
            raise ArchiveError("저장된 로컬 파일 경로가 작업 경로와 다릅니다.")
        if path.is_symlink():
            raise ArchiveError("영상 파일에 심볼릭 링크를 사용할 수 없습니다.")
        if path.exists() and job["sha256"]:
            if path.stat().st_size != job["file_size"] or sha256(path) != job["sha256"]:
                raise ArchiveError("로컬 원본 파일이 변경되었습니다. 파일을 유지합니다.")
        return path

    @staticmethod
    def target(root, metadata, video_id):
        return video_target(root, metadata, video_id)

    def process(self, original):
        video_id = original["video_id"]
        job = self.db.get(video_id)
        stage = None
        try:
            wants_youtube = self.config.wants_youtube(job)
            if wants_youtube and self.youtube_attempts >= self.config.max_youtube_uploads:
                raise Deferred("이번 실행의 YouTube 처리량 상한에 도달했습니다.")
            directory = self.directory(video_id)
            path = self.local_media(job, directory)
            if job["drive_root"] and job["drive_root"] != self.config.drive_remote:
                raise ArchiveError("기존 Drive 대상과 설정이 다릅니다. 별도 data 폴더를 사용하세요.")
            metadata = json.loads(job["metadata"] or "{}")
            needs_media = not job["drive_verified_at"] or wants_youtube and not job["youtube_id"]
            if needs_media and not path.exists():
                if job["drive_verified_at"]:
                    if shutil.disk_usage(directory).free < self.config.min_free_bytes + job["file_size"]:
                        raise ArchiveError("Drive 보관본을 복원할 여유 공간이 부족합니다.")
                    self.drive.restore(job["drive_target"], path, job["file_size"], job["sha256"], metadata["upload_timestamp"])
                else:
                    stage = "download"
                    self.db.update(video_id, state="downloading")
                    path, metadata = self.downloader.download(job, directory)
            if not job["sha256"]:
                # Includes recovery after a crash between final merge and DB commit.
                if not metadata:
                    stage = "download"
                    path, metadata = self.downloader.download(job, directory)
                size, digest = path.stat().st_size, sha256(path)
                metadata = {**metadata, "file_size": size, "sha256": digest}
                target = self.target(self.config.drive_remote, metadata, video_id)
                self.db.update(video_id, state="downloaded", title=metadata["title"], local_path=str(path), metadata=json.dumps(metadata, ensure_ascii=False),
                               file_size=size, sha256=digest, drive_target=target, drive_root=self.config.drive_remote)
                self.db.record_check(video_id, "download", {"file_size": size, "sha256": digest,
                                     "media_probe": True, "upload_timestamp": metadata["upload_timestamp"],
                                     "time_precision": metadata.get("time_precision")})
                job = self.db.get(video_id)
            stage = "drive"
            stamp = metadata["upload_timestamp"]
            if not job["drive_verified_at"]:
                self.db.update(video_id, state="drive_uploading")
            thumbnail = metadata.get("thumbnail")
            thumbnail_target = thumbnail_snapshot = None
            if thumbnail:
                image = thumbnail_path(directory, thumbnail)
                thumbnail_target = companion_target(job["drive_target"], image.name)
                if job["drive_verified_at"]:
                    thumbnail_snapshot = self.drive.verify(thumbnail_target, thumbnail["file_size"], thumbnail["sha256"], stamp)
                    if wants_youtube and not image.exists():
                        self.drive.restore(thumbnail_target, image, thumbnail["file_size"], thumbnail["sha256"], stamp)
                if image.exists() or not job["drive_verified_at"]:
                    verify_thumbnail(image, thumbnail)
                if not job["drive_verified_at"]:
                    thumbnail_snapshot = self.drive.put_verified(image, thumbnail_target, thumbnail["sha256"], stamp)
            sidecar = directory / "metadata.json"
            if sidecar.is_symlink():
                raise ArchiveError("메타데이터 파일에 심볼릭 링크를 사용할 수 없습니다.")
            atomic_json(sidecar, metadata)
            os.utime(sidecar, (sidecar.stat().st_atime, stamp))
            if not job["drive_verified_at"]:
                drive_snapshot = self.drive.put_verified(path, job["drive_target"], job["sha256"], stamp)
            else:
                drive_snapshot = self.drive.verify(job["drive_target"], job["file_size"], job["sha256"], stamp)
            sidecar_target = companion_target(job["drive_target"], "metadata.json")
            sidecar_snapshot = self.drive.put_verified(sidecar, sidecar_target, sha256(sidecar), stamp)
            self.db.update(video_id, state="drive_verified", drive_verified_at=epoch())
            def evidence(kind, snapshot, size, digest):
                return {"kind": kind, "file_size": size, "sha256": digest,
                        "remote_id": snapshot.get("ID"), "remote_modtime": snapshot.get("ModTime"),
                        "size_matches": True, "sha256_matches": True, "mtime_matches": True}
            files = [evidence("video", drive_snapshot, job["file_size"], job["sha256"]),
                     evidence("metadata", sidecar_snapshot, sidecar.stat().st_size, sha256(sidecar))]
            if thumbnail:
                files.append(evidence("thumbnail", thumbnail_snapshot, thumbnail["file_size"], thumbnail["sha256"]))
            self.db.record_check(video_id, "drive", {"files": files, "method": "full_sha256_readback"})
            if wants_youtube:
                stage = "youtube"
                self.youtube_attempts += 1
                if self.youtube is None:
                    self.youtube = self.youtube_factory(self.config)
                self.youtube.ensure(self.db, self.db.get(video_id), path, metadata)
                if not self.db.get(video_id)["youtube_verified_at"]:
                    raise ArchiveError("YouTube 업로드 검증 완료 기록이 없어 로컬 파일을 유지합니다.")
            stage = "cleanup"
            # Final source integrity check protects against mid-transfer local changes.
            if path.exists() and (path.stat().st_size != job["file_size"] or sha256(path) != job["sha256"]):
                raise ArchiveError("업로드 중 로컬 파일이 변경되었습니다. 정리를 중단합니다.")
            if thumbnail and image.exists():
                verify_thumbnail(image, thumbnail)
            stage = "drive"
            self.drive.confirm_unchanged(job["drive_target"], drive_snapshot)
            self.drive.confirm_unchanged(sidecar_target, sidecar_snapshot)
            if thumbnail_target:
                self.drive.confirm_unchanged(thumbnail_target, thumbnail_snapshot)
            stage = "cleanup"
            self.db.update(video_id, state="cleanup_pending", error=None, retry_at=0)
            self.cleanup(directory)
            self.db.update(video_id, state="complete", completed_at=epoch(), local_path=None, error=None)
            self.db.record_check(video_id, "cleanup", {"generated_files_removed": True})
            return "complete"
        except Skipped as error:
            self.db.update(video_id, state="skipped", error=safe_error(error))
            return "skipped"
        except Exception as error:
            current = self.db.get(video_id)
            state = "needs_reconcile" if isinstance(error, ReconcileRequired) else "waiting" if isinstance(error, Deferred) else "failed"
            if stage:
                self.db.record_check(video_id, stage, status="waiting" if state == "waiting" else "failed", error=safe_error(error))
            self.db.update(video_id, state=state, attempts=current["attempts"] + 1,
                           error=safe_error(error), retry_at=epoch() + self.config.retry_minutes * 60)
            return state

    def cleanup(self, directory):
        # Only generated files in this exact video directory are eligible; never recurse.
        generated = re.compile(r"media\.(?:(?:(?:f[\w-]+|temp)\.)?(?:mkv|mp4|webm|m4a|mp3|opus|aac|ogg|3gp|ts)"
                               r"(?:\.part(?:-Frag[0-9]+)?|\.ytdl)?|restore-part)\Z")
        eligible = [path for path in directory.iterdir()
                    if path.name in {"metadata.json", "thumbnail.jpg", "thumbnail.png", "thumbnail.converting.png"}
                    or path.name in {"media." + ext for ext in ("jpg", "jpeg", "png", "webp", "avif", "gif")}
                    or generated.fullmatch(path.name)]
        for path in eligible:
            if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
                raise ArchiveError("안전하게 삭제할 수 없는 임시 파일이 있습니다.")
        for path in eligible:
            path.unlink()
        if not any(directory.iterdir()):
            directory.rmdir()

    def run(self):
        counts = {}
        for job in self.db.jobs(self.config):
            state = self.process(job)
            counts[state] = counts.get(state, 0) + 1
            if self.config.sleep_seconds:
                time.sleep(self.config.sleep_seconds)
        return counts
