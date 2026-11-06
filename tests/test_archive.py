from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path, PurePosixPath
import struct
import tempfile
import types
import unittest
import zlib
from unittest.mock import Mock, patch

from yt_archive.cli import main
from yt_archive.common import (ArchiveError, Deferred, ReconcileRequired, Skipped, epoch,
                               run_lock, safe_error, sha256, time_matches, upload_time, video_url)
from yt_archive.config import Channel, load_config
from yt_archive.db import Database
from yt_archive.drive import Drive
from yt_archive.media import Downloader, validate_media
from yt_archive.runner import Runner
from yt_archive.thumbnails import collect_thumbnail, thumbnail_path, verify_thumbnail
from yt_archive.youtube import YouTube


SOURCE = "abcdefghijk"
SECOND = "lmnopqrstuv"
UPLOADED = "1234567890_"
CHANNEL = "UC" + "a" * 22
STAMP = 1728000000.0
JPEG = b"\xff\xd8\xfforiginal-thumbnail-bytes\xff\xd9"


class LayoutTests(unittest.TestCase):
    def test_channel_names_cannot_escape_the_video_root_or_use_management_folder(self):
        for name in ("../채널/위험", "..\\채널\\위험", "관리용 파일", " . ", "A/B:채널"):
            with self.subTest(name=name):
                target = Runner.target("gdrive:유튜브 동영상", {"channel": name, "channel_id": CHANNEL, "title": name}, SOURCE)
                parts = PurePosixPath(target.split(":", 1)[1]).parts
                self.assertEqual(len(parts), 3)
                self.assertEqual(parts[0], "유튜브 동영상")
                self.assertNotEqual(parts[1], "관리용 파일")
                self.assertNotIn("..", parts)
                self.assertNotIn("\\", target)
                self.assertTrue(parts[-1].endswith(f"[{SOURCE}].mkv"))

    def test_same_named_channels_have_distinct_folders(self):
        targets = [Runner.target("gdrive:유튜브 동영상", {"channel": "같은 채널명", "channel_id": channel, "title": "영상"}, SOURCE)
                   for channel in (CHANNEL, "UC" + "b" * 22)]
        self.assertNotEqual(targets[0].rsplit("/", 1)[0], targets[1].rsplit("/", 1)[0])


def png_fixture():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\x00\x00' * 2) * 2)) + chunk(b'IEND', b''))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text('extra_urls = []\n[download]\nsleep_seconds = 0\n[storage]\nmin_free_gib = 0\n', encoding="utf-8")
        self.config = load_config(self.config_path)
        self.db = Database(self.config.state_dir)
        self.addCleanup(self.db.close)

    def add(self, video_id=SOURCE, **kwargs):
        self.db.add(video_id, video_url(video_id)[1], **kwargs)
        return self.db.get(video_id)


class FakeDownloader:
    def __init__(self):
        self.calls = 0
        self.fail = None

    def download(self, job, directory):
        self.calls += 1
        if self.fail:
            (directory / "media.mkv.part").write_bytes(b"partial")
            raise self.fail
        path = directory / "media.mkv"
        path.write_bytes(b"complete-video-original-bytes")
        os.utime(path, (STAMP, STAMP))
        return path, {"video_id": job["video_id"], "title": "멤버십 영상", "description": "설명",
                      "upload_timestamp": STAMP, "time_precision": "second", "channel_id": CHANNEL}


class FakeDrive:
    def __init__(self):
        self.files = {}
        self.puts = []
        self.restores = 0
        self.fail = False

    def put_verified(self, path, target, digest, stamp):
        self.puts.append(target)
        if self.fail:
            raise ArchiveError("Drive verification failed")
        value = path.read_bytes()
        if target in self.files and self.files[target] != value:
            raise ArchiveError("collision")
        self.files[target] = value
        if sha256(path) != digest:
            raise ArchiveError("digest mismatch")
        return self.verify(target, len(value), digest, stamp)

    def verify(self, target, size, digest, stamp):
        import hashlib
        if self.fail or len(self.files.get(target, b"")) != size or hashlib.sha256(self.files[target]).hexdigest() != digest:
            raise ArchiveError("Drive changed")
        return {"size": size, "sha256": digest, "stamp": stamp}

    def confirm_unchanged(self, target, verified):
        self.verify(target, verified["size"], verified["sha256"], verified["stamp"])

    def restore(self, target, path, size, digest, stamp):
        self.restores += 1
        path.write_bytes(self.files[target])
        self.verify(target, size, digest, stamp)


