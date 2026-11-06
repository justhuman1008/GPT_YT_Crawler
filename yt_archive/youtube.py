from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .common import ArchiveError, Deferred, ReconcileRequired, VIDEO_ID, atomic_json, epoch
from .thumbnails import thumbnail_path, verify_thumbnail


SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
API = "https://www.googleapis.com/youtube/v3/"


def authorize(config):
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.youtube_client.is_file():
        raise ArchiveError("Desktop 앱용 OAuth 클라이언트 JSON을 설정 경로에 넣으세요.")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.youtube_client), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise ArchiveError("오프라인 갱신 토큰이 없습니다. OAuth 동의를 다시 진행하세요.")
    atomic_json(config.youtube_token, json.loads(credentials.to_json()))


class YouTube:
    def __init__(self, config, session=None, credentials=None):
        self.config = config
        self.checked_channel = False
        if session is None:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import AuthorizedSession

            if not config.youtube_token.is_file():
                raise ArchiveError("YouTube OAuth 인증이 필요합니다. auth-youtube 명령을 실행하세요.")
            credentials = Credentials.from_authorized_user_file(str(config.youtube_token), SCOPES)
            if not credentials.refresh_token:
                raise ArchiveError("YouTube 인증의 갱신 토큰이 없습니다.")
            session = AuthorizedSession(credentials)
        self.session = session
        self.credentials = credentials

    def request(self, method, url, **kwargs):
        # 308 is part of the upload protocol, never an HTTP redirect to follow.
        try:
            return self.session.request(method, url, timeout=(30, 180), allow_redirects=False, **kwargs)
        except Exception:
            raise Deferred("YouTube 연결 실패입니다. 저장된 업로드 세션으로 재시도합니다.") from None
        finally:
            if self.credentials is not None:
                atomic_json(self.config.youtube_token, json.loads(self.credentials.to_json()))

    @staticmethod
    def check_response(response, allowed=(200,)):
        if response.status_code not in allowed:
            if response.status_code in {429, 500, 502, 503, 504}:
                raise Deferred(f"YouTube 일시 오류 (HTTP {response.status_code}). 다음 실행에서 재시도합니다.")
            raise ArchiveError(f"YouTube API 오류 (HTTP {response.status_code}). 권한·할당량·채널 상태를 확인하세요.")

    def check_channel(self):
        if self.checked_channel:
            return
        response = self.request("GET", API + "channels", params={"part": "id", "mine": "true"})
        self.check_response(response)
        ids = [item["id"] for item in response.json().get("items", [])]
        if ids != [self.config.youtube_channel]:
            raise ArchiveError("OAuth로 선택한 YouTube 채널이 설정의 channel_id와 일치하지 않습니다.")
        self.checked_channel = True

    @staticmethod
    def valid_session(url):
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in {"www.googleapis.com", "upload.youtube.com"} and (
            parsed.path.startswith("/upload/youtube/") or parsed.path.startswith("/resumable/"))

    def ensure(self, database, job, path: Path, metadata):
        self.check_channel()
        video_id = job["video_id"]
        if job["youtube_channel"] and job["youtube_channel"] != self.config.youtube_channel:
            raise ArchiveError("진행 중인 업로드의 채널과 설정 채널이 다릅니다.")
        description = self.original_description(metadata)
        thumbnail = metadata.get("thumbnail")
        if not thumbnail:
            raise ArchiveError("원본 썸네일 보관본이 없어 YouTube 업로드를 완료할 수 없습니다. 원본 썸네일을 먼저 확보하세요.")
        image = thumbnail_path(path.parent, thumbnail)
        verify_thumbnail(image, thumbnail)
        uploaded = job["youtube_id"]
        if not uploaded:
            session_url = job["youtube_session"]
            if not session_url:
                title = metadata["title"].replace("<", "").replace(">", "")[:100] or video_id
                body = {"snippet": {"title": title, "description": description,
                                    "categoryId": self.config.category_id},
                        "status": {"privacyStatus": "unlisted"}}
                response = self.request("POST", "https://www.googleapis.com/upload/youtube/v3/videos",
                                        params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
                                        json=body, headers={"X-Upload-Content-Length": str(job["file_size"]),
                                                            "X-Upload-Content-Type": "video/x-matroska"})
                self.check_response(response, (200, 201))
                session_url = response.headers.get("Location", "")
                if not self.valid_session(session_url):
                    raise ArchiveError("YouTube 업로드 세션 주소가 올바르지 않습니다.")
                # Durable commit BEFORE the first byte is sent.
                database.update(video_id, youtube_session=session_url, youtube_channel=self.config.youtube_channel,
                                state="youtube_uploading")
            uploaded = self.transfer(session_url, path, job["file_size"])
            # Durable commit BEFORE checking processing, so retries cannot insert a new video.
            database.update(video_id, youtube_id=uploaded, state="youtube_processing")
        self.verify(uploaded, description)
        # Setting the same image is safe to retry if the response/DB commit is lost.
        if job.get("youtube_thumbnail_sha256") != thumbnail["sha256"] or not job.get("youtube_thumbnail_set_at"):
            self.set_thumbnail(uploaded, image, thumbnail)
            database.update(video_id, youtube_thumbnail_sha256=thumbnail["sha256"],
                            youtube_thumbnail_set_at=epoch(), state="youtube_thumbnail_set")
        self.verify(uploaded, description, require_thumbnail=True)
        database.record_check(video_id, "youtube", {"youtube_id": uploaded,
                              "channel_id": self.config.youtube_channel, "privacy": "unlisted",
                              "processing_complete": True, "description_matches": True,
                              "custom_thumbnail_present": True, "thumbnail_sha256": thumbnail["sha256"],
                              "thumbnail_set_at": database.get(video_id)["youtube_thumbnail_set_at"]})
        database.update(video_id, youtube_verified_at=epoch(), youtube_channel=self.config.youtube_channel,
                        state="youtube_verified")

    @staticmethod
    def original_description(metadata):
        description = metadata.get("description", "")
        if not isinstance(description, str) or len(description.encode("utf-8")) > 5000 or "<" in description or ">" in description:
            raise ArchiveError("원본 설명이 YouTube API의 5,000바이트·문자 제한을 넘습니다. 원문을 임의로 자르거나 수정하지 않고 파일을 유지합니다.")
        return description

    def set_thumbnail(self, uploaded, image, thumbnail):
        verify_thumbnail(image, thumbnail)
        contents = image.read_bytes()
        if len(contents) != thumbnail["file_size"] or hashlib.sha256(contents).hexdigest() != thumbnail["sha256"]:
            raise ArchiveError("전송 직전 원본 썸네일이 변경되었습니다. 로컬 파일을 유지합니다.")
        response = self.request("POST", "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                                params={"videoId": uploaded, "uploadType": "media"}, data=contents,
                                headers={"Content-Type": thumbnail["mime_type"]})
        if response.status_code == 403:
            raise ArchiveError("YouTube 맞춤 썸네일 설정이 거부됐습니다. 업로드 채널의 맞춤 썸네일 권한·할당량을 확인하세요. 기존 영상 ID와 로컬 파일을 유지합니다.")
        self.check_response(response)
        items = response.json().get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict) or not items[0]:
            raise Deferred("YouTube 썸네일 설정 결과를 확인하지 못했습니다. 같은 영상에 다시 적용합니다.")

    def transfer(self, session_url, path: Path, total):
        if not self.valid_session(session_url):
            raise ArchiveError("저장된 YouTube 세션 주소가 올바르지 않습니다.")
        response = self.request("PUT", session_url, data=b"",
                                headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"})
        if response.status_code in {404, 410}:
            raise ReconcileRequired("YouTube 세션이 만료되어 결과 확인이 필요합니다. Studio 확인 후 link-youtube 또는 reset-youtube를 사용하세요.")
        while True:
            self.check_response(response, (200, 201, 308))
            if response.status_code in {200, 201}:
                uploaded = response.json().get("id", "")
                if not isinstance(uploaded, str) or not VIDEO_ID.fullmatch(uploaded):
                    raise ReconcileRequired("YouTube 응답에 영상 ID가 없습니다. Studio에서 확인하세요.")
                return uploaded
            match = re.fullmatch(r"bytes=0-([0-9]+)", response.headers.get("Range", ""))
            if response.headers.get("Range") and not match:
                raise ReconcileRequired("YouTube 업로드 진행 범위를 확인할 수 없습니다.")
            offset = int(match[1]) + 1 if match else 0
            if offset >= total:
                raise Deferred("전체 전송 후 YouTube 완료 응답을 기다립니다.")
            with path.open("rb") as handle:
                handle.seek(offset)
                block = handle.read(min(self.config.chunk_bytes, total - offset))
            if not block:
                raise ArchiveError("YouTube 전송 중 로컬 파일이 변경되었습니다.")
            end = offset + len(block) - 1
            response = self.request("PUT", session_url, data=block,
                                    headers={"Content-Type": "video/x-matroska", "Content-Length": str(len(block)),
                                             "Content-Range": f"bytes {offset}-{end}/{total}"})
            if response.status_code in {404, 410}:
                raise ReconcileRequired("YouTube 업로드 세션이 만료되었습니다. 새 업로드 전에 Studio에서 결과를 확인하세요.")
            if response.status_code == 308:
                updated = re.fullmatch(r"bytes=0-([0-9]+)", response.headers.get("Range", ""))
                if not updated or int(updated[1]) < offset:
                    raise Deferred("YouTube 전송이 진행되지 않았습니다. 다음 실행에서 이어서 전송합니다.")

    def verify(self, uploaded, expected_description=None, require_thumbnail=False):
        response = self.request("GET", API + "videos", params={"part": "snippet,status,processingDetails,contentDetails", "id": uploaded})
        self.check_response(response)
        items = response.json().get("items", [])
        if len(items) != 1:
            raise ArchiveError("업로드한 YouTube 영상을 확인할 수 없습니다.")
        item = items[0]
        if item.get("snippet", {}).get("channelId") != self.config.youtube_channel:
            raise ArchiveError("확인한 YouTube 영상이 설정한 채널의 영상이 아닙니다.")
        status = item.get("status", {})
        processing = item.get("processingDetails", {}).get("processingStatus")
        if status.get("privacyStatus") != "unlisted":
            raise ArchiveError("실제 공개 상태가 일부 공개가 아닙니다. YouTube 프로젝트·채널 제한을 확인하세요.")
        if status.get("uploadStatus") in {"failed", "rejected", "deleted"} or processing in {"failed", "terminated"}:
            raise ArchiveError("YouTube에서 영상 처리 또는 업로드를 거부했습니다. 로컬 파일을 유지합니다.")
        if status.get("uploadStatus") != "processed" or processing not in {None, "succeeded"}:
            raise Deferred("YouTube 영상 처리 완료를 기다립니다. 로컬 파일을 유지합니다.")
        if expected_description is not None and item.get("snippet", {}).get("description") != expected_description:
            raise ArchiveError("YouTube에 적용된 설명이 원본과 다릅니다. Studio에서 원문을 확인하세요. 로컬 파일을 유지합니다.")
        if require_thumbnail and item.get("contentDetails", {}).get("hasCustomThumbnail") is not True:
            raise Deferred("YouTube 맞춤 썸네일 반영 확인을 기다립니다. 로컬 파일을 유지합니다.")
