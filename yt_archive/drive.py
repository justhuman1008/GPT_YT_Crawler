from __future__ import annotations

import configparser
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading

from .common import ArchiveError, sha256, time_matches
from .layout import COOKIES_RELATIVE_PATH, MANAGEMENT_DIRECTORY, REPORTS_DIRECTORY


class Drive:
    def __init__(self, config):
        self.config = config
        self.command = [config.rclone, "--config", str(config.rclone_config),
                        "--contimeout", "30s", "--timeout", "120s", "--retries", "2", "--log-level", "ERROR"]

    def validate_backend(self):
        parser = configparser.RawConfigParser()
        try:
            with self.config.rclone_config.open(encoding="utf-8-sig") as handle:
                parser.read_file(handle)
            name = self.config.drive_remote.split(":", 1)[0]
            if parser.get(name, "type") != "drive":
                raise ValueError()
        except (OSError, ValueError, configparser.Error):
            raise ArchiveError("rclone 설정의 대상 remote는 Google Drive(type=drive)여야 합니다.") from None

    def run(self, args, missing=False, timeout=14400):
        try:
            result = subprocess.run(self.command + args, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ArchiveError("rclone 실행 또는 네트워크 시간 초과입니다.") from None
        if missing and result.returncode in {3, 4}:
            return None
        if result.returncode:
            raise ArchiveError(f"Google Drive 작업 실패 (rclone 종료 코드 {result.returncode}). 인증·용량·연결을 확인하세요.")
        return result.stdout

    def stat(self, target):
        raw = self.run(["lsjson", target, "--stat", "--hash"], missing=True)
        return json.loads(raw) if raw is not None else None

    def cookie_snapshot(self):
        """Read metadata only, rejecting ambiguous same-name Drive files."""
        raw = self.run(["lsjson", self.config.drive_remote + "/" + MANAGEMENT_DIRECTORY,
                        "--files-only", "--hash"], missing=True, timeout=180)
        if raw is None:
            raise ArchiveError("Drive 관리용 파일 폴더의 youtube-cookies.txt를 찾을 수 없습니다.")
        try:
            entries = json.loads(raw)
            if not isinstance(entries, list):
                raise ValueError()
            matches = [item for item in entries if isinstance(item, dict)
                       and item.get("Name", item.get("Path")) == "youtube-cookies.txt"]
            if len(matches) != 1:
                raise ArchiveError("Drive 관리용 파일 폴더에는 youtube-cookies.txt가 정확히 하나 있어야 합니다.")
            item = matches[0]
            hashes = item.get("Hashes") or {}
            md5 = next((value for key, value in hashes.items() if key.lower() == "md5"), None)
            modified = datetime.fromisoformat(item["ModTime"].replace("Z", "+00:00"))
            if (item.get("IsDir") is not False or type(item.get("Size")) is not int
                    or not 0 < item["Size"] <= 1024 * 1024 or modified.tzinfo is None
                    or not isinstance(item.get("ID"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item["ID"])
                    or not isinstance(md5, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", md5)
                    or str(item.get("MimeType", "")).startswith("application/vnd.google-apps.")):
                raise ValueError()
            return {"id": item["ID"], "size": item["Size"], "modified": modified.astimezone(timezone.utc).isoformat(),
                    "md5": md5.lower()}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise ArchiveError("Drive 쿠키 파일의 크기·형식·수정 정보·MD5를 확인할 수 없습니다. 1MiB 이하의 일반 텍스트 파일을 사용하세요.") from None

    def download_cookies(self, destination):
        self.run(["copyto", self.config.drive_remote + "/" + COOKIES_RELATIVE_PATH, str(destination),
                  "--ignore-times", "--max-size", "1048576B"], timeout=180)

    def ensure_capacity(self, estimated=0):
        info = json.loads(self.run(["about", self.config.drive_remote.split(":", 1)[0] + ":", "--json"]))
        free = info.get("free")
        # Some Workspace accounts have no finite reported quota; upload errors still preserve local files.
        if free is not None and (not isinstance(free, (int, float)) or free < estimated + 1024**2):
            raise ArchiveError("Google Drive 잔여 용량이 부족합니다. 다운로드 전에 중단합니다.")

    def remote_digest(self, target):
        result = hashlib.sha256()
        expired = threading.Event()
        # Discard upstream stderr so signed URLs/tokens never reach logs or temp files.
        try:
            process = subprocess.Popen(self.command + ["cat", target], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
        except OSError:
            raise ArchiveError("rclone 원격 검증을 시작할 수 없습니다.") from None
        def expire():
            expired.set()
            if process.poll() is None:
                process.kill()
        timer = threading.Timer(14400, expire)
        timer.daemon = True
        timer.start()
        try:
            with process.stdout:
                for block in iter(lambda: process.stdout.read(4 * 1024**2), b""):
                    result.update(block)
            code = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
        if expired.is_set() or code:
            raise ArchiveError("Google Drive 원격 파일 읽기 검증에 실패했습니다.")
        return result.hexdigest()

    def put_verified(self, source: Path, target: str, digest: str, stamp: float):
        existing = self.stat(target)
        if existing is None:
            self.run(["copyto", str(source), target, "--immutable"])
        return self.verify(target, source.stat().st_size, digest, stamp)

    def publish_report(self, source: Path, relative: str):
        # Reports are the only mutable remote files. Never use this for video originals.
        if not re.fullmatch(re.escape(REPORTS_DIRECTORY) + r"/(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|latest)\.(?:json|md)", relative):
            raise ArchiveError("허용되지 않는 보고서 경로입니다.")
        target = self.config.drive_remote + "/" + relative
        digest, stamp = sha256(source), source.stat().st_mtime
        self.run(["copyto", str(source), target, "--ignore-times"])
        return self.verify(target, source.stat().st_size, digest, stamp)

    def verify(self, target: str, size: int, digest: str, stamp: float):
        before = self.stat(target)
        if (before is None or before.get("IsDir") or before.get("Size") != size or
                not time_matches(stamp, before.get("ModTime"))):
            raise ArchiveError("Google Drive 파일 크기·수정 시각 검증에 실패했습니다. 로컬 파일을 유지합니다.")
        if self.remote_digest(target) != digest:
            raise ArchiveError("Google Drive SHA-256 불일치입니다. 원격 파일을 덮어쓰지 않고 로컬을 유지합니다.")
        after = self.stat(target)
        if self.fingerprint(before) != self.fingerprint(after):
            raise ArchiveError("검증 중 Google Drive 파일이 변경되었습니다.")
        return after

    @staticmethod
    def fingerprint(item):
        return {key: item.get(key) for key in ("ID", "Size", "ModTime", "Hashes")} if item else None

    def confirm_unchanged(self, target, verified):
        if self.fingerprint(self.stat(target)) != self.fingerprint(verified):
            raise ArchiveError("업로드 후 Google Drive 보관본이 변경되었습니다. 로컬 파일을 유지합니다.")

    def restore(self, target, path: Path, size, digest, stamp):
        # Download to a separate file; never overwrite a local partial/completed source.
        temporary = path.with_suffix(".restore-part")
        self.run(["copyto", target, str(temporary)])
        if temporary.stat().st_size != size or sha256(temporary) != digest:
            raise ArchiveError("Drive 보관본 복원 검증에 실패했습니다.")
        temporary.replace(path)
        import os
        os.utime(path, (path.stat().st_atime, stamp))
