"""Durable daily/interval scheduling; one worker at a time under run_lock."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math

from .common import ArchiveError, atomic_json
from .sheet_control import DAILY, INTERVAL


KST = timezone(timedelta(hours=9))


def next_daily(settings, now):
    local = datetime.fromtimestamp(now, KST)
    candidate = local.replace(hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
    if candidate.timestamp() < now - (local.second + local.microsecond / 1000000):
        candidate += timedelta(days=1)
    return candidate.timestamp()


def schedule_tick(settings, path, now):
    """Reserve before work. Disabled checks do not create collection run rows."""
    previous = {}
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ArchiveError("자동 실행 상태 기록은 일반 파일이어야 합니다.")
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(previous, dict) or previous.get("schema_version") != 1
                    or not isinstance(previous.get("settings"), dict)
                    or previous.get("status") not in {"disabled", "waiting", "due"}
                    or "next_due" not in previous):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ArchiveError("자동 실행 상태 기록이 손상되었습니다. 중복 실행을 막기 위해 예약 실행을 중단합니다.") from None
    signature = asdict(settings)
    same = previous.get("settings") == signature
    state = {"schema_version": 1, "settings": signature, "checked_at": now,
             "last_started_at": previous.get("last_started_at")}
    if not settings.enabled:
        state.update(status="disabled", next_due=None)
    else:
        due = previous.get("next_due") if same else None
        if same and due is None:
            raise ArchiveError("자동 실행의 다음 실행 시각이 손상되었습니다.")
        if not same:
            due = now if settings.mode == INTERVAL else next_daily(settings, now)
        if type(due) not in (int, float) or not math.isfinite(due) or due < 0:
            raise ArchiveError("자동 실행의 다음 실행 시각을 확인할 수 없습니다.")
        if now >= due:
            state.update(status="due", last_started_at=now, scheduled_for=due,
                         next_due=now + settings.interval_minutes * 60 if settings.mode == INTERVAL else next_daily(settings, now + 60))
        else:
            state.update(status="waiting", next_due=due)
    atomic_json(path, state)
    return {"status": state["status"], "next_run": datetime.fromtimestamp(state["next_due"], KST).isoformat() if state["next_due"] is not None else None,
            "last_started_at": state.get("last_started_at")}
