"""Private Google Sheets controls using the already-authorized Drive credentials."""
from __future__ import annotations

import configparser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import re

from .common import ArchiveError, atomic_text
from .config import Channel, channel_url


DAILY = "매일 지정 시각"
INTERVAL = "일정 간격"
SETTINGS_RANGE = "'자동 실행'!A4:B7"
CHANNELS_RANGE = "'채널 목록'!A6:C1005"


@dataclass(frozen=True)
class Schedule:
    enabled: bool
    mode: str
    hour: int = 3
    minute: int = 0
    interval_minutes: int = 30


@dataclass(frozen=True)
class SheetControls:
    schedule: Schedule
    channels: tuple[Channel, ...]


def checkbox(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().upper() in {"TRUE", "FALSE"}:
        return value.strip().upper() == "TRUE"
    if value in (None, ""):
        return False
    raise ValueError("checkbox")


def parse_controls(settings_rows, channel_rows):
    try:
        settings = {}
        for row in settings_rows:
            if len(row) != 2 or row[0] in settings:
                raise ValueError("settings")
            settings[row[0]] = row[1]
        if set(settings) != {"자동 실행", "실행 방식", "실행 시각", "반복 간격(분)"}:
            raise ValueError("settings labels")
        enabled = checkbox(settings["자동 실행"])
        mode = settings["실행 방식"]
        hour, minute, interval = 3, 0, 30
        if mode == DAILY:
            match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", str(settings["실행 시각"]).strip())
            if not match or int(match[1]) > 23 or int(match[2]) > 59:
                raise ValueError("time")
            hour, minute = int(match[1]), int(match[2])
        elif mode == INTERVAL:
            value = settings["반복 간격(분)"]
            if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value).strip()):
                raise ValueError("interval")
            interval = int(value)
            if not 1 <= interval <= 10080:
                raise ValueError("interval")
        else:
            raise ValueError("mode")
        channels, seen = [], set()
        for row_number, row in enumerate(channel_rows, 6):
            values = list(row) + [""] * max(0, 3 - len(row))
            url, download, youtube = values[:3]
            download, youtube = checkbox(download), checkbox(youtube)
            try:
                url = channel_url(str(url).strip())
            except ValueError:
                if not download and not youtube:
                    continue  # Existing placeholder text is inert until selected.
                raise ArchiveError(f"채널 목록 {row_number}행의 채널 링크를 확인하세요.") from None
            if url in seen:
                raise ArchiveError(f"채널 목록 {row_number}행의 채널 링크가 중복됩니다.")
            seen.add(url)
            channels.append(Channel(url.rsplit("/", 1)[-1], url, ("membership", "videos", "streams"),
                                    youtube=youtube, download=download))
        return SheetControls(Schedule(enabled, mode, hour, minute, interval), tuple(channels))
    except ArchiveError:
        raise
    except (ValueError, TypeError, KeyError):
        raise ArchiveError("시트의 자동 실행 설정을 확인하세요. 체크박스·실행 방식·시각(HH:MM)·반복 간격이 올바르지 않습니다.") from None


class GoogleSheets:
    def __init__(self, config):
        self.config = config
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + config.control_sheet_id

    @contextmanager
    def session(self):
        """Caller holds run_lock, shared with all rclone/credential writers."""
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.credentials import Credentials

            parser = configparser.RawConfigParser()
            parser.read(self.config.rclone_config, encoding="utf-8-sig")
            remote = self.config.drive_remote.split(":", 1)[0]
            if parser.get(remote, "type") != "drive":
                raise ValueError()
            token = json.loads(parser.get(remote, "token"))
            expiry = datetime.fromisoformat(token["expiry"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            credentials = Credentials(token=token["access_token"], refresh_token=token["refresh_token"],
                                      client_id=parser.get(remote, "client_id"), client_secret=parser.get(remote, "client_secret"),
                                      token_uri="https://oauth2.googleapis.com/token", expiry=expiry,
                                      scopes=["https://www.googleapis.com/auth/drive"])
            with AuthorizedSession(credentials) as session:
                yield session
            if credentials.token != token["access_token"]:
                token.update(access_token=credentials.token, refresh_token=credentials.refresh_token,
                             expiry=credentials.expiry.replace(tzinfo=timezone.utc).isoformat())
                parser.set(remote, "token", json.dumps(token, separators=(",", ":")))
                output = io.StringIO()
                parser.write(output)
                atomic_text(self.config.rclone_config, output.getvalue())
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Google 시트 연결에 실패했습니다. Drive 인증·Google Sheets API·시트 접근 권한을 확인하세요.") from None

    def read(self):
        try:
            with self.session() as session:
                response = session.get(self.base + "/values:batchGet",
                                       params=[("ranges", SETTINGS_RANGE), ("ranges", CHANNELS_RANGE),
                                               ("valueRenderOption", "FORMATTED_VALUE")],
                                       timeout=(20, 60), allow_redirects=False)
                if response.status_code != 200:
                    raise ArchiveError(f"Google 시트 읽기 실패 (HTTP {response.status_code}). 설정·채널은 적용하지 않았습니다.")
                value = response.json()
                ranges = value["valueRanges"]
                if value.get("spreadsheetId") != self.config.control_sheet_id or len(ranges) != 2:
                    raise ValueError()
                return parse_controls(ranges[0].get("values", []), ranges[1].get("values", []))
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Google 시트 응답을 확인할 수 없습니다. 설정·채널은 적용하지 않았습니다.") from None
