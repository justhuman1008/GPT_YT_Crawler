from dataclasses import replace
from datetime import datetime
import contextlib
import io
import json
from unittest.mock import Mock, patch

from test_archive import Fixture
from test_reports import ReportDrive
from yt_archive.cli import main
from yt_archive.common import ArchiveError, LockBusy, run_lock
from yt_archive.reports import KST


class StartupErrorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, cookies_from_drive=True)
        self.drive = ReportDrive()
        self.failure = ArchiveError("쿠키 인증 확인 실패")

    def invoke(self, command="run", **patches):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("yt_archive.cli.load_config", return_value=self.config))
            stack.enter_context(patch("yt_archive.cli.Drive", return_value=self.drive))
            stack.enter_context(patch("yt_archive.cli.sync_drive_cookies", side_effect=self.failure))
            downloader = stack.enter_context(patch("yt_archive.cli.Downloader"))
            for target, replacement in patches.items():
                stack.enter_context(patch(target, replacement))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = main(["--config", str(self.config_path), command])
        downloader.assert_not_called()
        return code, output.getvalue(), errors.getvalue()

    def error_records(self):
        return [json.loads(line) for line in (self.config.state_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()]

    def today_publication(self):
        day = datetime.now(KST).date().isoformat()
        return json.loads((self.config.state_dir / "reports" / (day + ".publication.json")).read_text(encoding="utf-8"))

    def test_cookie_startup_failure_is_published_without_starting_downloads(self):
        code, output, errors = self.invoke()
        self.assertEqual((code, output), (1, ""))
        self.assertIn("쿠키 인증 확인 실패", errors)
        text = self.drive.reports["관리용 파일/로그/latest.md"].decode("utf-8")
        self.assertIn("쿠키 파일: 실패", text)
        self.assertIn("쿠키 인증 확인 실패", text)
        self.assertIn("결과: failed", text)
        self.assertEqual(self.today_publication()["drive"], "verified")
        record = self.error_records()[0]
        self.assertEqual(record["command"], "run")
        self.assertEqual(datetime.fromisoformat(record["at"]).utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(record["error"], "쿠키 인증 확인 실패")

    def test_local_reports_exist_and_collection_lock_is_held_before_remote_write(self):
        publish = self.drive.publish_report
        def guarded(path, target):
            self.assertTrue((self.config.state_dir / "reports" / "latest.md").is_file())
            with self.assertRaises(LockBusy):
                with run_lock(self.config.state_dir):
                    pass
            return publish(path, target)
        self.drive.publish_report = guarded
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.today_publication()["drive"], "verified")

    def test_upload_token_failure_is_published_and_cookie_sync_is_not_started(self):
        self.config = replace(self.config, youtube_token_from_drive=True)
        cookie_sync = Mock(side_effect=AssertionError("must stop before cookie sync"))
        code, _, _ = self.invoke(**{
            "yt_archive.cli.sync_drive_youtube_token": Mock(side_effect=ArchiveError("업로드 토큰 갱신 실패")),
            "yt_archive.cli.sync_drive_cookies": cookie_sync,
        })
        self.assertEqual(code, 1)
        cookie_sync.assert_not_called()
        text = self.drive.reports["관리용 파일/로그/latest.md"].decode("utf-8")
        self.assertIn("YouTube 업로드 토큰: 실패", text)
        self.assertIn("업로드 토큰 갱신 실패", text)

    def test_sheet_failure_is_published_even_before_credential_sync(self):
        self.config = replace(self.config, control_sheet_id="test-sheet")
        reader = Mock()
        reader.return_value.read.side_effect = ArchiveError("시트 설정 읽기 실패")
        code, _, _ = self.invoke("scheduled-run", **{"yt_archive.cli.GoogleSheets": reader})
        self.assertEqual(code, 1)
        text = self.drive.reports["관리용 파일/로그/latest.md"].decode("utf-8")
        self.assertIn("시트 설정 읽기 실패", text)
        self.assertEqual(self.error_records()[0]["command"], "scheduled-run")

    def test_drive_auth_failure_preserves_local_reports_and_original_failure(self):
        self.drive.validate_backend = Mock(side_effect=RuntimeError("SECRET_ACCESS_TOKEN"))
        code, _, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("로컬 기록은 보존", errors)
        self.assertEqual(self.drive.reports, {})
        self.assertEqual(self.today_publication()["drive"], "failed")
        self.assertEqual(self.db.connection.execute("SELECT status FROM runs").fetchone()[0], "failed")
        artifacts = errors + json.dumps(self.error_records()) + (self.config.state_dir / "reports/latest.md").read_text(encoding="utf-8")
        self.assertNotIn("SECRET_", artifacts)

    def test_remote_publication_failure_does_not_replace_the_cookie_error(self):
        self.drive.publish_failure = True
        code, _, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("쿠키 인증 확인 실패", errors)
        self.assertEqual(self.today_publication()["drive"], "failed")
        self.assertEqual(self.error_records()[0]["error"], "쿠키 인증 확인 실패")

    def test_reports_disabled_still_preserves_separate_error_file(self):
        self.config = replace(self.config, reports_enabled=False)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.drive.reports, {})
        self.assertEqual(len(self.error_records()), 1)

    def test_drive_reports_disabled_remains_local_only(self):
        self.config = replace(self.config, reports_drive=False)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.drive.reports, {})
        self.assertTrue((self.config.state_dir / "reports/latest.md").is_file())

    def test_bad_config_still_records_a_safe_error_without_database(self):
        self.config_path.write_text("[broken SECRET_CONFIG", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--config", str(self.config_path), "scheduled-run"])
        self.assertEqual(code, 1)
        self.assertEqual(self.error_records()[0]["command"], "scheduled-run")
        self.assertNotIn("SECRET_", errors.getvalue() + json.dumps(self.error_records()))

    def test_database_open_failure_uses_error_file(self):
        self.assertEqual(self.invoke(**{"yt_archive.cli.Database": Mock(side_effect=OSError("SECRET_DB"))})[0], 1)
        self.assertNotIn("SECRET_", json.dumps(self.error_records()))
        self.assertEqual(self.drive.reports, {})

    def test_database_finish_failure_does_not_mask_original_error(self):
        code, _, errors = self.invoke(**{"yt_archive.db.Database.finish_run": Mock(side_effect=OSError("SECRET_DB"))})
        self.assertEqual(code, 1)
        self.assertIn("쿠키 인증 확인 실패", errors)
        self.assertIn("DB 저장에도 실패", errors)
        self.assertNotIn("SECRET_", errors)
        self.assertEqual(self.error_records()[0]["error"], "쿠키 인증 확인 실패")

    def test_separate_error_file_failure_does_not_block_report_publication(self):
        (self.config.state_dir / "errors.jsonl").mkdir()
        code, _, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("별도 오류 파일 저장에도 실패", errors)
        self.assertIn("쿠키 인증 확인 실패", errors)
        self.assertEqual(self.today_publication()["drive"], "verified")

    def test_repeated_failures_append_records_instead_of_overwriting(self):
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(len(self.error_records()), 2)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM runs WHERE status='failed'").fetchone()[0], 2)

    def test_busy_scheduled_worker_does_not_create_an_error_record(self):
        with run_lock(self.config.state_dir):
            code, output, errors = self.invoke("scheduled-run")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["schedule"]["status"], "busy")
        self.assertFalse((self.config.state_dir / "errors.jsonl").exists())
