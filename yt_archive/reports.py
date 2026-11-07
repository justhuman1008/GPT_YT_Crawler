"""Daily, credential-free reports from durable transfer/verification history.

Generating a report never calls YouTube or re-downloads archived media. A check is
evidence at its recorded time, not a promise that a remote file still exists now.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import html
import json
from pathlib import Path
import re

from .common import ArchiveError, atomic_json, atomic_text, epoch, private_dir, run_lock, safe_error
from .layout import REPORTS_DIRECTORY


KST = timezone(timedelta(hours=9), "Asia/Seoul")
STAGES = {"download": "다운로드", "drive": "Google Drive", "youtube": "YouTube", "cleanup": "Oracle 임시 파일 정리"}
EVENTS = {"download": "downloaded", "drive": "drive_verified", "youtube": "youtube_verified", "cleanup": "complete"}
LABELS = {"verified": "검증 완료", "legacy_complete": "완료 이력만 있음 (상세 검증 기록 없음)",
          "failed": "실패", "waiting": "대기", "not_started": "미완료 / 확인 기록 없음",
          "disabled": "업로드 꺼짐", "excluded": "업로드 제외", "not_requested": "업로드 대상 아님"}
TABLE_LABELS = {**LABELS, "verified": "성공", "not_started": "미실행 / 미완료", "disabled": "사용 안 함"}
STATE_LABELS = {"pending": "대기", "downloading": "다운로드 중", "downloaded": "다운로드 완료",
                "drive_uploading": "Drive 처리 중", "drive_verified": "Drive 검증 완료",
                "youtube_uploading": "YouTube 전송 중", "youtube_processing": "YouTube 처리 확인 중",
                "youtube_thumbnail_set": "썸네일 적용 확인 중", "youtube_verified": "YouTube 검증 완료",
                "cleanup_pending": "정리 대기", "complete": "완료", "failed": "실패", "waiting": "대기",
                "needs_reconcile": "업로드 결과 수동 확인 필요", "skipped": "수집 제외"}


def parse_day(value="yesterday", now=None):
    today = datetime.fromtimestamp(epoch() if now is None else now, KST).date()
    if value in {"today", "yesterday"}:
        return today - timedelta(days=value == "yesterday")
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        day = date.fromisoformat(value)
        if day > today:
            raise ValueError()
        return day
    except (ValueError, TypeError):
        raise ArchiveError("보고서 날짜는 today, yesterday 또는 미래가 아닌 YYYY-MM-DD여야 합니다.") from None


def iso(stamp):
    return datetime.fromtimestamp(stamp, KST).isoformat(timespec="seconds") if stamp is not None else None


def digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def identifier(value, video=False):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}" if video else r"[A-Za-z0-9_-]{1,200}", value) else None


def clean_error(value):
    if not value:
        return None
    value = re.sub(r"https?://\S+", "[URL 생략]", str(value))
    value = re.sub(r"(?i)(?:access_token|refresh_token|client_secret|upload_id|authorization|cookie)\s*[:=]\s*\S+", "[인증 정보 생략]", value)
    value = re.sub(r"(?i)Bearer\s+\S+", "[인증 정보 생략]", value)
    return value[:2000]


def evidence_for(stage, value):
    """Explicit allowlist: never serialize DB rows, arbitrary metadata, or sessions."""
    if stage == "download":
        return {"file_size": value.get("file_size"), "sha256": digest(value.get("sha256")),
                "media_probe": value.get("media_probe") is True,
                "source_upload_time": iso(value.get("upload_timestamp")), "time_precision": value.get("time_precision")}
    if stage == "drive":
        files = []
        for item in value.get("files", []):
            if item.get("kind") not in {"video", "metadata", "thumbnail"}:
                continue
            remote_id = identifier(item.get("remote_id"))
            files.append({"kind": item["kind"], "file_size": item.get("file_size"), "sha256": digest(item.get("sha256")),
                          "url": f"https://drive.google.com/file/d/{remote_id}/view" if remote_id else None,
                          "remote_modtime": item.get("remote_modtime"),
                          **{key: item.get(key) is True for key in ("size_matches", "sha256_matches", "mtime_matches")}})
        return {"method": "full_sha256_readback", "files": files}
    if stage == "youtube":
        return {"privacy": "unlisted" if value.get("privacy") == "unlisted" else None,
                **{key: value.get(key) is True for key in ("processing_complete", "description_matches", "custom_thumbnail_present")},
                "thumbnail_sha256": digest(value.get("thumbnail_sha256")),
                "thumbnail_set_at": iso(value.get("thumbnail_set_at")),
                "byte_identity_verified": False}
    return {"generated_files_removed": value.get("generated_files_removed") is True}


def source_upload_fields(metadata, download):
    # A day-only fallback is stored as UTC midnight for file timestamps. It is
    # not the original publication clock time and must not be displayed as one.
    proof = download["checks"] or {}
    precision = proof.get("time_precision") or metadata.get("time_precision")
    stamp = proof.get("source_upload_time")
    if stamp is None:
        value = metadata.get("upload_timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                stamp = iso(value)
            except (ValueError, OverflowError, OSError):
                pass
    if not stamp or precision not in {"second", "day"}:
        return {"source_upload_time": None, "source_upload_date": None, "source_upload_time_precision": None}
    parsed = datetime.fromisoformat(stamp)
    return {"source_upload_time": stamp if precision == "second" else None,
            "source_upload_date": parsed.astimezone(KST if precision == "second" else timezone.utc).date().isoformat(),
            "source_upload_time_precision": precision}


def build_report(database, config, day="yesterday", *, now=None):
    now = epoch() if now is None else now
    day = parse_day(day, now)
    start = datetime.combine(day, time.min, KST).timestamp()
    end = start + 86400
    cutoff = min(end, now)
    # A WAL read transaction yields one consistent snapshot even during downloads.
    connection = database.connection
    connection.execute("BEGIN")
    try:
        jobs = [dict(row) for row in connection.execute("SELECT * FROM videos WHERE discovered_at<=?", (cutoff,))]
        events = [dict(row) for row in connection.execute("SELECT * FROM events WHERE at<? ORDER BY at,id", (cutoff,))]
        checks = [dict(row) for row in connection.execute("SELECT * FROM verification_checks WHERE at<? ORDER BY at,id", (cutoff,))]
        runs = [dict(row) for row in connection.execute(
            "SELECT * FROM runs WHERE started_at<? AND (finished_at IS NULL OR finished_at>=?) ORDER BY id", (cutoff, start))]
    finally:
        connection.rollback()
    by_events, by_checks = {}, {}
    for item in events:
        by_events.setdefault(item["video_id"], []).append(item)
    for item in checks:
        by_checks.setdefault(item["video_id"], []).append(item)
    summary = {stage: {"videos": 0, "bytes": 0} for stage in ("download", "drive", "youtube")}
    summary.update({"discovered": sum(start <= job["discovered_at"] < cutoff for job in jobs),
                    "failed_videos": 0, "waiting_videos": 0, "failure_events": 0, "completed": 0})
    rows, states = [], Counter()
    for job in jobs:
        history, proofs = by_events.get(job["video_id"], []), by_checks.get(job["video_id"], [])
        activity = [item for item in history if start <= item["at"] < cutoff]
        daily_checks = [item for item in proofs if start <= item["at"] < cutoff]
        state = history[-1]["state"] if history else "pending"
        states[state] += 1
        size = job["file_size"] or 0
        stages = {}
        for stage, event_name in EVENTS.items():
            successful = [item for item in proofs if item["stage"] == stage and item["status"] == "verified"]
            times = [item["at"] for item in history if item["state"] == event_name] + [item["at"] for item in successful]
            # Legacy columns prove only a historical completion, never invented checks.
            column = {"drive": "drive_verified_at", "youtube": "youtube_verified_at", "cleanup": "completed_at"}.get(stage)
            legacy = job.get(column) if column else None
            if not times and legacy and legacy < cutoff:
                times = [legacy]
            first = min(times) if times else None
            new_today = first is not None and start <= first < cutoff
            if new_today:
                if stage in summary:
                    summary[stage]["videos"] += 1
                    summary[stage]["bytes"] += size
                elif stage == "cleanup":
                    summary["completed"] += 1
            latest = next((item for item in reversed(proofs) if item["stage"] == stage), None)
            status = latest["status"] if latest else "legacy_complete" if first is not None else "not_started"
            stages[stage] = {"status": status, "first_completed_at": iso(first), "newly_completed": new_today,
                             "last_checked_at": iso(latest["at"]) if latest else None,
                             "last_success_at": iso(successful[-1]["at"]) if successful else None,
                             "error": clean_error(latest["error"]) if latest else None,
                             "checks": evidence_for(stage, json.loads(successful[-1]["evidence"])) if successful else None}
        if not activity and not daily_checks:
            continue
        metadata = json.loads(job["metadata"] or "{}")
        channel_id = identifier(metadata.get("channel_id"))
        channel = next((item for item in config.channels if item.url == job["source"]), None)
        channel_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id and channel_id.startswith("UC") else channel.url if channel else None
        uploaded = identifier(job["youtube_id"], video=True)
        upload_observed = stages["youtube"]["first_completed_at"] or any(item["state"] in {"youtube_processing", "youtube_thumbnail_set"} for item in history)
        youtube_url = f"https://www.youtube.com/watch?v={uploaded}" if uploaded and upload_observed else None
        if not youtube_url and stages["youtube"]["status"] == "not_started":
            stages["youtube"]["status"] = ("disabled" if not config.youtube_enabled else "excluded" if
                                          not (channel.youtube if channel else job["youtube_allowed"]) else
                                          "not_requested" if not config.wants_youtube(job) else "not_started")
        failures = [item for item in activity if item["state"] in {"failed", "needs_reconcile"}]
        waiting = [item for item in activity if item["state"] == "waiting"]
        summary["failed_videos"] += bool(failures)
        summary["waiting_videos"] += bool(waiting)
        summary["failure_events"] += len(failures)
        rows.append({"video_id": job["video_id"], "title": metadata.get("title") or job["title"] or job["video_id"],
                     "source_url": f"https://www.youtube.com/watch?v={job['video_id']}",
                     "channel": metadata.get("channel") or (channel.name if channel else None) or channel_id or "채널 미확인",
                     **source_upload_fields(metadata, stages["download"]),
                     "downloaded_at": stages["download"]["first_completed_at"],
                     "channel_url": channel_url, "file_size": size, "sha256": digest(job["sha256"]),
                     "state": state, "youtube_url": youtube_url, "stages": stages,
                     "activity": [{"at": iso(item["at"]), "state": item["state"], "error": clean_error(item["detail"])} for item in activity],
                     "error": clean_error(history[-1]["detail"]) if history else None})
    run_details = []
    for item in runs:
        finished = item["finished_at"]
        known = finished is not None and finished < cutoff
        details = json.loads(item["summary"]) if known else {}
        cookies = details.get("cookies", {})
        cookies_status = cookies.get("status") if isinstance(cookies, dict) else None
        youtube_token = details.get("youtube_token", {})
        token_status = youtube_token.get("status") if isinstance(youtube_token, dict) else None
        run_details.append({"started_at": iso(item["started_at"]), "finished_at": iso(finished) if known else None,
                            "status": item["status"] if known else "unfinished_at_cutoff",
                            "cookies_status": cookies_status if cookies_status in {"updated", "unchanged", "disabled", "failed"} else None,
                            "youtube_token_status": token_status if token_status in {"updated", "unchanged", "disabled", "failed"} else None,
                            "scan_failures": len(details.get("scan_failures", [])), "error": clean_error(details.get("error"))})
    warnings = []
    if not runs:
        warnings.append("대상 기간의 수집 실행 기록이 없습니다. 0건은 정상 실행을 보장하지 않습니다.")
    if any(item["status"] != "success" for item in run_details):
        warnings.append("실패·중단·미완료 수집 실행이 있습니다. 서비스 로그와 인증 상태를 확인하세요.")
    if any(item["scan_failures"] for item in run_details):
        warnings.append("채널 목록 조회 실패가 있어 신규 영상을 놓쳤을 수 있습니다.")
    return {"schema_version": 2, "date": day.isoformat(), "timezone": "Asia/Seoul", "period_start": iso(start),
            "period_end_exclusive": iso(end), "data_through": iso(cutoff), "period_closed": now >= end,
            "generated_at": iso(now), "summary": summary, "states_at_cutoff": dict(states), "runs": run_details,
            "settings_now": {"youtube_enabled": config.youtube_enabled, "channels": len(config.channels)},
            "warnings": warnings, "videos": rows,
            "notes": ["성공은 검증 완료 기준입니다. 표는 마지막 확인 상태이며, 실패 건수는 기간 중 한 번이라도 실패한 영상을 셉니다.",
                      "대기·미실행·사용 안 함은 실패로 세지 않습니다. 전체 완료는 켜진 모든 대상의 검증과 Oracle 임시 파일 정리 완료 기준입니다.",
                      "완료 건수·용량은 영상별 최초 완료일에 한 번만 집계하며, 용량은 영상 파일 기준입니다."]}


def md(value):
    value = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()|#!])", r"\\\1", value)


def readable_size(size):
    return f"{size / 1024**3:.3f} GiB ({size:,}바이트)"


def display_time(value):
    return datetime.fromisoformat(value).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S") if value else "미확인"


def display_day(value):
    day = date.fromisoformat(value)
    return f"{day.year}년 {day.month}월 {day.day}일"


def display_download_time(value):
    return datetime.fromisoformat(value).astimezone(KST).strftime("%H:%M:%S") if value else "—"


def display_run_time(started_at, finished_at, *, report_day=None):
    started = datetime.fromisoformat(started_at).astimezone(KST)
    day = date.fromisoformat(report_day) if report_day else started.date()

    def clock(value):
        offset = (value.date() - day).days
        prefix = {-1: "전날 ", 0: "", 1: "다음날 "}.get(offset, f"{abs(offset)}일 {'후' if offset > 0 else '전'} ")
        return f"{prefix}{value:%H:%M:%S}"

    if not finished_at:
        return clock(started) + " ~ 종료 기록 없음"
    finished = datetime.fromisoformat(finished_at).astimezone(KST)
    # Relative day labels keep midnight-spanning runs clear without dates.
    return clock(started) + " ~ " + clock(finished)


def display_upload_time(video):
    if video.get("source_upload_time_precision") == "second" and video.get("source_upload_time"):
        return datetime.fromisoformat(video["source_upload_time"]).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    if video.get("source_upload_date"):
        return video["source_upload_date"]
    return "—"


def table_stage(video, stage):
    result = video["stages"][stage]
    label = TABLE_LABELS[result["status"]]
    url = None
    if stage == "drive":
        url = next((item["url"] for item in (result["checks"] or {}).get("files", []) if item["kind"] == "video"), None)
    elif stage == "youtube":
        url = video["youtube_url"]
    # A retained link can help diagnose a failed recheck; the label still shows
    # the latest failed/waiting result rather than a past success.
    return f"{label} ([저장본]({url}))" if url else label


def render_markdown(report):
    summary = report["summary"]
    lines = [f"# 멤버십 영상 다운로드 기록 - {display_day(report['date'])}", "", f"- 작업일: {display_day(report['date'])}",
             f"- 보고서 생성: {display_time(report['generated_at'])} / {'일일 집계' if report['period_closed'] else '진행 중 집계'}", "",
             "## 처리량", "", "| 단계 | 최초 완료 영상 | 영상 파일 용량 |", "| --- | ---: | ---: |"]
    for stage in ("download", "drive", "youtube"):
        lines.append(f"| {STAGES[stage]} | {summary[stage]['videos']}개 | {readable_size(summary[stage]['bytes'])} |")
    lines += ["", f"- 전체 흐름 완료·정리: {summary['completed']}개", f"- 실패가 발생한 영상: {summary['failed_videos']}개 / 실패 이벤트: {summary['failure_events']}회",
              f"- 대기가 발생한 영상: {summary['waiting_videos']}개", f"- 수집 실행 기록: {len(report['runs'])}회", "",
              "## 실행 상태", "", f"- 생성 당시 YouTube 옵션: {'켜짐' if report['settings_now']['youtube_enabled'] else '꺼짐'}",
              f"- 생성 당시 설정 채널: {report['settings_now']['channels']}개",
              "- 대상 기간 종료 시점 상태: " + (", ".join(f"{STATE_LABELS.get(key, md(key))} {count}개" for key, count in report['states_at_cutoff'].items()) or "등록 영상 없음"), ""]
    lines += [f"- 주의: {md(value)}" for value in report["warnings"]]
    for item in report["runs"]:
        interval = display_run_time(item["started_at"], item["finished_at"], report_day=report["date"])
        lines.append(f"- 실행 시간: {interval} / 결과: {md(item['status'])}, 채널 조회 실패 {item['scan_failures']}건")
        cookie_label = {"updated": "교체 완료", "unchanged": "변경 없음", "disabled": "자동 교체 꺼짐", "failed": "실패"}.get(item.get("cookies_status"))
        if cookie_label:
            lines.append(f"  - 쿠키 파일: {cookie_label}")
        token_label = {"updated": "수신·확인 완료", "unchanged": "변경 없음", "disabled": "자동 수신 꺼짐", "failed": "실패"}.get(item.get("youtube_token_status"))
        if token_label:
            lines.append(f"  - YouTube 업로드 토큰: {token_label}")
        if item["error"]:
            lines.append(f"  - 오류: {md(item['error'])}")
    lines += ["", "## 영상별 처리 내역", "", "업로드 시각은 원본 게시 시각, 다운로드 시각은 최초 완료 시각입니다.", ""]
    if not report["videos"]:
        lines += ["이 기간에 처리·검증 이벤트가 기록된 영상이 없습니다.", ""]
    else:
        lines += ["| 채널 | 영상 제목 | 업로드 시각 | 영상 링크 | 영상 다운로드 | 드라이브 저장 | 유튜브 업로드 | 다운로드 시각 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for video in report["videos"]:
            cells = [md(video["channel"]), md(video["title"]), display_upload_time(video),
                     f"[원본 영상]({video['source_url']})", table_stage(video, "download"),
                     table_stage(video, "drive"), table_stage(video, "youtube"), display_download_time(video.get("downloaded_at"))]
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", "## 영상별 검증·오류 상세", ""]
    for video in report["videos"]:
        lines += [f"### {md(video['channel'])} — {md(video['title'])}", "",
                  f"- 원본: [영상 보기]({video['source_url']}) · ID: {md(video['video_id'])}",
                  f"- 영상 크기: {readable_size(video['file_size'])}",
                  f"- 기간 종료 시점 상태: {STATE_LABELS.get(video['state'], md(video['state']))}"]
        if video["channel_url"]:
            lines.append(f"- 원본 채널: [채널 보기]({video['channel_url']})")
        if video["youtube_url"]:
            lines.append(f"- 업로드된 YouTube 영상: [영상 확인]({video['youtube_url']})")
        for stage, result in video["stages"].items():
            lines.append(f"- **{STAGES[stage]}: {LABELS[result['status']]}**")
            if result["first_completed_at"]:
                lines.append(f"  - 최초 완료: {result['first_completed_at']} / 이번 날짜 신규 완료: {'예' if result['newly_completed'] else '아니오'}")
            if result["last_checked_at"]:
                lines.append(f"  - 마지막 확인: {result['last_checked_at']} / 마지막 성공: {result['last_success_at'] or '없음'}")
            proof = result["checks"]
            if proof:
                if stage == "download":
                    lines.append(f"  - 영상 스트림·길이 확인: {'통과' if proof['media_probe'] else '미확인'} / SHA-256: {proof['sha256'] or '없음'}")
                    lines.append(f"  - 파일 수정일 기준: 원본 업로드 {proof['source_upload_time'] or '미확인'} / 정밀도: {'날짜 (UTC 자정)' if proof['time_precision'] == 'day' else '초' if proof['time_precision'] == 'second' else '미확인'}")
                elif stage == "drive":
                    for file in proof["files"]:
                        label = {"video": "영상", "metadata": "설명·메타데이터", "thumbnail": "썸네일"}[file["kind"]]
                        link = f"[파일 확인]({file['url']})" if file["url"] else "직접 링크 없음"
                        checks = " / ".join(f"{name}: {'일치' if file[key] else '미확인'}" for key, name in
                                            (("size_matches", "크기"), ("sha256_matches", "전체 SHA-256"), ("mtime_matches", "원본 업로드 시각")))
                        lines.append(f"  - {label}: {link} / {checks}")
                elif stage == "youtube":
                    checks = " / ".join(f"{name}: {'확인' if proof[key] else '미확인'}" for key, name in
                                        (("processing_complete", "처리 완료"), ("description_matches", "원문 설명 일치"), ("custom_thumbnail_present", "맞춤 썸네일 반영")))
                    lines.append(f"  - 일부 공개: {'확인' if proof['privacy'] == 'unlisted' else '미확인'} / {checks}")
                elif stage == "cleanup":
                    lines.append("  - 생성된 임시 파일 정리 완료 기록 있음")
            if result["error"]:
                lines.append(f"  - 마지막 확인 오류: {md(result['error'])}")
        if video["error"]:
            lines.append(f"- 마지막 상태 오류: {md(video['error'])}")
        lines += ["", "처리 시각:", ""]
        lines += [f"- {display_download_time(item['at'])} — {STATE_LABELS.get(item['state'], md(item['state']))}" + (f" / {md(item['error'])}" if item['error'] else "") for item in video["activity"]]
        lines.append("")
    lines += ["## 집계 기준", ""] + [f"- {value}" for value in report["notes"]]
    return "\n".join(lines) + "\n"


def write_report(database, config, day="yesterday", *, drive=None):
    root = config.state_dir / "reports"
    if root.is_symlink():
        raise ArchiveError("보고서 폴더에 심볼릭 링크를 사용할 수 없습니다.")
    private_dir(root)
    with run_lock(root):
        report = build_report(database, config, day)
        paths = {ext: root / f"{report['date']}.{ext}" for ext in ("json", "md")}
        for path in (*paths.values(), root / "latest.json", root / "latest.md"):
            if path.is_symlink():
                raise ArchiveError("보고서 파일에 심볼릭 링크를 사용할 수 없습니다.")
        atomic_json(paths["json"], report)
        atomic_text(paths["md"], render_markdown(report))
        # Historical regeneration must never replace a newer latest report.
        latest = root / "latest.json"
        update_latest = not latest.exists() or json.loads(latest.read_text(encoding="utf-8"))["date"] <= report["date"]
        if update_latest:
            atomic_json(latest, report)
            atomic_text(root / "latest.md", render_markdown(report))
        publication = {"date": report["date"], "generated_at": report["generated_at"],
                       "local_files": {ext: str(path) for ext, path in paths.items()}, "drive": "not_requested"}
        if drive is not None:
            try:
                drive.validate_backend()
                # latest.json is published last so readers see a committed snapshot.
                for path in [paths["md"], paths["json"], *([root / "latest.md", latest] if update_latest else [])]:
                    drive.publish_report(path, REPORTS_DIRECTORY + "/" + path.name)
                publication["drive"] = "verified"
            except Exception as error:
                publication.update(drive="failed", error=safe_error(error))
        atomic_json(root / (report["date"] + ".publication.json"), publication)
        return publication
