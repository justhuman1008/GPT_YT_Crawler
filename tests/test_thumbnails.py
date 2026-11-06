from dataclasses import replace
import io
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
import test_archive as fixtures

from test_archive import (CHANNEL, JPEG, SOURCE, SECOND, STAMP, UPLOADED, Fixture, FakeDownloader,
                          FakeYouTube, png_fixture)
from yt_archive.cli import main
from yt_archive.common import ArchiveError, Deferred, sha256
from yt_archive.db import Database
from yt_archive.layout import companion_target
from yt_archive.thumbnails import collect_thumbnail, thumbnail_path, verify_thumbnail


class ThumbnailTests(Fixture):
    def collect(self, name, data, ffmpeg="ffmpeg"):
        source = self.root / name
        source.write_bytes(data)
        return collect_thumbnail({"thumbnails": [{"filepath": str(source)}]}, self.root, STAMP, ffmpeg)

    def test_jpeg_keeps_original_bytes_and_upload_time(self):
        info = self.collect("media.jpg", JPEG)
        image = thumbnail_path(self.root, info)
        self.assertEqual(image.read_bytes(), JPEG)
        self.assertEqual(image.stat().st_mtime, STAMP)
        verify_thumbnail(image, info)

    def test_png_keeps_original_bytes_without_conversion(self):
        with patch("yt_archive.thumbnails.subprocess.run") as converter:
            info = self.collect("media.png", png_fixture())
        converter.assert_not_called()
        self.assertEqual((self.root / info["filename"]).read_bytes(), png_fixture())

    def test_selected_thumbnail_does_not_use_stale_image(self):
        (self.root / "media.jpg").write_bytes(b"stale")
        info = self.collect("media.png", png_fixture())
        self.assertEqual(info["mime_type"], "image/png")

    def test_thumbnail_escape_and_corruption_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        source = outside / "media.jpg"
        source.write_bytes(JPEG)
        with self.assertRaises(ArchiveError):
            collect_thumbnail({"thumbnails": [{"filepath": str(source)}]}, self.root, STAMP, "ffmpeg")
        info = self.collect("media.jpg", JPEG)
        image = thumbnail_path(self.root, info)
        image.write_bytes(b"changed")
        with self.assertRaises(ArchiveError):
            verify_thumbnail(image, info)
        info["filename"] = "../outside/media.jpg"
        with self.assertRaises(ArchiveError):
            thumbnail_path(self.root, info)

    def test_oversize_image_is_preserved_and_rejected(self):
        with patch("yt_archive.thumbnails.MAX_THUMBNAIL_BYTES", 5), self.assertRaises(ArchiveError):
            self.collect("media.jpg", JPEG)
        self.assertEqual((self.root / "media.jpg").read_bytes(), JPEG)

    def test_missing_original_image_is_explicit(self):
        self.assertIsNone(collect_thumbnail({}, self.root, STAMP, "ffmpeg"))

    def test_real_ytdlp_thumbnail_writer_exposes_selected_file(self):
        import yt_dlp
        info = {"id": SOURCE, "ext": "mkv", "thumbnails": [{"id": "0", "url": "https://i.ytimg.com/example.jpg"}]}
        with yt_dlp.YoutubeDL({"writethumbnail": True, "quiet": True}) as client:
            with patch.object(client, "urlopen", return_value=io.BytesIO(JPEG)):
                client._write_thumbnails("video", info, str(self.root / "media.mkv"))
        metadata = collect_thumbnail(info, self.root, STAMP, "ffmpeg")
        self.assertEqual((self.root / metadata["filename"]).read_bytes(), JPEG)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg integration runs on the Ubuntu server")
    def test_webp_conversion_preserves_decoded_pixels(self):
        source = self.root / "media.png"
        source.write_bytes(png_fixture())
        webp = self.root / "media.webp"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
                        "-c:v", "libwebp", "-lossless", "1", str(webp)], check=True, capture_output=True)
        info = collect_thumbnail({"thumbnails": [{"filepath": str(webp)}]}, self.root, STAMP, "ffmpeg")
        image = thumbnail_path(self.root, info)
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(image), "-f", "rawvideo",
                                  "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
        self.assertEqual(decoded, b"\xff\x00\x00" * 4)
        verify_thumbnail(image, info)


class ThumbnailDownloader(FakeDownloader):
    def download(self, job, directory):
        path, metadata = super().download(job, directory)
        image = directory / "thumbnail.jpg"
        image.write_bytes(JPEG)
        os.utime(image, (STAMP, STAMP))
        metadata["thumbnail"] = {"filename": image.name, "mime_type": "image/jpeg",
                                 "file_size": len(JPEG), "sha256": sha256(image)}
        return path, metadata


