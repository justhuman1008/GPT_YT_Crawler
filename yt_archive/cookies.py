"""Pull changed Drive cookies without overwriting yt-dlp's refreshed working jar."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .common import ArchiveError, epoch, private_dir
from .layout import COOKIES_RELATIVE_PATH


MAX_COOKIE_BYTES = 1024 * 1024


def validated_cookie_text(contents):
    """Validate before passing data to parsers that may print secret bad lines."""
    try:
        if not contents or len(contents) > MAX_COOKIE_BYTES:
            raise ValueError()
        text = contents.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines()
        if not lines or lines[0] not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
            raise ValueError()
        usable = 0
        now = epoch()
        for line in lines[1:]:
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                raise ValueError()
            domain, subdomains, path, secure, expires, name, value = fields
            if (not re.fullmatch(r"\.?([a-zA-Z0-9-]+\.)*youtube\.com", domain, re.IGNORECASE)
                    or subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}
                    or domain.startswith(".") != (subdomains == "TRUE") or not path.startswith("/")
                    or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name)
                    or any(ord(character) < 32 or ord(character) == 127 for character in path + value)):
                raise ValueError()
            expiry = int(expires) if expires else 0
            if not 0 <= expiry <= 253402300799:
                raise ValueError()
            usable += bool(value) and (expiry == 0 or expiry > now)
        if not usable:
            raise ValueError()
        return text.rstrip("\n") + "\n"
    except (ValueError, UnicodeError, TypeError):
        raise ArchiveError("쿠키 파일을 적용하지 않았습니다. UTF-8 Netscape 형식으로 YouTube 도메인의 쿠키만 내보내고, 빈 파일이나 모두 만료된 파일이 아닌지 확인하세요.") from None


def _temporary(directory):
    descriptor, name = tempfile.mkstemp(prefix=".cookie-", dir=directory)
    os.close(descriptor)
    return Path(name)


def _write_staged(path, contents):
    with path.open("wb") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _install(candidate, destination, marker, record):
    """Stage both files first; restore the old jar if marker commit fails."""
    state = backup = None
    installed = False
    keep_backup = False
    try:
        state = _temporary(destination.parent)
        _write_staged(state, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
        if destination.exists():
            backup = _temporary(destination.parent)
            with destination.open("rb") as source, backup.open("wb") as handle:
                shutil.copyfileobj(source, handle)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(candidate, destination)
        installed = True
        os.replace(state, marker)
    except OSError:
        if installed:
            if backup is not None:
                try:
                    os.replace(backup, destination)
                except OSError:
                    keep_backup = True
                    raise ArchiveError("쿠키 교체 복구에 실패했습니다. 기존 쿠키는 비공개 쿠키 폴더의 .cookie- 임시 파일에 보존했습니다. 저장소 상태를 확인하세요.") from None
            else:
                destination.unlink()
        raise
    finally:
        for path in (state, backup):
            if path is not None and not (path == backup and keep_backup):
                with suppress(OSError):
                    path.unlink(missing_ok=True)


def sync_drive_cookies(config, drive):
    """Caller holds the collection lock. Return only credential-free status."""
    if not config.cookies_from_drive:
        return {"status": "disabled"}
    candidate = None
    try:
        destination = config.cookies_file
        marker = destination.with_name(destination.name + ".drive-state.json")
        if destination.parent.is_symlink() or any(path.is_symlink() or path.exists() and not path.is_file()
                                                for path in (destination, marker)):
            raise ArchiveError("쿠키 파일과 변경 기록은 심볼릭 링크가 아닌 일반 파일이어야 합니다.")
        private_dir(destination.parent)
        remote = config.drive_remote + "/" + COOKIES_RELATIVE_PATH
        snapshot = drive.cookie_snapshot()
        previous = {}
        if marker.exists():
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                pass
        if (isinstance(previous, dict) and previous.get("schema_version") == 1 and previous.get("remote") == remote
                and previous.get("snapshot") == snapshot and destination.is_file() and destination.stat().st_size > 0):
            destination.chmod(0o600)
            marker.chmod(0o600)
            return {"status": "unchanged"}
        candidate = _temporary(destination.parent)
        drive.download_cookies(candidate)
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size != snapshot["size"]:
            raise ArchiveError("받은 쿠키 파일의 크기가 Drive 기록과 달라 적용하지 않았습니다.")
        contents = candidate.read_bytes()
        if len(contents) > MAX_COOKIE_BYTES or hashlib.md5(contents, usedforsecurity=False).hexdigest() != snapshot["md5"]:
            raise ArchiveError("받은 쿠키 파일이 Drive 원본과 달라 적용하지 않았습니다.")
        text = validated_cookie_text(contents)
        _write_staged(candidate, text.encode("utf-8"))
        _install(candidate, destination, marker, {"schema_version": 1, "remote": remote, "snapshot": snapshot})
        return {"status": "updated"}
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError("Drive 쿠키 파일 검사·교체에 실패했습니다. 파일 접근 권한과 연결 상태를 확인하세요.") from None
    finally:
        if candidate is not None:
            with suppress(OSError):
                candidate.unlink(missing_ok=True)
