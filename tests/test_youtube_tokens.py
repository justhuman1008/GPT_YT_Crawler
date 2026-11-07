from dataclasses import replace
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from test_archive import CHANNEL, Fixture
from yt_archive.cli import doctor, main
from yt_archive.common import ArchiveError
from yt_archive.config import load_config
from yt_archive.drive import Drive
from yt_archive.reports import build_report, render_markdown
from yt_archive.youtube import API, SCOPES
from yt_archive.youtube_tokens import sync_drive_youtube_token, token_info, verify_youtube_token


def token_bytes(**changes):
    return json.dumps({"client_id": "123-example.apps.googleusercontent.com", "client_secret": "SECRET_CLIENT",
                       "refresh_token": "SECRET_REFRESH", "token": "SECRET_ACCESS", "scopes": SCOPES,
                       "token_uri": "https://oauth2.googleapis.com/token", **changes}).encode("utf-8")


class TokenDrive:
    def __init__(self):
        self.contents = token_bytes()
        self.received = None
        self.reads = self.copies = 0
        self.error = None
        self.changed_during_download = False

    def validate_backend(self):
        pass

    def ensure_capacity(self, estimated=0):
        pass

    def youtube_token_snapshot(self):
        self.reads += 1
        if self.error:
            raise self.error
        return {"id": "token-id", "size": len(self.contents), "modified": "2026-11-06T00:00:00+00:00",
                "md5": hashlib.md5(self.contents, usedforsecurity=False).hexdigest()}

    def download_youtube_token(self, path):
        self.copies += 1
        path.write_bytes(self.contents if self.received is None else self.received)
        if self.changed_during_download:
            self.contents = token_bytes(token="REMOTE_CHANGED_DURING_DOWNLOAD")


class TokenSyncTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, youtube_token_from_drive=True, youtube_channel=CHANNEL, reports_drive=False)
        self.drive = TokenDrive()
        self.token = self.config.youtube_token
        self.marker = self.token.with_name(self.token.name + ".drive-state.json")
        self.verifier = self.enterContext(patch("yt_archive.youtube_tokens.verify_youtube_token",
                                               side_effect=lambda info, channel: info))

    def sync(self):
        return sync_drive_youtube_token(self.config, self.drive)

    def installed(self):
        self.assertEqual(self.sync(), {"status": "updated"})
        return self.token.read_bytes(), self.marker.read_bytes()

    def assert_preserved(self, before):
        self.assertEqual((self.token.read_bytes(), self.marker.read_bytes()), before)
        self.assertEqual(list(self.token.parent.glob(".youtube-token-*")), [])

    def test_first_import_works_with_uploads_off_and_unchanged_drive_preserves_local_refresh(self):
        self.installed()
        refreshed = token_bytes(token="LOCALLY_REFRESHED_ACCESS")
        self.token.write_bytes(refreshed)
        self.assertEqual(self.sync(), {"status": "unchanged"})
        self.assertEqual(self.token.read_bytes(), refreshed)
        self.assertEqual((self.drive.copies, self.drive.reads, self.verifier.call_count), (1, 3, 1))
        self.assertFalse(self.config.youtube_enabled)
        self.assertNotIn("SECRET_", self.marker.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)

    def test_changed_drive_token_replaces_working_copy(self):
        self.installed()
        self.drive.contents = token_bytes(refresh_token="REPLACEMENT_REFRESH")
        self.assertEqual(self.sync(), {"status": "updated"})
        self.assertEqual(json.loads(self.token.read_bytes())["refresh_token"], "REPLACEMENT_REFRESH")

    def test_missing_or_invalid_local_token_or_marker_is_recovered(self):
        self.installed()
        for action in (lambda: self.token.unlink(), lambda: self.token.write_bytes(b"bad"),
                       lambda: self.marker.unlink(), lambda: self.marker.write_bytes(b"\xff")):
            action()
            self.assertEqual(self.sync(), {"status": "updated"})

    def test_disabled_does_not_access_drive(self):
        self.config = replace(self.config, youtube_token_from_drive=False)
        self.assertEqual(self.sync(), {"status": "disabled"})
        self.assertEqual(self.drive.reads, 0)
        self.assertFalse(self.token.exists())

    def test_truncated_or_mismatched_download_preserves_existing_token(self):
        before = self.installed()
        self.drive.contents = token_bytes(token="CHANGED_ACCESS")
        for received in (b"partial SECRET_", self.drive.contents.replace(b"CHANGED_ACCESS", b"MISMATCH_TOKEN")):
            self.drive.received = received
            with self.assertRaises(ArchiveError) as caught:
                self.sync()
            self.assertNotIn("SECRET_", str(caught.exception))
            self.assert_preserved(before)

    def test_wrong_endpoint_scopes_or_refresh_token_never_reach_network_verification(self):
        before = self.installed()
        self.verifier.reset_mock()
        for changes in ({"token_uri": "https://attacker.test/token"}, {"refresh_token": ""},
                        {"scopes": [SCOPES[0]]}, {"scopes": SCOPES + ["https://example.test"]},
                        {"universe_domain": "attacker.test"}, {"client_id": "invalid"}):
            self.drive.contents = token_bytes(**changes)
            with self.assertRaises(ArchiveError):
                self.sync()
            self.assert_preserved(before)
        self.verifier.assert_not_called()

    def test_change_during_download_preserves_existing_token(self):
        before = self.installed()
        self.drive.contents = token_bytes(token="CHANGED_ACCESS")
        self.drive.changed_during_download = True
        with self.assertRaises(ArchiveError):
            self.sync()
        self.assert_preserved(before)

    def test_channel_or_refresh_failure_preserves_existing_token_and_marker(self):
        before = self.installed()
        self.drive.contents = token_bytes(token="CHANGED_ACCESS")
        self.verifier.side_effect = ArchiveError("YouTube 토큰 갱신 또는 지정 채널 확인에 실패했습니다.")
        with self.assertRaises(ArchiveError):
            self.sync()
        self.assert_preserved(before)

    def test_marker_commit_failure_restores_previous_token(self):
        before = self.installed()
        self.drive.contents = token_bytes(token="CHANGED_ACCESS")
        real_replace = os.replace

        def replace_file(source, destination):
            if Path(destination) == self.marker:
                raise PermissionError("SECRET_PATH")
            return real_replace(source, destination)

        with patch("yt_archive.youtube_tokens.os.replace", side_effect=replace_file), self.assertRaises(ArchiveError):
            self.sync()
        self.assert_preserved(before)

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch("yt_archive.cli.load_config", return_value=self.config), \
             patch("yt_archive.cli.Drive", return_value=self.drive), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_token_only_command_does_not_download_videos_read_cookies_or_enable_uploads(self):
        self.config = replace(self.config, cookies_from_drive=True)
        with patch("yt_archive.cli.Downloader") as downloader, patch("yt_archive.cli.Runner") as runner, \
             patch("yt_archive.cli.sync_drive_cookies") as cookies:
            code, output, errors = self.invoke("sync-youtube-token")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["youtube_token"], {"status": "updated"})
        self.assertFalse(json.loads(output)["youtube_enabled"])
        downloader.assert_not_called()
        runner.assert_not_called()
        cookies.assert_not_called()

    def test_collection_imports_before_discovery_and_processing(self):
        downloader, runner = Mock(), Mock()
        downloader.discover.side_effect = lambda database: [] if self.token.is_file() else self.fail("missing token")
        runner.run.return_value = {}
        with patch("yt_archive.cli.Downloader", return_value=downloader), patch("yt_archive.cli.Runner", return_value=runner):
            code, output, errors = self.invoke("run")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.verifier.call_count, 1)
        self.assertEqual(json.loads(output)["youtube_token"]["status"], "updated")

    def test_failure_stops_work_and_records_only_safe_status(self):
        self.drive.error = RuntimeError("SECRET_ACCESS_TOKEN")
        with patch("yt_archive.cli.Downloader") as downloader:
            code, output, errors = self.invoke("run")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertNotIn("SECRET_", errors)
        downloader.assert_not_called()
        row = self.db.connection.execute("SELECT status,summary FROM runs").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(json.loads(row["summary"])["youtube_token"], {"status": "failed"})

    def test_metadata_failure_is_generic_and_preserves_existing_token(self):
        before = self.installed()
        self.drive.error = RuntimeError("SECRET_OAUTH")
        with self.assertRaises(ArchiveError) as caught:
            self.sync()
        self.assertNotIn("SECRET_", str(caught.exception))
        self.assert_preserved(before)

    def test_reports_expose_status_without_token_contents(self):
        from datetime import datetime
        from yt_archive.reports import KST
        start = datetime(2026, 11, 6, 1, tzinfo=KST).timestamp()
        with patch("yt_archive.db.epoch", return_value=start):
            run = self.db.start_run()
        with patch("yt_archive.db.epoch", return_value=start + 5):
            self.db.finish_run(run, "success", {"youtube_token": {"status": "updated", "token": "SECRET_ACCESS"}})
        report = build_report(self.db, self.config, "2026-11-06", now=start + 10)
        self.assertEqual(report["runs"][0]["youtube_token_status"], "updated")
        self.assertIn("YouTube 업로드 토큰: 수신·확인 완료", render_markdown(report))
        self.assertNotIn("SECRET_", json.dumps(report) + render_markdown(report))


