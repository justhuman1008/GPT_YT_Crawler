from dataclasses import replace
from datetime import datetime, timezone
import contextlib
import io
import json
from unittest.mock import Mock, patch

from test_archive import CHANNEL, SOURCE, SECOND, UPLOADED, STAMP, JPEG, Response, Fixture, FakeDownloader, FakeDrive
from yt_archive.cli import main
from yt_archive.common import ArchiveError, run_lock, sha256
from yt_archive.config import Channel
from yt_archive.drive import Drive
from yt_archive.layout import REPORTS_DIRECTORY
from yt_archive.reports import KST, build_report, parse_day, render_markdown, write_report
from yt_archive.runner import Runner
from yt_archive.youtube import YouTube


START = datetime(2026, 10, 25, tzinfo=KST).timestamp()
END = START + 86400
NOW = END + 3600
DAY = "2026-10-25"


class ReportDrive(FakeDrive):
    def __init__(self):
        super().__init__()
        self.reports = {}
        self.publish_failure = False

    def validate_backend(self):
        pass

    def verify(self, *args):
        result = super().verify(*args)
        return {**result, "ID": "safe-drive-id", "ModTime": "2024-10-04T00:00:00Z"}

    def publish_report(self, path, target):
        if self.publish_failure:
            raise ArchiveError("보고서 업로드 검증 실패")
        self.reports[target] = path.read_bytes()


