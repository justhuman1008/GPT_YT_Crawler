"""A credential-free fallback even when configuration or the database fails."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys

from .common import private_dir, safe_error
from .reports import KST, clean_error


def record_error(config_path, config, command, error):
    record = {"at": datetime.now(KST).isoformat(timespec="seconds"),
              "command": command, "error": clean_error(safe_error(error))}
    roots = [config.state_dir] if config is not None else []
    roots.append(Path(config_path).resolve().parent / "data")
    for root in dict.fromkeys(roots):
        path = root / "errors.jsonl"
        try:
            if root.is_symlink() or path.is_symlink():
                continue
            private_dir(root)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                path.chmod(0o600)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return
        except Exception:
            continue
    # systemd captures this and the original safe error even if storage is unavailable.
    print("별도 오류 파일 저장에도 실패했습니다. 서비스 로그를 확인하세요.", file=sys.stderr)
