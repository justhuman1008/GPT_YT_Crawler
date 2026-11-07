from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit


class ArchiveError(RuntimeError):
    """An actionable error whose text contains no upstream credentials."""


class Deferred(ArchiveError):
    """A video is not ready, e.g. live or still processing."""


class LockBusy(ArchiveError):
    """Another collection owns the lock; a scheduler must skip this tick."""


class Skipped(ArchiveError):
    """A video does not match the configured collection policy."""


class ReconcileRequired(ArchiveError):
    """Upload outcome needs manual reconciliation; never start another upload."""


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def video_url(value: str) -> tuple[str, str]:
    if VIDEO_ID.fullmatch(value):
        return value, f"https://www.youtube.com/watch?v={value}"
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname not in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    }:
        raise ArchiveError("유효한 HTTPS YouTube 영상 URL이 필요합니다.")
    candidate = (parts.path.strip("/") if parts.hostname == "youtu.be" else
                 parse_qs(parts.query).get("v", [""])[0])
    if not candidate and parts.path.startswith(("/shorts/", "/live/")):
        candidate = parts.path.split("/")[2]
    if not VIDEO_ID.fullmatch(candidate):
        raise ArchiveError("YouTube 영상 ID를 확인할 수 없습니다.")
    return candidate, f"https://www.youtube.com/watch?v={candidate}"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def atomic_text(path: Path, value: str) -> None:
    private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def upload_time(info: dict) -> tuple[float, str]:
    value = info.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if value > 0 and math.isfinite(value):
                datetime.fromtimestamp(value, timezone.utc)
                return float(int(value)), "second"
        except (OverflowError, ValueError, OSError):
            pass
    value = info.get("upload_date")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{8}", value):
        try:
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp(), "day"
        except ValueError:
            pass
    raise ArchiveError("원본 업로드 날짜를 확인할 수 없습니다. 파일을 보존하고 재시도합니다.")


def time_matches(expected: float, actual: str) -> bool:
    try:
        parsed = datetime.fromisoformat(actual.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and abs(parsed.timestamp() - expected) <= 1
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


@contextlib.contextmanager
def run_lock(directory: Path):
    """OS lock releases even after SIGKILL; lock files are never unlinked."""
    private_dir(directory)
    with (directory / "run.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockBusy("다른 작업이 실행 중입니다.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def safe_error(error: Exception) -> str:
    # Never persist raw downloader, subprocess, OAuth or HTTP exception text.
    return str(error) if isinstance(error, ArchiveError) else f"{type(error).__name__}: 외부 작업 실패 (인증/네트워크/설정을 확인하세요)."


def epoch() -> float:
    return time.time()
