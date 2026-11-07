from __future__ import annotations

import argparse
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import shutil
import sys

from .common import ArchiveError, LockBusy, epoch, run_lock, safe_error, video_url
from .config import load_config
from .cookies import sync_drive_cookies
from .db import Database
from .drive import Drive
from .failure_logs import record_error
from .media import Downloader
from .runner import Runner
from .reports import write_report
from .sheet_control import GoogleSheets
from .scheduling import schedule_tick
from .youtube import YouTube, authorize
from .youtube_tokens import sync_drive_youtube_token


def parser():
    result = argparse.ArgumentParser(description="YouTube → Google Drive → 선택적 YouTube 일부 공개 업로드")
    result.add_argument("--config", default="config.toml", help="TOML 설정 파일 (기본: config.toml)")
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="영상 발견 및 대기 작업 처리")
    run.add_argument("--no-discover", action="store_true", help="목록 조회 없이 등록된 작업만 처리")
    commands.add_parser("discover", help="채널 목록을 조회해 작업만 등록 (영상 다운로드·업로드 없음)")
    scheduled = commands.add_parser("scheduled-run", help="Drive 시트의 자동 실행 설정을 확인하고 실행 시각이면 수집")
    scheduled.set_defaults(no_discover=False)
    commands.add_parser("status", help="처리 이력 JSON 조회 (인증 정보 제외)")
    report = commands.add_parser("report", help="채널·영상별 일일 검증 보고서 작성 (기본: 어제, 한국시간)")
    report.add_argument("--date", default="yesterday", help="today, yesterday 또는 YYYY-MM-DD")
    report.add_argument("--local-only", action="store_true", help="Drive 게시 없이 로컬 보고서만 작성")
    commands.add_parser("doctor", help="설정·프로그램·인증 파일 점검 (원격 쓰기 없음)")
    add = commands.add_parser("add", help="영상 URL 또는 ID 직접 등록")
    add.add_argument("url")
    add.add_argument("--no-youtube", action="store_true")
    retry = commands.add_parser("retry", help="보존된 단계에서 즉시 재시도하도록 예약")
    retry.add_argument("video_id")
    commands.add_parser("auth-youtube", help="로컬 브라우저에서 YouTube OAuth 승인 (영상 업로드 없음)")
    commands.add_parser("sync-youtube-token", help="Drive에서 변경된 업로드 토큰만 받아 검증 (영상 수집·업로드 없음)")
    link = commands.add_parser("link-youtube", help="Studio에서 찾은 업로드 ID를 검증해 연결")
    link.add_argument("video_id")
    link.add_argument("youtube_id")
    reset = commands.add_parser("reset-youtube", help="Studio에서 업로드가 없음을 확인한 후 세션 초기화")
    reset.add_argument("video_id")
    reset.add_argument("--confirm-no-upload", action="store_true", required=True)
    return result