class FakeYouTube:
    def __init__(self, fail=None, verified=True):
        self.calls = 0
        self.fail = fail
        self.verified = verified

    def ensure(self, database, job, path, metadata):
        self.calls += 1
        if self.fail:
            database.update(job["video_id"], youtube_session="https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret")
            raise self.fail
        database.update(job["video_id"], youtube_id=UPLOADED,
                        youtube_verified_at=epoch() if self.verified else None)


class PipelineTests(Fixture):
    def make_runner(self, youtube_enabled=False, youtube=None, **kwargs):
        self.config = replace(self.config, youtube_enabled=youtube_enabled,
                              youtube_channel=CHANNEL if youtube_enabled else "", **kwargs)
        self.downloader = getattr(self, "downloader", FakeDownloader())
        self.drive = getattr(self, "drive", FakeDrive())
        self.youtube = youtube or FakeYouTube()
        self.factory = Mock(return_value=self.youtube)
        return Runner(self.config, self.db, self.downloader, self.drive, self.factory)

    def media(self):
        return self.config.state_dir / "downloads" / SOURCE / "media.mkv"

    def test_default_off_drive_verified_then_deleted_without_youtube(self):
        runner = self.make_runner()
        self.assertEqual(runner.process(self.add()), "complete")
        self.factory.assert_not_called()
        self.assertFalse(self.media().exists())
        self.assertEqual(len(self.drive.files), 2)
        self.assertEqual(self.db.get(SOURCE)["state"], "complete")

    def test_youtube_enabled_both_verified_before_cleanup(self):
        runner = self.make_runner(True)
        self.assertEqual(runner.process(self.add()), "complete")
        self.assertEqual(self.youtube.calls, 1)
        self.assertIsNotNone(self.db.get(SOURCE)["youtube_verified_at"])
        self.assertFalse(self.media().exists())

    def test_drive_failure_keeps_local_and_never_calls_youtube(self):
        runner = self.make_runner(True)
        self.drive.fail = True
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())
        self.factory.assert_not_called()
        self.assertIsNone(self.db.get(SOURCE)["completed_at"])

    def test_youtube_failure_keeps_drive_and_local(self):
        runner = self.make_runner(True, FakeYouTube(ArchiveError("upload failed")))
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())
        self.assertEqual(len(self.drive.files), 2)
        self.assertIsNotNone(self.db.get(SOURCE)["drive_verified_at"])

    def test_youtube_processing_keeps_local(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        self.assertEqual(runner.process(self.add()), "waiting")
        self.assertTrue(self.media().exists())

    def test_unknown_upload_outcome_keeps_local(self):
        runner = self.make_runner(True, FakeYouTube(ReconcileRequired("expired")))
        self.assertEqual(runner.process(self.add()), "needs_reconcile")
        self.assertTrue(self.media().exists())

    def test_missing_youtube_verification_never_deletes(self):
        runner = self.make_runner(True, FakeYouTube(verified=False))
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())

    def test_restart_reuses_local_and_drive_without_downloading(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        runner.process(self.add())
        runner = self.make_runner(True)
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.downloader.calls, 1)
        self.assertFalse(self.media().exists())

    def test_completed_jobs_do_not_repeat(self):
        runner = self.make_runner()
        runner.process(self.add())
        self.assertEqual(self.db.jobs(self.config), [])
        self.assertEqual(runner.run(), {})
        self.assertEqual(self.downloader.calls, 1)

    def test_existing_upload_is_opt_in(self):
        runner = self.make_runner()
        runner.process(self.add())
        runner = self.make_runner(True)
        self.assertEqual(self.db.jobs(self.config), [])

    def test_existing_upload_restores_from_drive(self):
        runner = self.make_runner()
        runner.process(self.add())
        runner = self.make_runner(True, youtube_existing=True)
        self.assertEqual(len(self.db.jobs(self.config)), 1)
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.drive.restores, 1)
        self.assertEqual(self.downloader.calls, 1)

    def test_disabling_failed_backfill_still_cleans_restored_original(self):
        runner = self.make_runner()
        runner.process(self.add())
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")), youtube_existing=True)
        runner.process(self.db.get(SOURCE))
        self.assertTrue(self.media().exists())
        runner = self.make_runner(False)
        self.db.update(SOURCE, retry_at=0)
        self.assertEqual(len(self.db.jobs(self.config)), 1)
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertFalse(self.media().exists())

    def test_disabling_youtube_releases_waiting_cleanup(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        runner.process(self.add())
        runner = self.make_runner(False)
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.factory.assert_not_called()

    def test_corrupt_local_file_is_preserved(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        runner.process(self.add())
        self.media().write_bytes(b"corrupt")
        self.assertEqual(runner.process(self.db.get(SOURCE)), "failed")
        self.assertEqual(self.media().read_bytes(), b"corrupt")

    def test_corrupt_drive_prevents_cleanup_on_restart(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        runner.process(self.add())
        self.drive.files[self.db.get(SOURCE)["drive_target"]] = b"corrupt"
        self.assertEqual(runner.process(self.db.get(SOURCE)), "failed")
        self.assertTrue(self.media().exists())

    def test_drive_destination_change_is_blocked(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        runner.process(self.add())
        runner = self.make_runner(False, drive_remote="gdrive:Other")
        self.assertEqual(runner.process(self.db.get(SOURCE)), "failed")
        self.assertTrue(self.media().exists())

    def test_crash_after_commit_before_cleanup_recovers(self):
        runner = self.make_runner()
        with patch.object(runner, "cleanup", side_effect=OSError("crash")):
            runner.process(self.add())
        self.assertTrue(self.media().exists())
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.downloader.calls, 1)

    def test_crash_during_cleanup_without_media_recovers(self):
        runner = self.make_runner()
        def interrupted(directory):
            self.media().unlink()
            raise OSError("crash after unlink")
        with patch.object(runner, "cleanup", side_effect=interrupted):
            runner.process(self.add())
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.downloader.calls, 1)

    def test_download_failure_keeps_partial(self):
        runner = self.make_runner()
        self.downloader.fail = ArchiveError("interrupted")
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().with_suffix(".mkv.part").exists())

    def test_live_is_deferred_and_not_uploaded(self):
        runner = self.make_runner()
        self.downloader.fail = Deferred("live")
        self.assertEqual(runner.process(self.add()), "waiting")
        self.assertEqual(self.drive.files, {})

    def test_membership_filter_skip_is_recorded(self):
        runner = self.make_runner()
        self.downloader.fail = Skipped("not member only")
        self.assertEqual(runner.process(self.add()), "skipped")
        self.assertEqual(self.db.jobs(self.config), [])

    def test_manual_youtube_exclusion(self):
        runner = self.make_runner(True)
        runner.process(self.add(youtube_allowed=False))
        self.factory.assert_not_called()
        self.assertFalse(self.media().exists())

    def test_current_channel_setting_excludes_existing_job(self):
        channel = Channel("test", "https://www.youtube.com/@test", ("videos",), youtube=False)
        runner = self.make_runner(True, channels=(channel,))
        runner.process(self.add(source=channel.url))
        self.factory.assert_not_called()

    def test_youtube_per_run_cap_preserves_original(self):
        runner = self.make_runner(True, max_youtube_uploads=1)
        runner.process(self.add())
        job = self.add(SECOND)
        self.assertEqual(runner.process(job), "waiting")
        self.assertEqual(self.downloader.calls, 1)

    def test_cleanup_preserves_unrelated_files(self):
        runner = self.make_runner()
        directory = runner.directory(SOURCE)
        (directory / "keep.txt").write_text("original", encoding="utf-8")
        (directory / "media.notes.txt").write_text("original", encoding="utf-8")
        runner.process(self.add())
        self.assertEqual((directory / "keep.txt").read_text(encoding="utf-8"), "original")
        self.assertEqual((directory / "media.notes.txt").read_text(encoding="utf-8"), "original")
        self.assertFalse(self.media().exists())

    def test_drive_mutation_during_youtube_upload_blocks_deletion(self):
        runner = self.make_runner(True)
        original = self.youtube.ensure
        def mutate(database, job, path, metadata):
            original(database, job, path, metadata)
            self.drive.files[job["drive_target"]] = b"changed after verification"
        self.youtube.ensure = mutate
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())

    def test_sidecar_failure_blocks_youtube_and_deletion(self):
        runner = self.make_runner(True)
        original = self.drive.put_verified
        def fail_sidecar(path, target, digest, stamp):
            if target.endswith("metadata.json"):
                raise ArchiveError("sidecar failed")
            return original(path, target, digest, stamp)
        self.drive.put_verified = fail_sidecar
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())
        self.factory.assert_not_called()

    def test_path_tampering_blocks_cleanup(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("processing")))
        runner.process(self.add())
        other = self.root / "unrelated.mkv"
        other.write_bytes(b"keep")
        self.db.update(SOURCE, local_path=str(other))
        self.assertEqual(runner.process(self.db.get(SOURCE)), "failed")
        self.assertEqual(other.read_bytes(), b"keep")


