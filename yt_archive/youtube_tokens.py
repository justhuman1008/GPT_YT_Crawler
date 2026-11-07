"""Import changed Drive tokens at startup without replacing locally refreshed tokens."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .common import ArchiveError, private_dir
from .layout import YOUTUBE_TOKEN_RELATIVE_PATH
from .youtube import API, SCOPES


MAX_TOKEN_BYTES = 64 * 1024


def token_info(contents):
    """Validate endpoints and fields before a library can use untrusted JSON."""
    try:
        if not contents or len(contents) > MAX_TOKEN_BYTES:
            raise ValueError()
        info = json.loads(contents.decode("utf-8-sig"))
        if (not isinstance(info, dict) or info.get("token_uri") != "https://oauth2.googleapis.com/token"
                or info.get("universe_domain", "googleapis.com") != "googleapis.com"
                or info.get("type", "authorized_user") != "authorized_user"
                or not isinstance(info.get("scopes"), list) or set(info["scopes"]) != set(SCOPES)
                or not isinstance(info.get("client_id"), str)
                or not re.fullmatch(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", info["client_id"])):
            raise ValueError()
        for field in ("refresh_token", "client_secret"):
            value = info.get(field)
            if not isinstance(value, str) or not value or any(ord(char) < 33 or ord(char) == 127 for char in value):
                raise ValueError()
        return info
    except (ValueError, TypeError, UnicodeError):
        raise ArchiveError("YouTube 토큰 형식·Google 인증 주소·업로드 및 조회 권한을 확인하세요. 기존 토큰은 보존합니다.") from None


def verify_youtube_token(info, channel_id):
    """Refresh and query the owner channel; never call an upload endpoint."""
    try:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from requests import Session

        credentials = Credentials.from_authorized_user_info(info, SCOPES)
        with Session() as refresh_session:
            credentials.refresh(Request(session=refresh_session))
        if credentials.granted_scopes is not None and not set(SCOPES).issubset(credentials.granted_scopes):
            raise ValueError()
        with AuthorizedSession(credentials) as session:
            response = session.get(API + "channels", params={"part": "id", "mine": "true"},
                                   timeout=(20, 60), allow_redirects=False)
            if response.status_code != 200 or [item["id"] for item in response.json().get("items", [])] != [channel_id]:
                raise ValueError()
        return token_info(credentials.to_json().encode("utf-8"))
    except Exception:
        raise ArchiveError("YouTube 토큰 갱신 또는 지정 채널 확인에 실패했습니다. 기존 토큰은 보존합니다.") from None


def _temporary(directory):
    descriptor, name = tempfile.mkstemp(prefix=".youtube-token-", dir=directory)
    os.close(descriptor)
    return Path(name)


def _write(path, value):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _install(candidate, destination, marker, record):
    state = backup = None
    installed = keep_backup = False
    try:
        state = _temporary(destination.parent)
        _write(state, record)
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
                    raise ArchiveError("토큰 교체 복구에 실패했습니다. 기존 토큰은 비공개 폴더의 .youtube-token- 임시 파일에 보존했습니다.") from None
            else:
                destination.unlink()
        raise
    finally:
        for path in (state, backup):
            if path is not None and not (path == backup and keep_backup):
                with suppress(OSError):
                    path.unlink(missing_ok=True)


def sync_drive_youtube_token(config, drive):
    """Caller holds the collection lock. Work even while video uploads are off."""
    if not config.youtube_token_from_drive:
        return {"status": "disabled"}
    candidate = None
    try:
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", config.youtube_channel):
            raise ArchiveError("토큰 자동 수신 전에 youtube.channel_id를 지정하세요.")
        destination = config.youtube_token
        marker = destination.with_name(destination.name + ".drive-state.json")
        if destination.parent.is_symlink() or any(path.is_symlink() or path.exists() and not path.is_file()
                                                for path in (destination, marker)):
            raise ArchiveError("YouTube 토큰과 변경 기록은 심볼릭 링크가 아닌 일반 파일이어야 합니다.")
        private_dir(destination.parent)
        snapshot = drive.youtube_token_snapshot()
        remote = config.drive_remote + "/" + YOUTUBE_TOKEN_RELATIVE_PATH
        previous = {}
        if marker.is_file():
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                pass
        if (isinstance(previous, dict) and previous.get("schema_version") == 1
                and previous.get("remote") == remote and previous.get("snapshot") == snapshot
                and previous.get("channel_id") == config.youtube_channel and destination.is_file()
                and 0 < destination.stat().st_size <= MAX_TOKEN_BYTES):
            try:
                token_info(destination.read_bytes())
            except ArchiveError:
                pass
            else:
                destination.chmod(0o600)
                marker.chmod(0o600)
                return {"status": "unchanged"}
        candidate = _temporary(destination.parent)
        drive.download_youtube_token(candidate)
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size != snapshot["size"]:
            raise ArchiveError("받은 YouTube 토큰의 크기가 Drive 기록과 달라 적용하지 않았습니다.")
        contents = candidate.read_bytes()
        if len(contents) > MAX_TOKEN_BYTES or hashlib.md5(contents, usedforsecurity=False).hexdigest() != snapshot["md5"]:
            raise ArchiveError("받은 YouTube 토큰이 Drive 원본과 달라 적용하지 않았습니다.")
        info = token_info(contents)
        if drive.youtube_token_snapshot() != snapshot:
            raise ArchiveError("수신 중 Drive 토큰이 변경되어 적용하지 않았습니다. 다시 실행하세요.")
        refreshed = verify_youtube_token(info, config.youtube_channel)
        _write(candidate, refreshed)
        _install(candidate, destination, marker, {"schema_version": 1, "remote": remote,
                                                 "snapshot": snapshot, "channel_id": config.youtube_channel})
        return {"status": "updated"}
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError("Drive의 YouTube 토큰 확인·수신·교체에 실패했습니다. 기존 토큰은 보존합니다.") from None
    finally:
        if candidate is not None:
            with suppress(OSError):
                candidate.unlink(missing_ok=True)