class TokenValidationTests(Fixture):
    def test_live_verifier_only_refreshes_and_reads_expected_channel(self):
        credentials = Mock(granted_scopes=SCOPES)
        credentials.to_json.return_value = token_bytes().decode()
        session = Mock()
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.json.return_value = {"items": [{"id": CHANNEL}]}
        with patch("google.oauth2.credentials.Credentials.from_authorized_user_info", return_value=credentials), \
             patch("google.auth.transport.requests.AuthorizedSession") as factory:
            factory.return_value.__enter__.return_value = session
            info = verify_youtube_token(token_info(token_bytes()), CHANNEL)
        credentials.refresh.assert_called_once()
        self.assertEqual(info["refresh_token"], "SECRET_REFRESH")
        session.get.assert_called_once_with(API + "channels", params={"part": "id", "mine": "true"},
                                            timeout=(20, 60), allow_redirects=False)
        session.post.assert_not_called()

    def test_wrong_channel_and_upstream_failure_are_safe_errors(self):
        credentials = Mock(granted_scopes=SCOPES)
        session = Mock()
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.json.return_value = {"items": [{"id": "UC" + "b" * 22}]}
        with patch("google.oauth2.credentials.Credentials.from_authorized_user_info", return_value=credentials), \
             patch("google.auth.transport.requests.AuthorizedSession") as factory:
            factory.return_value.__enter__.return_value = session
            with self.assertRaises(ArchiveError):
                verify_youtube_token(token_info(token_bytes()), CHANNEL)
            credentials.refresh.side_effect = RuntimeError("SECRET_UPSTREAM")
            with self.assertRaises(ArchiveError) as caught:
                verify_youtube_token(token_info(token_bytes()), CHANNEL)
            self.assertNotIn("SECRET_", str(caught.exception))

    def test_config_requires_explicit_boolean_and_channel(self):
        self.assertFalse(load_config(self.config_path).youtube_token_from_drive)
        for content in ('token_from_drive = "true"', 'token_from_drive = true'):
            self.config_path.write_text('[youtube]\n' + content, encoding="utf-8")
            with self.assertRaises(ArchiveError):
                load_config(self.config_path)
        self.config_path.write_text(f'[youtube]\ntoken_from_drive = true\nchannel_id = "{CHANNEL}"', encoding="utf-8")
        self.assertTrue(load_config(self.config_path).youtube_token_from_drive)

    def test_drive_metadata_rejects_duplicates_and_wrong_size(self):
        drive = Drive(self.config)
        entry = {"Name": "youtube-token.json", "ID": "token-id", "IsDir": False, "Size": 826,
                 "ModTime": "2026-11-06T00:00:00Z", "Hashes": {"md5": "a" * 32}}
        with patch.object(drive, "run", return_value=json.dumps([entry])):
            self.assertEqual(drive.youtube_token_snapshot()["size"], 826)
        for entries in ([], [entry, entry], [{**entry, "Size": 65537}]):
            with patch.object(drive, "run", return_value=json.dumps(entries)), self.assertRaises(ArchiveError):
                drive.youtube_token_snapshot()

    def test_doctor_reads_metadata_without_downloading(self):
        drive = TokenDrive()
        config = replace(self.config, youtube_token_from_drive=True, youtube_channel=CHANNEL)
        with patch("yt_archive.cli.Drive", return_value=drive), contextlib.redirect_stdout(io.StringIO()):
            doctor(config)
        self.assertEqual((drive.reads, drive.copies), (1, 0))

    def test_transfer_uses_private_drive_path_and_size_bound(self):
        drive = Drive(self.config)
        destination = self.root / "staged.json"
        def receive(command, *, stdout, stderr, timeout):
            self.assertEqual(command[-4:], ["cat", self.config.drive_remote + "/관리용 파일/youtube-token.json",
                                            "--head", "65537"])
            self.assertEqual(timeout, 180)
            stdout.write(token_bytes())
            return Mock(returncode=0)
        with patch("yt_archive.drive.subprocess.run", side_effect=receive) as command:
            drive.download_youtube_token(destination)
        self.assertEqual(destination.read_bytes(), token_bytes())
        self.assertEqual(command.call_count, 1)

    def test_oversized_or_failed_download_is_rejected_without_secret_errors(self):
        drive = Drive(self.config)
        destination = self.root / "staged.json"
        def oversized(command, *, stdout, **kwargs):
            stdout.write(b"x" * 65537)
            return Mock(returncode=0)
        with patch("yt_archive.drive.subprocess.run", side_effect=oversized), self.assertRaises(ArchiveError):
            drive.download_youtube_token(destination)
        with patch("yt_archive.drive.subprocess.run", side_effect=OSError("SECRET_UPSTREAM")):
            with self.assertRaises(ArchiveError) as caught:
                drive.download_youtube_token(destination)
            self.assertNotIn("SECRET_", str(caught.exception))
