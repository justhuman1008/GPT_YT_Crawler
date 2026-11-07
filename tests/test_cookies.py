from dataclasses import replace
from datetime import datetime
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from test_archive import Fixture, SOURCE
from yt_archive.cli import doctor, main
from yt_archive.common import ArchiveError
from yt_archive.config import load_config
from yt_archive.cookies import MAX_COOKIE_BYTES, sync_drive_cookies, validated_cookie_text
from yt_archive.drive import Drive
from yt_archive.reports import KST, build_report, render_markdown


def cookie_bytes(value="SECRET_COOKIE", *, expires="4102444800", domain=".youtube.com"):
    return ("# Netscape HTTP Cookie File\n" + domain + "\tTRUE\t/\tTRUE\t" + expires +
            "\tSID\t" + value + "\n").encode("utf-8")


class CookieDrive:
    def __init__(self, contents=None):
        self.contents = cookie_bytes() if contents is None else contents
        self.file_id = "cookie-file-id"
        self.modified = "2026-11-05T00:00:00+00:00"
        self.copies = 0
        self.reads = 0
        self.error = None
        self.download_error = None
        self.received = None

    def validate_backend(self):
        pass

    def ensure_capacity(self, estimated=0):
        pass

    def cookie_snapshot(self):
        self.reads += 1
        if self.error:
            raise self.error
        return {"id": self.file_id, "size": len(self.contents), "modified": self.modified,
                "md5": hashlib.md5(self.contents, usedforsecurity=False).hexdigest()}

    def download_cookies(self, destination):
        self.copies += 1
        destination.write_bytes(self.contents if self.received is None else self.received)
        if self.download_error:
            raise self.download_error


class CookieTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, cookies_from_drive=True, reports_drive=False)
        self.drive = CookieDrive()
        self.cookie = self.config.cookies_file
        self.marker = self.cookie.with_name(self.cookie.name + ".drive-state.json")

    def sync(self):
        return sync_drive_cookies(self.config, self.drive)

    def installed(self):
        self.assertEqual(self.sync(), {"status": "updated"})
        return self.cookie.read_bytes(), self.marker.read_bytes()

    def assert_preserved(self, before):
        self.assertEqual(self.cookie.read_bytes(), before[0])
        self.assertEqual(self.marker.read_bytes(), before[1])
        self.assertEqual(list(self.cookie.parent.glob(".cookie-*")), [])

    def test_first_install_and_unchanged_file_never_overwrites_refreshed_local_jar(self):
        self.installed()
        refreshed = cookie_bytes("LOCALLY_REFRESHED")
        self.cookie.write_bytes(refreshed)
        self.assertEqual(self.sync(), {"status": "unchanged"})
        self.assertEqual(self.cookie.read_bytes(), refreshed)
        self.assertEqual(self.drive.copies, 1)
        self.assertEqual(self.drive.reads, 2)  # Exactly one metadata check per startup.
        self.assertNotIn("SECRET_COOKIE", self.marker.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(self.cookie.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)

    def test_remote_content_id_timestamp_or_location_change_triggers_replacement(self):
        self.installed()
        for attribute, value in (("contents", cookie_bytes("REMOTE_NEW")), ("file_id", "replacement-id"),
                                 ("modified", "2026-11-05T01:00:00+00:00")):
            with self.subTest(attribute=attribute):
                setattr(self.drive, attribute, value)
                self.assertEqual(self.sync()["status"], "updated")
        self.config = replace(self.config, drive_remote="gdrive:새 보관 폴더")
        self.assertEqual(self.sync()["status"], "updated")
        self.assertEqual(self.drive.copies, 5)

    def test_missing_or_empty_local_file_and_lost_or_invalid_marker_are_restored(self):
        self.installed()
        actions = [lambda: self.cookie.unlink(), lambda: self.cookie.write_bytes(b""),
                   lambda: self.marker.unlink(), lambda: self.marker.write_bytes(b"not-json"),
                   lambda: self.marker.write_bytes(b"\xff")]
        for action in actions:
            action()
            self.assertEqual(self.sync()["status"], "updated")
            self.assertEqual(self.cookie.read_bytes(), self.drive.contents)

    def test_disabled_does_not_read_drive_or_create_cookie_directory(self):
        self.config = replace(self.config, cookies_from_drive=False)
        self.assertEqual(sync_drive_cookies(self.config, None), {"status": "disabled"})
        self.assertFalse(self.cookie.parent.exists())

    def test_metadata_failure_preserves_existing_files_without_download(self):
        before = self.installed()
        self.drive.error = RuntimeError("SECRET_UPSTREAM_TOKEN")
        with self.assertRaises(ArchiveError) as caught:
            self.sync()
        self.assertNotIn("SECRET_", str(caught.exception))
        self.assertEqual(self.drive.copies, 1)
        self.assert_preserved(before)

    def test_partial_download_failure_preserves_existing_files(self):
        before = self.installed()
        self.drive.contents = cookie_bytes("NEW_SECRET")
        self.drive.received = b"partial SECRET_CONTENT"
        self.drive.download_error = OSError("SECRET_DOWNLOAD_URL")
        with self.assertRaises(ArchiveError) as caught:
            self.sync()
        self.assertNotIn("SECRET_", str(caught.exception))
        self.assert_preserved(before)

    def test_incomplete_or_changed_content_is_never_promoted(self):
        before = self.installed()
        self.drive.contents = cookie_bytes("REMOTE_NEW")
        for received in (b"truncated", self.drive.contents.replace(b"REMOTE_NEW", b"OTHER_DATA")):
            with self.subTest(size=len(received)):
                self.drive.received = received
                with self.assertRaises(ArchiveError):
                    self.sync()
                self.assert_preserved(before)

    def test_invalid_or_expired_source_does_not_replace_cookie_or_marker(self):
        before = self.installed()
        invalid = [b"", b"SECRET_BAD_FILE", b"# Netscape HTTP Cookie File\n",
                   cookie_bytes(expires="1"), cookie_bytes(domain=".google.com"),
                   cookie_bytes() + b"SECRET_INVALID_LINE\n", b"\xff",
                   b"# Netscape HTTP Cookie File\n" + b"#" * MAX_COOKIE_BYTES]
        for contents in invalid:
            with self.subTest(size=len(contents)):
                self.drive.contents = contents
                with self.assertRaises(ArchiveError) as caught:
                    self.sync()
                self.assertNotIn("SECRET_", str(caught.exception))
                self.assert_preserved(before)

    def test_utf8_bom_windows_newlines_and_httponly_session_cookie_are_supported(self):
        normalized = cookie_bytes(expires="0").replace(b".youtube.com", b"#HttpOnly_.youtube.com")
        self.drive.contents = b"\xef\xbb\xbf" + normalized.replace(b"\n", b"\r\n")
        self.installed()
        self.assertEqual(self.cookie.read_bytes(), normalized)
        self.assertEqual(self.sync()["status"], "unchanged")
        self.assertEqual(self.drive.copies, 1)

    def test_google_subdomains_not_matching_youtube_are_rejected_without_secret_diagnostics(self):
        for domain in (".youtube.com.attacker.test", ".notyoutube.com", ".google.com"):
            with self.subTest(domain=domain), self.assertRaises(ArchiveError) as caught:
                validated_cookie_text(cookie_bytes(domain=domain))
            self.assertNotIn("SECRET_COOKIE", str(caught.exception))

    def test_marker_commit_failure_rolls_back_cookie_and_retains_old_marker(self):
        before = self.installed()
        self.drive.contents = cookie_bytes("NEW_SECRET")
        real_replace = os.replace

        def fail_marker(source, destination):
            if Path(destination) == self.marker:
                raise PermissionError("SECRET_PATH")
            return real_replace(source, destination)

        with patch("yt_archive.cookies.os.replace", side_effect=fail_marker):
            with self.assertRaises(ArchiveError):
                self.sync()
        self.assert_preserved(before)

    def test_first_install_marker_failure_leaves_no_cookie_or_marker(self):
        real_replace = os.replace

        def fail_marker(source, destination):
            if Path(destination) == self.marker:
                raise OSError("marker write failed")
            return real_replace(source, destination)

        with patch("yt_archive.cookies.os.replace", side_effect=fail_marker):
            with self.assertRaises(ArchiveError):
                self.sync()
        self.assertFalse(self.cookie.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(list(self.cookie.parent.glob(".cookie-*")), [])

    def test_rollback_failure_retains_private_recovery_copy(self):
        before = self.installed()
        self.drive.contents = cookie_bytes("NEW_SECRET")
        real_replace = os.replace
        cookie_replaces = 0

        def fail_marker_and_rollback(source, destination):
            nonlocal cookie_replaces
            if Path(destination) == self.marker:
                raise OSError("marker failed")
            if Path(destination) == self.cookie:
                cookie_replaces += 1
                if cookie_replaces > 1:
                    raise OSError("restore failed")
            return real_replace(source, destination)

        with patch("yt_archive.cookies.os.replace", side_effect=fail_marker_and_rollback):
            with self.assertRaisesRegex(ArchiveError, "복구에 실패"):
                self.sync()
        retained = list(self.cookie.parent.glob(".cookie-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), before[0])
        self.assertEqual(self.marker.read_bytes(), before[1])

    def test_directory_in_place_of_cookie_is_rejected(self):
        self.cookie.mkdir(parents=True)
        with self.assertRaises(ArchiveError):
            self.sync()
        self.assertEqual(self.drive.reads, 0)

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch("yt_archive.cli.load_config", return_value=self.config), \
             patch("yt_archive.cli.Drive", return_value=self.drive), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--config", str(self.config_path), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_run_syncs_before_discovery_and_never_during_video_processing(self):
        downloader, runner = Mock(), Mock()

        def discover(database):
            self.assertEqual(self.cookie.read_bytes(), self.drive.contents)
            return []

        def process():
            # Simulates the working jar being refreshed while several videos are processed.
            self.cookie.write_bytes(cookie_bytes("LOCAL_SESSION_REFRESH"))
            self.assertEqual(self.drive.reads, 1)
            return {}

        downloader.discover.side_effect = discover
        runner.run.side_effect = process
        with patch("yt_archive.cli.Downloader", return_value=downloader), patch("yt_archive.cli.Runner", return_value=runner):
            code, output, errors = self.invoke("run")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["cookies"], {"status": "updated"})
        self.assertEqual(self.drive.reads, 1)
        self.assertEqual(self.cookie.read_bytes(), cookie_bytes("LOCAL_SESSION_REFRESH"))
        row = self.db.connection.execute("SELECT summary FROM runs").fetchone()
        self.assertEqual(json.loads(row["summary"])["cookies"], {"status": "updated"})

    def test_no_discover_still_checks_cookies_once_before_jobs(self):
        downloader, runner = Mock(), Mock()
        runner.run.return_value = {}
        with patch("yt_archive.cli.Downloader", return_value=downloader), patch("yt_archive.cli.Runner", return_value=runner):
            code, _, errors = self.invoke("run", "--no-discover")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.drive.reads, 1)
        downloader.discover.assert_not_called()

    def test_discover_checks_at_startup_and_records_cookie_status(self):
        downloader = Mock()
        downloader.discover.return_value = []
        with patch("yt_archive.cli.Downloader", return_value=downloader), patch("yt_archive.cli.Runner") as runner:
            code, output, errors = self.invoke("discover")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.drive.reads, 1)
        self.assertEqual(json.loads(output)["cookies"]["status"], "updated")
        runner.assert_not_called()
        row = self.db.connection.execute("SELECT status, summary FROM runs").fetchone()
        self.assertEqual(row["status"], "success")
        self.assertEqual(json.loads(row["summary"])["command"], "discover")

    def test_startup_failure_stops_youtube_access_and_writes_safe_local_failure_report(self):
        before = self.installed()
        self.drive.error = RuntimeError("SECRET_ACCESS_TOKEN")
        with patch("yt_archive.cli.Downloader") as downloader, patch("yt_archive.cli.Runner") as runner:
            code, output, errors = self.invoke("run")
        self.assertEqual(code, 1)
        downloader.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(output, "")
        self.assertNotIn("SECRET_", errors)
        self.assert_preserved(before)
        row = self.db.connection.execute("SELECT status, summary FROM runs").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(json.loads(row["summary"])["cookies"], {"status": "failed"})
        report_text = (self.config.state_dir / "reports" / "latest.md").read_text(encoding="utf-8")
        self.assertIn("쿠키 파일: 실패", report_text)
        self.assertNotIn("SECRET_", report_text)

    def test_non_collection_commands_do_not_sync_cookies(self):
        for command in (("status",), ("add", SOURCE), ("retry", SOURCE),
                        ("report", "--local-only", "--date", "today")):
            code, _, errors = self.invoke(*command)
            self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.drive.reads, 0)
        self.assertEqual(self.drive.copies, 0)

    def test_doctor_only_reads_metadata_without_downloading_or_installing(self):
        with patch("yt_archive.cli.Drive", return_value=self.drive), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            doctor(self.config)
        checks = {item["check"]: item["ok"] for item in json.loads(output.getvalue())["checks"]}
        self.assertTrue(checks["Drive youtube-cookies.txt metadata"])
        self.assertEqual((self.drive.reads, self.drive.copies), (1, 0))
        self.assertFalse(self.cookie.exists())

    def test_reports_whitelist_status_and_never_publish_cookie_fingerprints(self):
        start = datetime(2026, 11, 5, 1, tzinfo=KST).timestamp()
        with patch("yt_archive.db.epoch", return_value=start):
            run = self.db.start_run()
        with patch("yt_archive.db.epoch", return_value=start + 5):
            self.db.finish_run(run, "success", {"cookies": {"status": "updated", "md5": "SECRET_HASH", "value": "SECRET_COOKIE"}})
        report = build_report(self.db, self.config, "2026-11-05", now=start + 10)
        rendered = render_markdown(report)
        self.assertEqual(report["runs"][0]["cookies_status"], "updated")
        self.assertIn("쿠키 파일: 교체 완료", rendered)
        self.assertNotIn("SECRET_", json.dumps(report) + rendered)

    def test_config_defaults_off_but_accepts_explicit_boolean_only(self):
        self.assertFalse(load_config(self.config_path).cookies_from_drive)
        for value, expected in (("true", True), ("false", False)):
            self.config_path.write_text("[download]\ncookies_from_drive = " + value, encoding="utf-8")
            self.assertIs(load_config(self.config_path).cookies_from_drive, expected)
        self.config_path.write_text('[download]\ncookies_from_drive = "true"', encoding="utf-8")
        with self.assertRaises(ArchiveError):
            load_config(self.config_path)