def doctor(config):
    checks = []
    def check(name, ok):
        checks.append({"check": name, "ok": bool(ok)})
    for package in ("yt-dlp", "google-auth-oauthlib", "requests"):
        try:
            version(package)
            check(package, True)
        except PackageNotFoundError:
            check(package, False)
    for name in (config.ffmpeg, config.ffprobe, config.rclone, config.js_runtime):
        check("program: " + name, shutil.which(name) is not None)
    if not config.cookies_from_drive:
        check("youtube cookies", config.cookies_file.is_file())
    check("rclone config", config.rclone_config.is_file())
    drive = Drive(config)
    try:
        drive.validate_backend()
        check("Google Drive remote", True)
    except ArchiveError:
        check("Google Drive remote", False)
    if config.cookies_from_drive:
        try:
            drive.cookie_snapshot()
            check("Drive youtube-cookies.txt metadata", True)
        except ArchiveError:
            check("Drive youtube-cookies.txt metadata", False)
    if config.youtube_enabled:
        check("youtube OAuth token", config.youtube_token.is_file())
    if config.youtube_token_from_drive:
        try:
            drive.youtube_token_snapshot()
            check("Drive youtube-token.json metadata", True)
        except ArchiveError:
            check("Drive youtube-token.json metadata", False)
    print(json.dumps({"checks": checks, "youtube_enabled": config.youtube_enabled,
                      "channels": len(config.channels), "extra_urls": len(config.extra_urls)}, ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in checks) else 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    config = None
    database = None
    active_run = None
    cookies = {"status": "disabled"}
    youtube_token = {"status": "disabled"}
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "report":
            # WAL snapshot permits reporting while a long download is running.
            database = Database(config.state_dir)
            result = write_report(database, config, args.date,
                                  drive=Drive(config) if config.reports_drive and not args.local_only else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["drive"] == "failed" else 0
        # One lock covers all state/credential writers, including OAuth and discovery.
        with run_lock(config.state_dir):
            if args.command == "auth-youtube":
                authorize(config)
                print("YouTube OAuth 토큰을 저장했습니다. channel_id를 확인한 뒤 enabled를 켜세요.")
                return 0
            if args.command in {"run", "discover", "scheduled-run"} and config.control_sheet_id:
                database = Database(config.state_dir)
                try:
                    controls = GoogleSheets(config).read()
                    config = replace(config, channels=controls.channels)
                    if args.command == "scheduled-run":
                        timing = schedule_tick(controls.schedule, config.state_dir / "schedule-state.json", epoch())
                        if timing["status"] != "due":
                            print(json.dumps({"schedule": timing}, ensure_ascii=False))
                            return 0
                except Exception:
                    active_run = database.start_run()
                    raise
            elif args.command == "scheduled-run":
                raise ArchiveError("자동 실행에는 control.spreadsheet_id 설정이 필요합니다.")
            if database is None:
                database = Database(config.state_dir)
            if args.command == "status":
                print(json.dumps(database.public_status(), ensure_ascii=False, indent=2))
            elif args.command == "add":
                video_id, url = video_url(args.url)
                database.add(video_id, url, youtube_allowed=not args.no_youtube)
                print(json.dumps({"added": video_id}, ensure_ascii=False))
            elif args.command in {"retry", "link-youtube", "reset-youtube"}:
                video_id, _ = video_url(args.video_id)
                job = database.get(video_id)
                if job is None:
                    raise ArchiveError("등록되지 않은 원본 영상 ID입니다.")
                if args.command == "link-youtube":
                    uploaded, _ = video_url(args.youtube_id)
                    if not config.youtube_enabled or not job["drive_verified_at"]:
                        raise ArchiveError("Drive 검증 완료 및 YouTube 활성화 설정이 필요합니다.")
                    youtube = YouTube(config)
                    youtube.check_channel()
                    youtube.verify(uploaded, youtube.original_description(database.metadata(video_id)))
                    database.update(video_id, youtube_id=uploaded, youtube_channel=config.youtube_channel,
                                    youtube_verified_at=None, youtube_thumbnail_sha256=None, youtube_thumbnail_set_at=None,
                                    retry_at=0, error=None, state="youtube_processing")
                elif args.command == "reset-youtube":
                    if job["youtube_verified_at"]:
                        raise ArchiveError("이미 검증된 YouTube 업로드는 초기화하지 않습니다.")
                    database.update(video_id, youtube_id=None, youtube_session=None, youtube_channel=None,
                                    youtube_thumbnail_sha256=None, youtube_thumbnail_set_at=None,
                                    retry_at=0, error=None, state="pending")
                else:
                    database.update(video_id, retry_at=0, error=None, state="pending")
                print(json.dumps({"updated": video_id}, ensure_ascii=False))
            else:
                active_run = database.start_run()
                drive = None
                if config.youtube_token_from_drive:
                    youtube_token = {"status": "failed"}
                    drive = Drive(config)
                    drive.validate_backend()
                    youtube_token = sync_drive_youtube_token(config, drive)
                if args.command == "sync-youtube-token":
                    result = {"command": args.command, "youtube_token": youtube_token,
                              "youtube_enabled": config.youtube_enabled}
                    database.finish_run(active_run, "success", result)
                    active_run = None
                    print(json.dumps(result, ensure_ascii=False))
                    return 0
                if config.cookies_from_drive:
                    # One startup check under the collection lock, before any YouTube access.
                    cookies = {"status": "failed"}
                    if drive is None:
                        drive = Drive(config)
                        drive.validate_backend()
                    cookies = sync_drive_cookies(config, drive)
                downloader = Downloader(config)
                failures = []
                if args.command == "discover" or not args.no_discover:
                    failures = downloader.discover(database)
                if args.command == "discover":
                    database.finish_run(active_run, "failed" if failures else "success",
                                        {"command": "discover", "scan_failures": failures, "cookies": cookies,
                                         "youtube_token": youtube_token})
                    active_run = None
                    print(json.dumps({"registered": len(database.public_status()), "scan_failures": failures,
                                      "cookies": cookies, "youtube_token": youtube_token}, ensure_ascii=False))
                    return 1 if failures else 0
                if drive is None:
                    drive = Drive(config)
                    drive.validate_backend()
                downloader.capacity_check = drive.ensure_capacity
                counts = Runner(config, database, downloader, drive).run()
                failed = bool(failures or counts.get("failed") or counts.get("needs_reconcile"))
                database.finish_run(active_run, "failed" if failed else "success",
                                    {"command": "run", "results": counts, "scan_failures": failures, "cookies": cookies,
                                     "youtube_token": youtube_token})
                active_run = None
                reports = []
                if config.reports_enabled:
                    for day in ("yesterday", "today"):
                        try:
                            reports.append(write_report(database, config, day, drive=drive if config.reports_drive else None))
                        except Exception as error:
                            reports.append({"date": day, "drive": "failed", "error": safe_error(error)})
                print(json.dumps({"results": counts, "scan_failures": failures, "cookies": cookies, "youtube_token": youtube_token,
                                  "reports": reports}, ensure_ascii=False))
                return 1 if failed or any(item["drive"] == "failed" for item in reports) else 0
        return 0
    except Exception as error:
        if isinstance(error, LockBusy) and args.command == "scheduled-run":
            print(json.dumps({"schedule": {"status": "busy"}}, ensure_ascii=False))
            return 0
        try:
            record_error(args.config, config, args.command, error)
        except Exception:
            print("별도 오류 파일 저장에도 실패했습니다. 서비스 로그를 확인하세요.", file=sys.stderr)
        if database is not None and active_run is not None:
            try:
                database.finish_run(active_run, "failed", {"error": safe_error(error), "cookies": cookies, "youtube_token": youtube_token,
                                                          "command": args.command})
            except Exception:
                print("실행 실패 기록의 DB 저장에도 실패했습니다. 별도 오류 파일을 확인하세요.", file=sys.stderr)
            if config.reports_enabled:
                ready_days = []
                # Persist both local reports before attempting any remote connection.
                for day in ("yesterday", "today"):
                    try:
                        write_report(database, config, day)
                        ready_days.append(day)
                    except Exception:
                        print("실패 기록의 로컬 보고서 작성에도 실패했습니다.", file=sys.stderr)
                if config.reports_drive and ready_days:
                    try:
                        # The failed collection has released its lock. Serialize rclone
                        # and credential writes again; a busy worker keeps local logs.
                        with run_lock(config.state_dir):
                            error_drive = Drive(config)
                            for day in reversed(ready_days):
                                result = write_report(database, config, day, drive=error_drive)
                                if result["drive"] != "verified":
                                    print("오류 보고서의 Drive 저장에 실패했습니다. Oracle의 로컬 기록은 보존합니다.", file=sys.stderr)
                                    break
                    except Exception:
                        print("오류 보고서를 Drive에 저장하지 못했습니다. Oracle의 로컬 기록은 보존합니다.", file=sys.stderr)
        print(safe_error(error), file=sys.stderr)
        return 1
    finally:
        if database:
            database.close()
