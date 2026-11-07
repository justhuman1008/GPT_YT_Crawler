from dataclasses import replace
from datetime import datetime, timedelta
import contextlib
import io
import json
from unittest.mock import Mock, patch

from test_archive import Fixture, SOURCE, SECOND
from yt_archive.cli import main
from yt_archive.common import ArchiveError, run_lock
from yt_archive.config import load_config
from yt_archive.sheet_control import DAILY, INTERVAL, GoogleSheets, Schedule, SheetControls, parse_controls
from yt_archive.scheduling import KST, schedule_tick


def settings(enabled=False, mode=INTERVAL, time="03:00", interval="30"):
    return [["자동 실행", enabled], ["실행 방식", mode], ["실행 시각", time], ["반복 간격(분)", interval]]


class SheetParsingTests(Fixture):
    def test_existing_placeholder_rows_are_ignored_and_checkboxes_control_channels(self):
        controls = parse_controls(settings(), [["채널 A 링크를 입력하세요", "FALSE", "FALSE"],
                                               ["https://youtube.com/@enabled", "TRUE", "FALSE"],
                                               ["https://www.youtube.com/@paused", False, True]])
        self.assertFalse(controls.schedule.enabled)
        self.assertEqual(len(controls.channels), 2)
        self.assertEqual(controls.channels[0].url, "https://www.youtube.com/@enabled")
        self.assertTrue(controls.channels[0].download)
        self.assertFalse(controls.channels[0].youtube)
        self.assertFalse(controls.channels[1].download)
        self.assertTrue(controls.channels[1].youtube)

    def test_invalid_selected_link_and_duplicate_channel_fail_without_echoing_input(self):
        for rows in ([["SECRET_URL", True, False]], [["https://youtube.com/@same", True], ["https://youtube.com/@same", True]]):
            with self.assertRaises(ArchiveError) as caught:
                parse_controls(settings(), rows)
            self.assertNotIn("SECRET_URL", str(caught.exception))

    def test_only_active_mode_inputs_are_required(self):
        self.assertEqual(parse_controls(settings(mode=DAILY, time="23:59", interval="unused"), []).schedule.hour, 23)
        self.assertEqual(parse_controls(settings(time="unused", interval="120"), []).schedule.interval_minutes, 120)

    def test_bad_controls_fail_closed(self):
        inputs = [settings(enabled="yes"), settings(mode="SECRET_COMMAND"), settings(mode=DAILY, time="24:00"),
                  settings(mode=DAILY, time="03:60"), settings(interval="0"), settings(interval="10081"),
                  settings(interval="1.5"), settings()+[["自动 실행", False]], settings()[:-1]]
        for rows in inputs:
            with self.subTest(rows=rows), self.assertRaises(ArchiveError):
                parse_controls(rows, [])

    def test_paused_or_removed_channels_do_not_process_queued_jobs(self):
        controls = parse_controls(settings(), [["https://youtube.com/@on", True, False],
                                               ["https://youtube.com/@off", False, False]])
        config = replace(self.config, control_sheet_id="sheet-example", channels=controls.channels, max_jobs=1)
        self.db.add(SOURCE, "https://youtube.com/watch?v="+SOURCE, source="https://www.youtube.com/@off")
        self.db.add(SECOND, "https://youtube.com/watch?v="+SECOND, source="https://www.youtube.com/@on")
        self.assertEqual([j["video_id"] for j in self.db.jobs(config)], [SECOND])
        self.assertEqual(self.db.jobs(replace(config, channels=())), [])

    def test_manual_jobs_remain_explicitly_available(self):
        self.add()
        config = replace(self.config, control_sheet_id="sheet-example")
        self.assertEqual(len(self.db.jobs(config)), 1)

    def test_sheet_id_must_be_an_id_and_not_a_url_or_path(self):
        self.config_path.write_text('[control]\nspreadsheet_id="https://example.test/SECRET"', encoding="utf-8")
        with self.assertRaises(ArchiveError):
            load_config(self.config_path)

    def test_batch_response_order_and_identity_are_checked(self):
        config = replace(self.config, control_sheet_id="sheet-example")
        reader = GoogleSheets(config)
        response = Mock(status_code=200)
        response.json.return_value = {"spreadsheetId": "sheet-example", "valueRanges": [{"values": settings()}, {}]}
        session = Mock()
        session.get.return_value = response
        with patch.object(reader, "session") as factory:
            factory.return_value.__enter__.return_value = session
            self.assertFalse(reader.read().schedule.enabled)
            response.json.return_value["spreadsheetId"] = "OTHER"
            with self.assertRaises(ArchiveError):
                reader.read()
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_http_and_parser_failures_are_safe(self):
        reader = GoogleSheets(replace(self.config, control_sheet_id="sheet-example"))
        session = Mock()
        session.get.side_effect = RuntimeError("SECRET_AUTHORIZATION")
        with patch.object(reader, "session") as factory:
            factory.return_value.__enter__.return_value = session
            with self.assertRaises(ArchiveError) as caught:
                reader.read()
        self.assertNotIn("SECRET_", str(caught.exception))


class SchedulingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.path = self.config.state_dir / "schedule-state.json"
        self.now = datetime(2026, 11, 6, 2, 0, tzinfo=KST).timestamp()

    def tick(self, value, now=None):
        return schedule_tick(value, self.path, self.now if now is None else now)

    def test_disabled_does_not_reserve_a_run(self):
        result = self.tick(Schedule(False, INTERVAL))
        self.assertEqual(result["status"], "disabled")
        self.assertIsNone(result["last_started_at"])

    def test_interval_starts_once_then_respects_interval_even_after_restart(self):
        value = Schedule(True, INTERVAL, interval_minutes=30)
        self.assertEqual(self.tick(value)["status"], "due")
        self.assertEqual(self.tick(value, self.now + 60)["status"], "waiting")
        self.assertEqual(self.tick(value, self.now + 1800)["status"], "due")
        self.assertEqual(self.tick(value, self.now + 1801)["status"], "waiting")

    def test_daily_waits_for_korean_time_and_runs_only_once(self):
        value = Schedule(True, DAILY, hour=3)
        self.assertEqual(self.tick(value)["status"], "waiting")
        self.assertEqual(self.tick(value, self.now + 3600)["status"], "due")
        self.assertEqual(self.tick(value, self.now + 3660)["status"], "waiting")
        self.assertEqual(self.tick(value, self.now + 86400 + 3600)["status"], "due")

    def test_daily_enabled_after_time_waits_for_tomorrow(self):
        result = self.tick(Schedule(True, DAILY, hour=1))
        self.assertEqual(result["status"], "waiting")
        self.assertTrue(result["next_run"].startswith("2026-11-07T01:00"))

    def test_enabling_during_scheduled_minute_is_due(self):
        self.assertEqual(self.tick(Schedule(True, DAILY, hour=2), self.now + 15)["status"], "due")

    def test_downtime_catches_up_once_without_replaying_every_missed_day(self):
        value = Schedule(True, DAILY, hour=3)
        self.tick(value)
        later = self.now + 4 * 86400 + 7200
        self.assertEqual(self.tick(value, later)["status"], "due")
        self.assertEqual(self.tick(value, later + 60)["status"], "waiting")

    def test_disabled_or_changed_mode_replaces_old_schedule(self):
        self.tick(Schedule(True, INTERVAL))
        self.assertEqual(self.tick(Schedule(False, INTERVAL))["status"], "disabled")
        self.assertEqual(self.tick(Schedule(True, INTERVAL), self.now + 120)["status"], "due")
        self.assertEqual(self.tick(Schedule(True, DAILY, hour=4), self.now + 180)["status"], "waiting")

    def test_bad_state_cannot_restart_a_duplicate_job(self):
        for contents in ('broken', '{}', '{"schema_version":1}', '{"schema_version":1,"settings":{},"status":"due"}'):
            self.path.write_text(contents, encoding="utf-8")
            with self.assertRaises(ArchiveError):
                self.tick(Schedule(True, INTERVAL))


class ScheduledCliTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, control_sheet_id="sheet-example", reports_enabled=False)
        self.controls = SheetControls(Schedule(False, INTERVAL), ())

    def invoke(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("yt_archive.cli.load_config", return_value=self.config), \
             patch("yt_archive.cli.GoogleSheets") as reader, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            reader.return_value.read.return_value = self.controls
            code = main(["scheduled-run"])
        return code, output.getvalue(), errors.getvalue()

    def test_disabled_never_downloads_credentials_or_videos_and_adds_no_run_count(self):
        with patch("yt_archive.cli.Drive") as drive, patch("yt_archive.cli.Downloader") as downloader:
            code, output, error = self.invoke()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["schedule"]["status"], "disabled")
        drive.assert_not_called()
        downloader.assert_not_called()
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_due_tick_runs_once_and_following_tick_does_no_work(self):
        self.controls = SheetControls(Schedule(True, INTERVAL), ())
        downloader, runner = Mock(), Mock()
        downloader.discover.return_value = []
        runner.run.return_value = {}
        with patch("yt_archive.cli.Downloader", return_value=downloader), patch("yt_archive.cli.Drive"), \
             patch("yt_archive.cli.Runner", return_value=runner) as factory:
            self.assertEqual(self.invoke()[0], 0)
            self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(runner.run.call_count, 1)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertFalse(factory.call_args[0][0].youtube_enabled)

    def test_busy_collection_is_skipped_without_starting_another(self):
        with run_lock(self.config.state_dir):
            code, output, error = self.invoke()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["schedule"]["status"], "busy")

    def test_failed_sheet_read_is_recorded_without_upstream_secrets(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("yt_archive.cli.load_config", return_value=self.config), patch("yt_archive.cli.GoogleSheets") as reader, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            reader.return_value.read.side_effect = ArchiveError("Google 시트 읽기 실패")
            code = main(["scheduled-run"])
        self.assertEqual(code, 1)
        row = self.db.connection.execute("SELECT status FROM runs").fetchone()
        self.assertEqual(row["status"], "failed")