class DriveCookieMetadataTests(Fixture):
    def setUp(self):
        super().setUp()
        self.drive = Drive(self.config)
        self.entry = {"Name": "youtube-cookies.txt", "Path": "youtube-cookies.txt", "ID": "file-id",
                      "IsDir": False, "Size": 120, "ModTime": "2026-11-05T00:00:00Z",
                      "Hashes": {"MD5": "A" * 32}, "MimeType": "text/plain"}

    def snapshot(self, entries):
        with patch.object(self.drive, "run", return_value=json.dumps(entries)):
            return self.drive.cookie_snapshot()

    def test_metadata_check_is_nonrecursive_read_only_and_does_not_fetch_contents(self):
        with patch.object(self.drive, "run", return_value=json.dumps([self.entry])) as run:
            snapshot = self.drive.cookie_snapshot()
        self.assertEqual(snapshot, {"id": "file-id", "size": 120, "modified": "2026-11-05T00:00:00+00:00", "md5": "a" * 32})
        run.assert_called_once_with(["lsjson", self.config.drive_remote + "/관리용 파일", "--files-only", "--hash"],
                                    missing=True, timeout=180)

    def test_missing_or_duplicate_same_name_file_is_an_error(self):
        for entries in ([], [self.entry, self.entry]):
            with self.subTest(count=len(entries)), self.assertRaises(ArchiveError):
                self.snapshot(entries)
        with patch.object(self.drive, "run", return_value=None), self.assertRaises(ArchiveError):
            self.drive.cookie_snapshot()

    def test_bad_size_hash_id_date_or_native_google_document_is_rejected(self):
        invalid = [("Size", 0), ("Size", -1), ("Size", True), ("Size", MAX_COOKIE_BYTES + 1),
                   ("Hashes", {}), ("Hashes", {"MD5": "SECRET_BAD_HASH"}), ("ID", "invalid/id"),
                   ("ModTime", "not-a-date"), ("ModTime", "2026-11-05T00:00:00"), ("IsDir", True),
                   ("MimeType", "application/vnd.google-apps.document")]
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaises(ArchiveError) as caught:
                self.snapshot([{**self.entry, key: value}])
            self.assertNotIn("SECRET_", str(caught.exception))

    def test_invalid_metadata_json_is_a_safe_error(self):
        with patch.object(self.drive, "run", return_value="SECRET_BROKEN_JSON"):
            with self.assertRaises(ArchiveError) as caught:
                self.drive.cookie_snapshot()
        self.assertNotIn("SECRET_", str(caught.exception))

    def test_cookie_transfer_is_drive_to_local_only_with_size_limit(self):
        destination = self.root / "private-staging.txt"
        def receive(command, *, stdout, stderr, timeout):
            self.assertEqual(command[-4:], ["cat", self.config.drive_remote + "/관리용 파일/youtube-cookies.txt",
                                            "--head", "1048577"])
            stdout.write(cookie_bytes())
            return Mock(returncode=0)
        with patch("yt_archive.drive.subprocess.run", side_effect=receive) as run:
            self.drive.download_cookies(destination)
        self.assertEqual(destination.read_bytes(), cookie_bytes())
        self.assertEqual(run.call_count, 1)