class ReportTests(Fixture):
    def at(self, stamp):
        return patch("yt_archive.db.epoch", return_value=stamp)

    def completed(self, video=SOURCE, stamp=START+60):
        with self.at(stamp):
            job = self.add(video, source="https://www.youtube.com/@source")
            drive = ReportDrive()
            runner = Runner(self.config, self.db, FakeDownloader(), drive, Mock(side_effect=AssertionError("YouTube disabled")))
            self.assertEqual(runner.process(job), "complete")
        return drive

    def report(self, day=DAY, now=NOW):
        return build_report(self.db, self.config, day, now=now)

    def test_real_pipeline_records_each_stage_without_youtube_access(self):
        self.config = replace(self.config, channels=(Channel("설정 채널", "https://www.youtube.com/@source", ("videos",)),))
        self.completed()
        report = self.report()
        self.assertEqual(report["summary"]["download"]["videos"], 1)
        self.assertEqual(report["summary"]["drive"]["bytes"], len(b"complete-video-original-bytes"))
        row = report["videos"][0]
        self.assertEqual(row["title"], "멤버십 영상")
        self.assertEqual(row["channel"], "설정 채널")
        self.assertEqual(row["channel_url"], "https://www.youtube.com/channel/"+CHANNEL)
        self.assertEqual(row["stages"]["download"]["status"], "verified")
        self.assertTrue(row["stages"]["download"]["checks"]["media_probe"])
        self.assertEqual(row["stages"]["youtube"]["status"], "disabled")
        self.assertEqual(row["stages"]["cleanup"]["status"], "verified")
        self.assertEqual(row["source_upload_time"], datetime.fromtimestamp(STAMP, KST).isoformat(timespec="seconds"))
        self.assertEqual(row["source_upload_time_precision"], "second")
        self.assertEqual(row["downloaded_at"], "2026-10-25T00:01:00+09:00")
        self.assertIsNone(self.db.get(SOURCE)["local_path"])
        text = render_markdown(report)
        self.assertIn("| 채널 | 영상 제목 | 업로드 시각 | 영상 링크 | 영상 다운로드 | 드라이브 저장 | 유튜브 업로드 | 다운로드 시각 |", text)
        self.assertIn("작업일: 2026년 10월 25일", text)
        self.assertIn("| 설정 채널 | 멤버십 영상 | 2024-10-04 09:00 |", text)
        self.assertIn("| 사용 안 함 | 00:01:00 |", text)
        self.assertIn("전체 SHA-256: 일치", text)
        self.assertIn("https://drive.google.com/file/d/safe-drive-id/view", text)
        self.assertIn("멤버십 영상", text)

    def test_retry_or_later_youtube_backfill_never_recounts_download_and_drive(self):
        self.completed(stamp=START-3600)
        with self.at(START+30):
            self.db.update(SOURCE, state="drive_verified", drive_verified_at=START+30)
            self.db.record_check(SOURCE, "drive", {"files": []})
            self.db.update(SOURCE, state="youtube_verified", youtube_id=UPLOADED, youtube_verified_at=START+30)
            self.db.update(SOURCE, state="complete", completed_at=START+30)
        report = self.report()
        self.assertEqual(report["summary"]["download"]["videos"], 0)
        self.assertEqual(report["summary"]["drive"]["videos"], 0)
        self.assertEqual(report["summary"]["youtube"]["videos"], 1)
        self.assertEqual(report["summary"]["completed"], 0)
        self.assertEqual(report["videos"][0]["youtube_url"], "https://www.youtube.com/watch?v="+UPLOADED)
        self.assertEqual(report["videos"][0]["downloaded_at"], "2026-10-24T23:00:00+09:00")

    def test_midnight_is_korea_time_and_end_is_exclusive(self):
        with self.at(START-1):
            self.add()
            self.db.update(SOURCE, state="downloaded", file_size=100)
        with self.at(START):
            self.db.update(SOURCE, state="drive_verified")
        with self.at(END):
            self.db.update(SOURCE, state="youtube_verified", youtube_id=UPLOADED)
        report = self.report()
        self.assertEqual(report["summary"]["download"]["videos"], 0)
        self.assertEqual(report["summary"]["drive"]["videos"], 1)
        self.assertEqual(report["summary"]["youtube"]["videos"], 0)
        self.assertEqual(report["videos"][0]["state"], "drive_verified")
        self.assertIsNone(report["videos"][0]["youtube_url"])

    def test_previous_success_does_not_hide_a_later_failed_drive_check(self):
        self.completed()
        with self.at(START+120):
            self.db.record_check(SOURCE, "drive", status="failed", error="Drive 보관본 변경")
            self.db.update(SOURCE, state="failed", error="Drive 보관본 변경")
        report = self.report()
        self.assertEqual(report["videos"][0]["stages"]["drive"]["status"], "failed")
        self.assertIsNotNone(report["videos"][0]["stages"]["drive"]["last_success_at"])
        self.assertEqual(report["summary"]["failed_videos"], 1)

    def test_failed_download_and_youtube_wait_are_explicit(self):
        with self.at(START+60):
            job = self.add()
            downloader = FakeDownloader()
            downloader.fail = ArchiveError("다운로드 실패")
            self.assertEqual(Runner(self.config, self.db, downloader, FakeDrive()).process(job), "failed")
            self.add(SECOND)
            self.db.update(SECOND, state="youtube_processing", youtube_id=UPLOADED)
            self.db.record_check(SECOND, "youtube", status="waiting", error="처리 대기")
            self.db.update(SECOND, state="waiting", error="처리 대기")
        report = self.report()
        rows = {row["video_id"]: row for row in report["videos"]}
        self.assertEqual(rows[SOURCE]["stages"]["download"]["status"], "failed")
        self.assertEqual(rows[SECOND]["stages"]["youtube"]["status"], "waiting")
        self.assertIsNotNone(rows[SECOND]["youtube_url"])
        self.assertEqual(report["summary"]["youtube"]["videos"], 0)
        self.assertEqual(report["summary"]["waiting_videos"], 1)
        self.assertIsNone(rows[SOURCE]["downloaded_at"])
        self.assertIsNone(rows[SOURCE]["source_upload_time"])
        text = render_markdown(report)
        table = text.split("## 영상별 처리 내역", 1)[1].split("## 영상별 검증·오류 상세", 1)[0]
        self.assertIn("| 실패 | 미실행 / 미완료 | 사용 안 함 | — |", table)
        self.assertIn("대기 ([저장본](https://www.youtube.com/watch?v="+UPLOADED+")", table)

    def test_date_only_upload_is_not_shown_as_a_midnight_or_kst_clock_time(self):
        stamp = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
        with self.at(START+60):
            self.add()
            self.db.update(SOURCE, state="downloaded", metadata=json.dumps({"upload_timestamp": stamp, "time_precision": "day"}))
            self.db.record_check(SOURCE, "download", {"upload_timestamp": stamp, "time_precision": "day"})
        row = self.report()["videos"][0]
        self.assertIsNone(row["source_upload_time"])
        self.assertEqual(row["source_upload_date"], "2026-10-24")
        self.assertEqual(row["source_upload_time_precision"], "day")
        table = render_markdown(self.report()).split("## 영상별 검증·오류 상세", 1)[0]
        self.assertIn("| 2026-10-24 |", table)
        self.assertNotIn("시각 미확인", table)
        self.assertNotIn("2026-10-24 09:00:00", table)

    def test_legacy_metadata_can_supply_upload_time_without_inventing_download_time(self):
        with self.at(START+60):
            self.add()
            self.db.update(SOURCE, state="drive_verified", metadata=json.dumps({"upload_timestamp": STAMP, "time_precision": "second"}))
        row = self.report()["videos"][0]
        self.assertEqual(row["source_upload_time"], "2024-10-04T09:00:00+09:00")
        self.assertIsNone(row["downloaded_at"])

    def test_video_table_escapes_pipes_and_newlines_in_channel_and_title(self):
        self.completed()
        metadata = self.db.metadata(SOURCE)
        metadata.update(title="제목 | [특별편]\n두 번째 줄", channel="채널 | A\n방송")
        self.db.update(SOURCE, metadata=json.dumps(metadata, ensure_ascii=False))
        table = render_markdown(self.report()).split("## 영상별 처리 내역", 1)[1].split("## 영상별 검증·오류 상세", 1)[0]
        row = next(line for line in table.splitlines() if "[원본 영상]" in line)
        self.assertIn(r"채널 \| A 방송", row)
        self.assertIn(r"제목 \| \[특별편\] 두 번째 줄", row)
        self.assertEqual(len([line for line in table.splitlines() if "[원본 영상]" in line]), 1)

    def test_legacy_stage_is_not_fabricated_detailed_verification(self):
        with self.at(START+30):
            self.add()
            self.db.update(SOURCE, state="drive_verified", drive_verified_at=START+30)
        result = self.report()["videos"][0]["stages"]["drive"]
        self.assertEqual(result["status"], "legacy_complete")
        self.assertIsNone(result["checks"])

    def test_youtube_report_uses_receipt_after_actual_description_thumbnail_checks(self):
        self.config = replace(self.config, youtube_enabled=True, youtube_channel=CHANNEL)
        path, image = self.root / "media.mkv", self.root / "thumbnail.jpg"
        path.write_bytes(b"video")
        image.write_bytes(JPEG)
        metadata = {"title": "영상", "description": "원문 설명", "channel": "원본 채널", "thumbnail": {
            "filename": image.name, "mime_type": "image/jpeg", "file_size": len(JPEG), "sha256": sha256(image)}}
        processed = Response(data={"items": [{"snippet": {"channelId": CHANNEL, "description": "원문 설명"},
                                              "status": {"privacyStatus": "unlisted", "uploadStatus": "processed"},
                                              "processingDetails": {"processingStatus": "succeeded"},
                                              "contentDetails": {"hasCustomThumbnail": True}}]})
        session = Mock()
        session.request.side_effect = [Response(data={"items": [{"id": CHANNEL}]}), processed,
                                       Response(data={"items": [{"default": {"url": "https://i.ytimg.com/new.jpg"}}]}), processed]
        with self.at(START+60):
            self.add()
            self.db.update(SOURCE, youtube_id=UPLOADED, file_size=5, metadata=json.dumps(metadata))
            YouTube(self.config, session).ensure(self.db, self.db.get(SOURCE), path, metadata)
        report = self.report()
        stage = report["videos"][0]["stages"]["youtube"]
        self.assertEqual(stage["status"], "verified")
        self.assertTrue(stage["checks"]["description_matches"])
        self.assertTrue(stage["checks"]["custom_thumbnail_present"])
        self.assertFalse(stage["checks"]["byte_identity_verified"])
        self.assertIn("원문 설명 일치: 확인", render_markdown(report))
        self.assertEqual(report["summary"]["youtube"]["videos"], 1)

    def test_report_contains_only_safe_fields_and_escapes_markdown(self):
        with self.at(START+30):
            self.add()
            self.db.update(SOURCE, state="youtube_verified", youtube_id=UPLOADED, youtube_verified_at=START+30,
                           youtube_session="https://upload.youtube.com/resumable/SECRET_SESSION",
                           metadata=json.dumps({"title": "<script>[click](javascript:alert(1))", "channel": "채널",
                                                "description": "SECRET_DESCRIPTION", "cookies": "SECRET_COOKIES"}),
                           error="access_token=SECRET_ACCESS https://host/?upload_id=SECRET_ID")
            self.db.record_check(SOURCE, "youtube", {"privacy": "unlisted", "processing_complete": True,
                                 "description_matches": True, "custom_thumbnail_present": True,
                                 "token": "SECRET_EVIDENCE", "raw_response": "SECRET_RESPONSE"})
        report = self.report()
        serialized = json.dumps(report)+render_markdown(report)
        self.assertNotIn("SECRET_", serialized)
        self.assertNotIn("<script>", render_markdown(report))
        self.assertIn("&lt;script&gt;", render_markdown(report))
        self.assertFalse(report["videos"][0]["stages"]["youtube"]["checks"]["byte_identity_verified"])

    def test_zero_without_run_is_not_reported_as_normal_operation(self):
        report = self.report()
        self.assertEqual(report["videos"], [])
        self.assertTrue(report["warnings"])
        with self.at(START+60):
            run = self.db.start_run()
            self.db.finish_run(run, "failed", {"scan_failures": ["source"]})
        self.assertTrue(any("목록 조회 실패" in item for item in self.report()["warnings"]))

    def test_live_report_is_a_consistent_read_while_collection_lock_is_held(self):
        self.completed()
        with run_lock(self.config.state_dir), patch("yt_archive.cli.Drive", side_effect=AssertionError("offline report")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--config", str(self.config_path), "report", "--local-only", "--date", "today"]), 0)
        self.assertEqual(json.loads(output.getvalue())["drive"], "not_requested")

    def test_publication_is_verified_and_latest_committed_last(self):
        drive = self.completed()
        with patch("yt_archive.reports.epoch", return_value=NOW):
            result = write_report(self.db, self.config, DAY, drive=drive)
        self.assertEqual(result["drive"], "verified")
        self.assertEqual(list(drive.reports)[-1], REPORTS_DIRECTORY+"/latest.json")
        self.assertEqual(json.loads(drive.reports[REPORTS_DIRECTORY+"/latest.json"])["date"], DAY)
        self.assertIn("멤버십 영상", drive.reports[REPORTS_DIRECTORY+"/"+DAY+".md"].decode("utf-8"))
        with patch("yt_archive.reports.epoch", return_value=NOW):
            write_report(self.db, self.config, "2026-10-24", drive=drive)
        self.assertEqual(json.loads(drive.reports[REPORTS_DIRECTORY+"/latest.json"])["date"], DAY)

    def test_publication_failure_keeps_local_report_and_video_history(self):
        drive = self.completed()
        drive.publish_failure = True
        with patch("yt_archive.reports.epoch", return_value=NOW):
            result = write_report(self.db, self.config, DAY, drive=drive)
        self.assertEqual(result["drive"], "failed")
        self.assertTrue((self.config.state_dir / "reports" / (DAY+".md")).is_file())
        self.assertEqual(self.db.get(SOURCE)["state"], "complete")

    def test_publisher_cannot_overwrite_a_video_or_escape_reports(self):
        drive = Drive(self.config)
        drive.run = Mock(side_effect=AssertionError("should not execute rclone"))
        for target in ("media.mkv", "reports/2026-10-25.json", "관리용 파일/보고서/2026-10-25.json", REPORTS_DIRECTORY+"/../media.mkv",
                       REPORTS_DIRECTORY+"/x.json", REPORTS_DIRECTORY+"/2026-10-25.json\n"):
            with self.subTest(target=target), self.assertRaises(ArchiveError):
                drive.publish_report(self.root / "anything", target)

    def test_report_publication_targets_only_the_management_report_folder(self):
        path = self.root / (DAY+".md")
        path.write_text("보고서", encoding="utf-8")
        drive = Drive(self.config)
        drive.run = Mock(return_value="")
        drive.verify = Mock()
        relative = REPORTS_DIRECTORY+"/"+path.name
        drive.publish_report(path, relative)
        target = self.config.drive_remote+"/관리용 파일/로그/"+path.name
        drive.run.assert_called_once_with(["copyto", str(path), target, "--ignore-times"])
        self.assertEqual(drive.verify.call_args.args[0], target)

    def test_invalid_future_dates_are_rejected(self):
        for value in ("2026-10-27", "2026-02-30", "../../secrets", "2026-1-1"):
            with self.subTest(value=value), self.assertRaises(ArchiveError):
                parse_day(value, NOW)

    def test_daily_report_config_is_type_checked(self):
        from yt_archive.config import load_config
        self.config_path.write_text('[reports]\nenabled="true"\n', encoding="utf-8")
        with self.assertRaises(ArchiveError):
            load_config(self.config_path)
