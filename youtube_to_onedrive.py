"""Download one authorized YouTube video and verify its OneDrive upload.

Credentials are supplied through RCLONE_CONFIG_B64 and YOUTUBE_COOKIES_B64.
The default mode downloads a 15-second sample; --full downloads the whole video.
"""

import argparse
import base64
import configparser
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func


DESTINATION = "onedrive2:바로 가기/유튜브 멤버쉽 영상"
EXPECTED_RECEIVER_DRIVE = "bf37965565531fea"
DEFAULT_URL = "https://www.youtube.com/watch?v=UOwDYIBeq9E"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output-dir", default="downloads")
    parser.add_argument("--rclone", default="rclone")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    report_path = output_dir / f"verification-{run_id}.json"
    report = {
        "execution_platform": platform.system(),
        "mode": "full_video" if args.full else "sample_15_seconds",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "local_metadata_pass": False,
        "download_pass": False,
        "video_audio_decode_pass": False,
        "onedrive_upload_pass": False,
        "onedrive_size_pass": False,
        "onedrive_download_sha256_pass": False,
    }
    secret_values = []
    warnings = []

    def redact(value):
        text = str(value)
        for secret in secret_values:
            if len(secret) >= 8:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"https?://[^\s<>]+", "[url]", text)
        return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)

    class Logger:
        def debug(self, message):
            pass

        def warning(self, message):
            warnings.append(redact(message))

        def error(self, message):
            warnings.append(redact(message))

    try:
        credentials = {}
        for key in ("RCLONE_CONFIG_B64", "YOUTUBE_COOKIES_B64"):
            encoded = os.environ.get(key, "")
            if not encoded:
                raise RuntimeError(f"Missing {key}: request this key in the cloud environment")
            secret_values.append(encoded)
            credentials[key] = base64.b64decode(re.sub(r"\s+", "", encoded), validate=True).decode("utf-8-sig")
            os.environ.pop(key, None)

        config_text = credentials["RCLONE_CONFIG_B64"]
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read_string(config_text)
        except configparser.Error:
            raise RuntimeError("Invalid rclone config format") from None
        if config.sections() != ["onedrive2"]:
            raise RuntimeError("Use the account2-only rclone configuration containing onedrive2")
        remote = config["onedrive2"]
        if remote.get("type") != "onedrive" or remote.get("drive_id", "").lower() != EXPECTED_RECEIVER_DRIVE:
            raise RuntimeError("The rclone configuration does not match account2")
        token = json.loads(remote.get("token", "{}"))
        secret_values.extend(str(token.get(key, "")) for key in ("access_token", "refresh_token"))

        cookie_text = credentials["YOUTUBE_COOKIES_B64"].replace("\r\n", "\n")
        if not cookie_text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise RuntimeError("The cookies must use Netscape format")
        login_cookie_present = False
        for line in cookie_text.splitlines():
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            fields = line.split("\t", 6)
            if len(fields) != 7:
                raise RuntimeError("Invalid cookie row")
            secret_values.append(fields[6])
            domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
            if domain != "youtube.com" and not domain.endswith(".youtube.com"):
                raise RuntimeError("Export only youtube.com cookies")
            if fields[5] in {"SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}:
                login_cookie_present = True
        if not login_cookie_present:
            raise RuntimeError("YouTube login cookies are missing")

        ffmpeg = shutil.which(args.ffmpeg)
        if not ffmpeg:
            raise RuntimeError("Install ffmpeg before running this script")
        rclone = shutil.which(args.rclone)
        if not rclone:
            raise RuntimeError("Install rclone before running this script")
        os.environ["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")
        runtimes = {}
        for name in ("deno", "node"):
            runtime = shutil.which(name)
            if runtime:
                runtimes[name] = {"path": runtime}
        if not runtimes:
            raise RuntimeError("Install Deno >= 2.3 or Node >= 22 for YouTube JS challenges")

        with tempfile.TemporaryDirectory(prefix="youtube-onedrive-") as private_directory:
            private_dir = Path(private_directory)
            config_path = private_dir / "rclone.conf"
            cookie_path = private_dir / "cookies.txt"
            config_path.write_text(config_text, encoding="utf-8")
            cookie_path.write_text(cookie_text + "\n", encoding="utf-8")
            for path in (config_path, cookie_path):
                path.chmod(0o600)
            child_environment = os.environ.copy()
            for key in ("RCLONE_CONFIG_B64", "YOUTUBE_COOKIES_B64"):
                child_environment.pop(key, None)

            def run_rclone(arguments, timeout=1200):
                completed = subprocess.run(
                    [rclone, *arguments, "--config", str(config_path), "--contimeout", "20s", "--timeout", "60s", "--retries", "1"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, env=child_environment,
                )
                if completed.returncode:
                    raise RuntimeError("rclone: " + redact(completed.stderr[-2000:]))
                return completed.stdout

            if "onedrive2:" not in run_rclone(["listremotes"]).splitlines():
                raise RuntimeError("The onedrive2 remote was not found")
            run_rclone(["lsjson", DESTINATION, "--stat"])
            report["onedrive_folder_access_pass"] = True

            filename = "%(id)s.%(ext)s" if args.full else f"test-%(id)s-15s-{run_id}.%(ext)s"
            options = {
                "cookiefile": str(cookie_path),
                "logger": Logger(), "quiet": True, "noprogress": True,
                "noplaylist": True, "socket_timeout": 30,
                "extractor_retries": 1, "retries": 1, "fragment_retries": 1,
                "cachedir": False, "js_runtimes": runtimes,
                "ffmpeg_location": ffmpeg, "format": "bv*+ba/b",
                "outtmpl": str(output_dir / filename), "merge_output_format": "mkv",
            }
            if not args.full:
                options["download_ranges"] = download_range_func([], [(0, 15)])
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(args.url, download=False)
                report.update({
                    "local_metadata_pass": True, "video_id": info["id"],
                    "title": info.get("title"), "availability": info.get("availability"),
                    "source_duration_seconds": info.get("duration"),
                    "height": info.get("height"), "yt_dlp_version": yt_dlp.version.__version__,
                })
                known_size = sum(item.get("filesize") or item.get("filesize_approx") or 0 for item in info.get("requested_formats", [info]))
                if args.full and known_size and shutil.disk_usage(output_dir).free < 3 * known_size + 256 * 1024 * 1024:
                    raise RuntimeError("Insufficient disk space for download, merge and upload verification")
                downloader.process_ie_result(info, download=True)

            prefix = info["id"] if args.full else f"test-{info['id']}-15s-{run_id}"
            media = [path for path in output_dir.glob(prefix + ".*") if path.suffix in {".mkv", ".mp4", ".webm"}]
            if len(media) != 1 or media[0].stat().st_size == 0:
                raise RuntimeError("Expected one completed video file")
            video = media[0]
            report["download_pass"] = True
            decode = subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-t", "15", "-f", "null", "-"],
                capture_output=True, text=True, errors="replace", timeout=120, env=child_environment,
            )
            if decode.returncode:
                raise RuntimeError("Video/audio decode failed: " + redact(decode.stderr[-1000:]))
            report["video_audio_decode_pass"] = True
            target = DESTINATION + "/" + video.name
            run_rclone(["copyto", str(video), target, "--immutable"])
            report["onedrive_upload_pass"] = True
            remote_info = json.loads(run_rclone(["lsjson", target, "--stat"]))
            report["onedrive_size_pass"] = remote_info.get("Size") == video.stat().st_size
            if not report["onedrive_size_pass"]:
                raise RuntimeError("OneDrive file size differs")
            downloaded_copy = private_dir / "uploaded-video-check.bin"
            run_rclone(["copyto", target, str(downloaded_copy)])
            report["onedrive_download_sha256_pass"] = digest(video) == digest(downloaded_copy)
            if not report["onedrive_download_sha256_pass"]:
                raise RuntimeError("OneDrive downloaded file SHA256 differs")
            report.update({"remote_file": target, "file_size_bytes": video.stat().st_size, "sha256": digest(video)})

    except Exception as error:
        report["error"] = redact(error)
    finally:
        report["warnings"] = warnings[-10:]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["onedrive_download_sha256_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