class ThumbnailPipelineTests(Fixture):
    make_runner = fixtures.PipelineTests.make_runner
    media = fixtures.PipelineTests.media

    def setUp(self):
        super().setUp()
        self.downloader = ThumbnailDownloader()

    def test_drive_archives_image_and_metadata_before_cleanup(self):
        runner = self.make_runner()
        self.assertEqual(runner.process(self.add()), "complete")
        target = self.db.get(SOURCE)["drive_target"]
        self.assertEqual(self.drive.files[companion_target(target, "thumbnail.jpg")], JPEG)
        metadata = json.loads(self.drive.files[companion_target(target, "metadata.json")])
        self.assertEqual(metadata["thumbnail"]["file_size"], len(JPEG))
        self.assertFalse(self.media().parent.exists())

    def test_same_title_videos_have_separate_media_metadata_and_thumbnails(self):
        runner = self.make_runner()
        for video_id in (SOURCE, SECOND):
            self.assertEqual(runner.process(self.add(video_id)), "complete")
        self.assertEqual(len(self.drive.files), 6)
        targets = [self.db.get(video_id)["drive_target"] for video_id in (SOURCE, SECOND)]
        self.assertNotEqual(targets[0], targets[1])
        self.assertEqual(targets[0].rsplit("/", 1)[0], targets[1].rsplit("/", 1)[0])
        for video_id, target in zip((SOURCE, SECOND), targets):
            metadata = json.loads(self.drive.files[companion_target(target, "metadata.json")])
            self.assertEqual(metadata["video_id"], video_id)
            self.assertEqual(self.drive.files[companion_target(target, "thumbnail.jpg")], JPEG)

    def test_legacy_archive_can_be_restored_without_moving_or_redownloading(self):
        runner = self.make_runner()
        self.assertEqual(runner.process(self.add()), "complete")
        target = self.db.get(SOURCE)["drive_target"]
        legacy = f"{self.config.drive_remote}/{CHANNEL}/2024/{SOURCE}/media.mkv"
        for old, new in [(target, legacy), *[
            (companion_target(target, name), companion_target(legacy, name))
            for name in ("metadata.json", "thumbnail.jpg")
        ]]:
            self.drive.files[new] = self.drive.files.pop(old)
        self.db.update(SOURCE, drive_target=legacy)
        runner = self.make_runner(True, youtube_existing=True)
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.db.get(SOURCE)["drive_target"], legacy)
        self.assertEqual(self.drive.restores, 2)
        self.assertEqual(self.downloader.calls, 1)
        self.assertEqual(len(self.drive.files), 3)

    def test_backfill_restores_thumbnail_and_media_from_drive(self):
        runner = self.make_runner()
        runner.process(self.add())
        runner = self.make_runner(True, youtube_existing=True)
        original = self.youtube.ensure
        def check_image(db, job, path, metadata):
            verify_thumbnail(path.parent / "thumbnail.jpg", metadata["thumbnail"])
            original(db, job, path, metadata)
        self.youtube.ensure = check_image
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.drive.restores, 2)
        self.assertEqual(self.downloader.calls, 1)

    def test_failed_thumbnail_upload_keeps_files_and_skips_youtube(self):
        runner = self.make_runner(True)
        original = self.drive.put_verified
        def fail_image(path, target, digest, stamp):
            if target.endswith("thumbnail.jpg"):
                raise ArchiveError("image upload failed")
            return original(path, target, digest, stamp)
        self.drive.put_verified = fail_image
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())
        self.assertTrue((self.media().parent / "thumbnail.jpg").exists())
        self.factory.assert_not_called()

    def test_image_failure_preserves_video_and_reuses_local_copy(self):
        runner = self.make_runner(True, FakeYouTube(Deferred("thumbnail permission")))
        runner.process(self.add())
        self.assertTrue((self.media().parent / "thumbnail.jpg").exists())
        runner = self.make_runner(True)
        self.assertEqual(runner.process(self.db.get(SOURCE)), "complete")
        self.assertEqual(self.downloader.calls, 1)

    def test_remote_image_mutation_blocks_cleanup(self):
        runner = self.make_runner(True)
        original = self.youtube.ensure
        def mutate(db, job, path, metadata):
            original(db, job, path, metadata)
            self.drive.files[companion_target(job["drive_target"], "thumbnail.jpg")] = b"changed"
        self.youtube.ensure = mutate
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())

    def test_local_image_mutation_during_upload_blocks_cleanup(self):
        runner = self.make_runner(True)
        original = self.youtube.ensure
        def mutate(db, job, path, metadata):
            original(db, job, path, metadata)
            (path.parent / "thumbnail.jpg").write_bytes(b"changed")
        self.youtube.ensure = mutate
        self.assertEqual(runner.process(self.add()), "failed")
        self.assertTrue(self.media().exists())

    def test_old_database_gets_checkpoint_columns_without_losing_jobs(self):
        self.add()
        with self.db.connection:
            self.db.connection.execute("ALTER TABLE videos DROP COLUMN youtube_thumbnail_sha256")
            self.db.connection.execute("ALTER TABLE videos DROP COLUMN youtube_thumbnail_set_at")
        migrated = Database(self.config.state_dir)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.get(SOURCE)["video_id"], SOURCE)
        self.assertIsNone(migrated.get(SOURCE)["youtube_thumbnail_set_at"])

    def test_linked_video_still_requires_thumbnail_step(self):
        self.add()
        self.db.update(SOURCE, drive_verified_at=STAMP, metadata=json.dumps({"description": "원문"}))
        self.config_path.write_text(f'[youtube]\nenabled = true\nchannel_id = "{CHANNEL}"\n', encoding="utf-8")
        with patch("yt_archive.cli.YouTube") as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.original_description.return_value = "원문"
            self.assertEqual(main(["--config", str(self.config_path), "link-youtube", SOURCE, UPLOADED]), 0)
        job = self.db.get(SOURCE)
        self.assertEqual(job["youtube_id"], UPLOADED)
        self.assertEqual(job["state"], "youtube_processing")
        self.assertIsNone(job["youtube_verified_at"])
        self.assertIsNone(job["youtube_thumbnail_set_at"])