class UtilityTests(Fixture):
    def test_default_config_disables_youtube(self):
        self.assertFalse(self.config.youtube_enabled)

    def test_invalid_boolean_is_rejected(self):
        self.config_path.write_text('[youtube]\nenabled = "false"', encoding="utf-8")
        with self.assertRaises(ArchiveError):
            load_config(self.config_path)

    def test_youtube_enabled_requires_channel(self):
        self.config_path.write_text('[youtube]\nenabled = true', encoding="utf-8")
        with self.assertRaises(ArchiveError):
            load_config(self.config_path)

    def test_relative_paths_resolve_against_config(self):
        self.assertEqual(self.config.cookies_file, self.root / "secrets" / "youtube-cookies.txt")

    def test_full_scan_is_due_until_success(self):
        self.assertTrue(self.db.full_scan_due("source", 168))
        self.db.scanned("source")
        self.assertFalse(self.db.full_scan_due("source", 168))

    def test_duplicate_discovery_preserves_state(self):
        self.add()
        self.db.update(SOURCE, state="drive_verified", drive_verified_at=1)
        self.add()
        self.assertEqual(len(self.db.public_status()), 1)
        self.assertEqual(self.db.get(SOURCE)["drive_verified_at"], 1)

    def test_scan_order_is_preserved_and_new_scan_has_priority(self):
        self.add(discovered_at=1)
        self.add(SECOND, discovered_at=1)
        self.assertEqual([row["video_id"] for row in self.db.jobs(self.config)], [SOURCE, SECOND])
        self.add(UPLOADED, discovered_at=2)
        self.assertEqual(self.db.jobs(self.config)[0]["video_id"], UPLOADED)

    def test_retry_time_prevents_immediate_repeat(self):
        self.add()
        self.db.update(SOURCE, retry_at=epoch() + 3600)
        self.assertEqual(self.db.jobs(self.config), [])

    def test_status_excludes_session_urls(self):
        self.add()
        self.db.update(SOURCE, youtube_session="https://token-secret", metadata='{"token":"secret"}')
        status = json.dumps(self.db.public_status())
        self.assertNotIn("secret", status)
        self.assertNotIn("youtube_session", status)

    def test_upload_timestamp_and_utc_date_fallback(self):
        self.assertEqual(upload_time({"timestamp": STAMP, "upload_date": "bad"}), (STAMP, "second"))
        stamp, precision = upload_time({"upload_date": "20260101"})
        self.assertTrue(time_matches(stamp, "2026-01-01T00:00:00Z"))
        self.assertEqual(precision, "day")

    def test_invalid_dates_fail_closed(self):
        for info in ({}, {"timestamp": True}, {"timestamp": float("nan")}, {"upload_date": "20260230"}):
            with self.subTest(info=info), self.assertRaises(ArchiveError):
                upload_time(info)

    def test_video_urls(self):
        for value in (SOURCE, "https://youtu.be/" + SOURCE,
                      "https://www.youtube.com/shorts/" + SOURCE,
                      "https://www.youtube.com/watch?v=" + SOURCE + "&list=extra"):
            self.assertEqual(video_url(value)[0], SOURCE)
        with self.assertRaises(ArchiveError):
            video_url("https://evil.example/watch?v=" + SOURCE)

    def test_raw_errors_are_not_logged(self):
        self.assertNotIn("token-secret", safe_error(RuntimeError("token-secret")))

    def test_run_lock_excludes_second_writer(self):
        with run_lock(self.config.state_dir):
            with self.assertRaises(ArchiveError), run_lock(self.config.state_dir):
                pass
        with run_lock(self.config.state_dir):
            pass

    def test_cli_add_and_status(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--config", str(self.config_path), "add", SOURCE]), 0)
            self.assertEqual(main(["--config", str(self.config_path), "status"]), 0)
        self.assertIn(SOURCE, output.getvalue())


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self.data, self.headers = status, data or {}, headers or {}

    def json(self):
        return self.data


class YouTubeTests(Fixture):
    def client(self, responses):
        self.config = replace(self.config, youtube_enabled=True, youtube_channel=CHANNEL, chunk_bytes=4)
        self.session = Mock()
        self.session.request.side_effect = responses
        return YouTube(self.config, self.session)

    def processed(self, privacy="unlisted", status="processed", channel=CHANNEL, description="", custom=True):
        return Response(data={"items": [{"snippet": {"channelId": channel, "description": description},
                                         "status": {"privacyStatus": privacy, "uploadStatus": status},
                                         "contentDetails": {"hasCustomThumbnail": custom},
                                         "processingDetails": {"processingStatus": "succeeded" if status == "processed" else "processing"}}]})

    def job_and_file(self):
        self.add()
        path = self.root / "video.mkv"
        path.write_bytes(b"abcdefgh")
        image = self.root / "thumbnail.jpg"
        image.write_bytes(JPEG)
        self.metadata = {"title": "test", "description": "", "thumbnail": {
            "filename": image.name, "mime_type": "image/jpeg", "file_size": len(JPEG), "sha256": sha256(image)}}
        self.db.update(SOURCE, file_size=8)
        return self.db.get(SOURCE), path

    def test_new_upload_is_unlisted_and_durable_before_bytes(self):
        description = "원문 설명\nhttps://example.com/\n" + "가" * 1500
        location = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret"
        client = self.client([Response(data={"items": [{"id": CHANNEL}]}),
                              Response(headers={"Location": location}), Response(308),
                              Response(308, headers={"Range": "bytes=0-3"}),
                              Response(data={"id": UPLOADED}), self.processed(description=description),
                              Response(data={"items": [{"default": {"url": "https://i.ytimg.com/new.jpg"}}]}), self.processed(description=description)])
        job, path = self.job_and_file()
        self.metadata["description"] = description
        original_request = client.request
        def observe(method, url, **kwargs):
            if method == "PUT" and kwargs.get("data"):
                self.assertEqual(self.db.get(SOURCE)["youtube_session"], location)
            return original_request(method, url, **kwargs)
        with patch.object(client, "request", side_effect=observe):
            client.ensure(self.db, job, path, self.metadata)
        calls = self.session.request.call_args_list
        self.assertEqual(calls[1].kwargs["json"]["status"]["privacyStatus"], "unlisted")
        self.assertEqual(calls[3].kwargs["headers"]["Content-Range"], "bytes 0-3/8")
        self.assertEqual(calls[4].kwargs["headers"]["Content-Range"], "bytes 4-7/8")
        self.assertEqual(self.db.get(SOURCE)["youtube_id"], UPLOADED)
        self.assertIsNotNone(self.db.get(SOURCE)["youtube_verified_at"])
        self.assertEqual(calls[1].kwargs["json"]["snippet"]["description"], description)
        self.assertEqual(calls[6].kwargs["data"], JPEG)
        self.assertEqual(calls[6].kwargs["params"]["videoId"], UPLOADED)
        self.assertEqual(self.db.get(SOURCE)["youtube_thumbnail_sha256"], self.metadata["thumbnail"]["sha256"])

    def test_description_is_exact_including_unicode_links_and_line_breaks(self):
        description = "원본 설명 🎵\n\n00:00 시작\nhttps://example.com/?a=1&b=2\n"
        client = self.client([Response(data={"items": [{"id": CHANNEL}]}),
                              self.processed(description=description),
                              Response(data={"items": [{"default": {"url": "https://i.ytimg.com/new.jpg"}}]}),
                              self.processed(description=description)])
        job, path = self.job_and_file()
        self.metadata["description"] = description
        self.db.update(SOURCE, youtube_id=UPLOADED)
        client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
        self.assertEqual(client.original_description(self.metadata), description)
        self.assertIsNotNone(self.db.get(SOURCE)["youtube_verified_at"])

    def test_description_limit_uses_utf8_bytes_without_silent_truncation(self):
        self.assertEqual(YouTube.original_description({"description": "a" * 5000}), "a" * 5000)
        for value in ("가" * 1667, "a" * 5001, "before < after", "before > after"):
            with self.subTest(value=value[:10]), self.assertRaises(ArchiveError):
                YouTube.original_description({"description": value})

    def test_thumbnail_failure_retries_same_uploaded_video_without_new_upload(self):
        client = self.client([Response(data={"items": [{"id": CHANNEL}]}), self.processed(), Response(403)])
        _, path = self.job_and_file()
        self.db.update(SOURCE, youtube_id=UPLOADED)
        with self.assertRaises(ArchiveError):
            client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
        self.assertEqual(self.db.get(SOURCE)["youtube_id"], UPLOADED)
        self.assertIsNone(self.db.get(SOURCE)["youtube_verified_at"])
        self.assertTrue(path.exists())
        self.session.request.side_effect = [self.processed(), Response(data={"items": [{"default": {"url": "https://i.ytimg.com/new.jpg"}}]}), self.processed()]
        client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
        self.assertTrue(all(call.args[1].endswith("/thumbnails/set") for call in self.session.request.call_args_list if call.args[0] == "POST"))

    def test_new_video_id_is_durable_when_thumbnail_is_rejected(self):
        location = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret"
        client = self.client([Response(data={"items": [{"id": CHANNEL}]}), Response(headers={"Location": location}),
                              Response(data={"id": UPLOADED}), self.processed(), Response(403)])
        job, path = self.job_and_file()
        with self.assertRaises(ArchiveError):
            client.ensure(self.db, job, path, self.metadata)
        self.assertEqual(self.db.get(SOURCE)["youtube_id"], UPLOADED)
        self.assertEqual(self.db.get(SOURCE)["youtube_session"], location)
        self.assertIsNone(self.db.get(SOURCE)["youtube_verified_at"])
        self.assertTrue(path.exists())

    def test_image_changed_during_read_is_never_sent(self):
        client = self.client([])
        self.job_and_file()
        with patch.object(Path, "read_bytes", return_value=b"changed"), self.assertRaises(ArchiveError):
            client.set_thumbnail(UPLOADED, self.root / "thumbnail.jpg", self.metadata["thumbnail"])
        self.session.request.assert_not_called()

    def test_empty_thumbnail_acknowledgement_is_not_marked_verified(self):
        client = self.client([Response(data={"items": [{"id": CHANNEL}]}), self.processed(), Response(data={"items": []})])
        _, path = self.job_and_file()
        self.db.update(SOURCE, youtube_id=UPLOADED)
        with self.assertRaises(Deferred):
            client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
        self.assertIsNone(self.db.get(SOURCE)["youtube_thumbnail_set_at"])

    def test_thumbnail_checkpoint_waits_for_propagation_without_resending_image(self):
        client = self.client([Response(data={"items": [{"id": CHANNEL}]}), self.processed(),
                              Response(data={"items": [{"default": {"url": "https://i.ytimg.com/new.jpg"}}]}), self.processed(custom=False)])
        _, path = self.job_and_file()
        self.db.update(SOURCE, youtube_id=UPLOADED)
        with self.assertRaises(Deferred):
            client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
        self.assertIsNone(self.db.get(SOURCE)["youtube_verified_at"])
        self.session.request.side_effect = [self.processed(), self.processed()]
        client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
        posts = [call for call in self.session.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_missing_or_corrupt_thumbnail_stops_before_video_insertion(self):
        for missing in (True, False):
            client = self.client([Response(data={"items": [{"id": CHANNEL}]})])
            _, path = self.job_and_file()
            if missing:
                self.metadata["thumbnail"] = None
            else:
                (self.root / "thumbnail.jpg").write_bytes(b"corrupt")
            with self.assertRaises(ArchiveError):
                client.ensure(self.db, self.db.get(SOURCE), path, self.metadata)
            self.assertEqual(self.session.request.call_count, 1)

    def test_wrong_description_is_not_marked_verified(self):
        client = self.client([self.processed(description="modified")])
        with self.assertRaises(ArchiveError):
            client.verify(UPLOADED, "original")

    def test_resume_uses_server_offset_without_new_post(self):
        client = self.client([Response(308, headers={"Range": "bytes=0-3"}), Response(data={"id": UPLOADED})])
        _, path = self.job_and_file()
        result = client.transfer("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret", path, 8)
        self.assertEqual(result, UPLOADED)
        self.assertEqual(self.session.request.call_args_list[1].kwargs["data"], b"efgh")
        self.assertTrue(all(call.args[0] == "PUT" for call in self.session.request.call_args_list))

    def test_lost_final_response_recovers_uploaded_id(self):
        client = self.client([Response(data={"id": UPLOADED})])
        _, path = self.job_and_file()
        self.assertEqual(client.transfer("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x", path, 8), UPLOADED)
        self.assertEqual(self.session.request.call_count, 1)

    def test_expired_session_never_creates_second_upload(self):
        client = self.client([Response(404)])
        _, path = self.job_and_file()
        with self.assertRaises(ReconcileRequired):
            client.transfer("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x", path, 8)
        self.assertEqual(self.session.request.call_count, 1)

    def test_wrong_channel_is_rejected_before_upload(self):
        client = self.client([Response(data={"items": [{"id": "wrong"}]})])
        job, path = self.job_and_file()
        with self.assertRaises(ArchiveError):
            client.ensure(self.db, job, path, {"title": "test"})
        self.assertEqual(self.session.request.call_count, 1)

    def test_private_or_public_result_is_not_accepted(self):
        for privacy in ("private", "public"):
            client = self.client([self.processed(privacy)])
            with self.subTest(privacy=privacy), self.assertRaises(ArchiveError):
                client.verify(UPLOADED)

    def test_other_channels_video_is_rejected(self):
        client = self.client([self.processed(channel="other")])
        with self.assertRaises(ArchiveError):
            client.verify(UPLOADED)

    def test_processing_is_deferred(self):
        client = self.client([self.processed(status="uploaded")])
        with self.assertRaises(Deferred):
            client.verify(UPLOADED)

    def test_quota_error_is_preserved_as_actionable_error(self):
        client = self.client([Response(403)])
        with self.assertRaises(ArchiveError):
            client.verify(UPLOADED)

    def test_bad_session_cannot_receive_credentials(self):
        client = self.client([])
        _, path = self.job_and_file()
        with self.assertRaises(ArchiveError):
            client.transfer("https://evil.example/upload/youtube/v3/videos", path, 8)
        self.session.request.assert_not_called()

    def test_no_progress_does_not_loop(self):
        client = self.client([Response(308), Response(308)])
        _, path = self.job_and_file()
        with self.assertRaises(Deferred):
            client.transfer("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x", path, 8)
        self.assertEqual(self.session.request.call_count, 2)


class DriveTests(Fixture):
    def test_capacity_checked_before_large_download(self):
        drive = Drive(self.config)
        with patch.object(drive, "run", return_value='{"free": 1000}'):
            with self.assertRaises(ArchiveError):
                drive.ensure_capacity(10 * 1024**2)

    def test_verification_rejects_wrong_timestamp_before_readback(self):
        drive = Drive(self.config)
        with patch.object(drive, "stat", return_value={"Size": 8, "ModTime": "2000-01-01T00:00:00Z"}), \
                patch.object(drive, "remote_digest") as digest:
            with self.assertRaises(ArchiveError):
                drive.verify("gdrive:target", 8, "sha", STAMP)
        digest.assert_not_called()

    def test_verification_checks_full_content_and_stable_remote(self):
        drive = Drive(self.config)
        stat = {"ID": "one", "Size": 8, "ModTime": "2024-10-04T00:00:00Z"}
        self.assertTrue(time_matches(STAMP, stat["ModTime"]))
        with patch.object(drive, "stat", return_value=stat), patch.object(drive, "remote_digest", return_value="sha") as digest:
            drive.verify("gdrive:target", 8, "sha", STAMP)
            digest.assert_called_once()

    def test_existing_remote_is_never_overwritten(self):
        drive = Drive(self.config)
        path = self.root / "video.mkv"
        path.write_bytes(b"local")
        with patch.object(drive, "stat", return_value={"Size": 99}), patch.object(drive, "run") as run:
            with self.assertRaises(ArchiveError):
                drive.put_verified(path, "gdrive:target", sha256(path), STAMP)
        run.assert_not_called()

    def test_only_google_drive_backend_is_accepted(self):
        self.config.rclone_config.parent.mkdir()
        self.config.rclone_config.write_text("[gdrive]\ntype = onedrive\n", encoding="utf-8")
        with self.assertRaises(ArchiveError):
            Drive(self.config).validate_backend()
        self.config.rclone_config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
        Drive(self.config).validate_backend()


class DownloaderTests(Fixture):
    def module(self, extract):
        self.options = []
        self.extract = extract
        owner = self
        class Client:
            def __init__(self, options):
                owner.options.append(options)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, source, download=False):
                return owner.extract(source)
            def process_ie_result(self, info, download=True):
                (owner.config.state_dir / "downloads" / SOURCE / "media.mkv").write_bytes(b"video")
        return types.SimpleNamespace(YoutubeDL=Client)

    def source_config(self):
        self.config = replace(self.config, channels=(Channel("채널", "https://www.youtube.com/@channel", ("membership", "videos")),))
        return Downloader(self.config)

    def test_first_scan_reads_all_then_recent_scan_is_limited(self):
        downloader = self.source_config()
        module = self.module(lambda source: {"entries": [{"id": SOURCE, "title": "영상"}, {"id": SECOND}]})
        with patch.dict("sys.modules", {"yt_dlp": module}):
            self.assertEqual(downloader.discover(self.db), [])
            self.assertTrue(all("playlistend" not in options for options in self.options))
            self.options.clear()
            downloader.discover(self.db)
            self.assertTrue(all(options["playlistend"] == self.config.recent_limit for options in self.options))
        self.assertEqual(len(self.db.public_status()), 2)

    def test_broken_tab_does_not_block_other_tab_or_complete_its_scan(self):
        downloader = self.source_config()
        def extract(source):
            if source.endswith("membership"):
                raise RuntimeError("cookie-secret")
            return {"entries": [{"id": SOURCE}]}
        with patch.dict("sys.modules", {"yt_dlp": self.module(extract)}):
            failures = downloader.discover(self.db)
        self.assertEqual(failures, [self.config.channels[0].url + "/membership"])
        self.assertTrue(self.db.full_scan_due(failures[0], 168))
        self.assertIsNotNone(self.db.get(SOURCE))

    def test_mid_pagination_error_keeps_discoveries_and_marks_scan_incomplete(self):
        downloader = self.source_config()
        def entries():
            yield {"id": SOURCE}
            raise RuntimeError("page failed")
        with patch.dict("sys.modules", {"yt_dlp": self.module(lambda source: {"entries": entries()})}):
            self.assertEqual(len(downloader.discover(self.db)), 2)
        self.assertIsNotNone(self.db.get(SOURCE))
        self.assertTrue(self.db.full_scan_due(self.config.channels[0].url + "/videos", 168))

    def prepare_download(self, **info):
        job = self.add()
        directory = self.config.state_dir / "downloads" / SOURCE
        directory.mkdir(parents=True)
        metadata = {"id": SOURCE, "title": "테스트", "timestamp": STAMP, "channel_id": CHANNEL, **info}
        module = self.module(lambda source: metadata)
        return job, directory, module

    def test_live_video_is_not_downloaded(self):
        downloader = Downloader(self.config)
        job, directory, module = self.prepare_download(live_status="is_live")
        with patch.dict("sys.modules", {"yt_dlp": module}), self.assertRaises(Deferred):
            downloader.download(job, directory)
        self.assertFalse((directory / "media.mkv").exists())

    def test_missing_timestamp_stops_before_download(self):
        downloader = Downloader(self.config)
        job, directory, module = self.prepare_download(timestamp=None)
        with patch.dict("sys.modules", {"yt_dlp": module}), self.assertRaises(ArchiveError):
            downloader.download(job, directory)
        self.assertFalse((directory / "media.mkv").exists())

    def test_download_sets_original_timestamp_and_checks_drive_capacity(self):
        downloader = Downloader(self.config)
        downloader.capacity_check = Mock()
        job, directory, module = self.prepare_download(filesize=99)
        with patch.dict("sys.modules", {"yt_dlp": module}), patch("yt_archive.media.validate_media"):
            path, metadata = downloader.download(job, directory)
        self.assertEqual(path.stat().st_mtime, STAMP)
        self.assertEqual(metadata["title"], "테스트")
        self.assertTrue(self.options[-1]["writethumbnail"])
        downloader.capacity_check.assert_called_once_with(99)

    def test_insufficient_disk_stops_before_network(self):
        downloader = Downloader(self.config)
        downloader.config = replace(self.config, min_free_bytes=10**30)
        job, directory, module = self.prepare_download()
        with patch.dict("sys.modules", {"yt_dlp": module}), self.assertRaises(ArchiveError):
            downloader.download(job, directory)
        self.assertEqual(self.options, [])

    def test_ffprobe_requires_playable_video_stream(self):
        result = types.SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_type":"audio"}],"format":{"duration":"3"}}')
        with patch("yt_archive.media.subprocess.run", return_value=result), self.assertRaises(ArchiveError):
            validate_media(self.root / "video.mkv", "ffprobe")


if __name__ == "__main__":
    unittest.main()
