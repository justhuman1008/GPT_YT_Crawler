# GPT_YT_Crawler
GPT 클라우드용

멤버십 가입 계정으로 시청할 수 있는 YouTube 영상 한 개를 다운로드하고, 계정2를 통해 계정1의 공유 OneDrive 폴더에 저장합니다. 기본 실행은 15초 테스트이며, `--full`을 지정하면 전체 영상을 받습니다.

## Codex Cloud 환경 준비

Python 3.10 이상, ffmpeg, rclone, Deno 2.3 이상 또는 Node.js 22 이상이 필요합니다.

```sh
python3 -m pip install -r requirements.txt
```

개인 보관함에 아래 두 값을 `Environment variable`로 등록하고, 환경에서도 해당 키를 개인 값으로 요청하도록 설정합니다.

| 키 | 값 |
| --- | --- |
| `RCLONE_CONFIG_B64` | 계정2 전용 rclone 설정 파일의 Base64 |
| `YOUTUBE_COOKIES_B64` | 멤버십 계정의 youtube.com 쿠키 파일을 Netscape 형식으로 내보낸 뒤 Base64로 인코딩한 값 |

YouTube 영상 서버와 Microsoft OneDrive에 연결할 수 있도록 환경의 인터넷 접근을 설정합니다. 변경 후 Republish하고 새 작업에서 실행합니다. 준비된 환경 스냅샷에는 인증 파일을 만들지 않습니다.

## 15초 다운로드 및 OneDrive 검증

```sh
python3 youtube_to_onedrive.py
```

기본 영상은 `https://www.youtube.com/watch?v=UOwDYIBeq9E`입니다. 다른 영상은 `--url`로 지정합니다.

프로그램은 다음을 실제로 검사합니다.

1. 계정2 전용 rclone 설정과 YouTube 로그인 쿠키의 형식
2. 공유 폴더 접근
3. 제공되는 최고 화질의 영상과 음성 다운로드
4. 영상·음성의 첫 15초 디코딩
5. `onedrive2:바로 가기/유튜브 멤버쉽 영상`에 업로드
6. 원격 파일 크기와 다시 내려받은 파일의 SHA256 일치

15초 테스트 파일은 `test-영상ID-15s-실행ID` 이름으로 저장됩니다. 검증 결과는 로컬 `downloads/verification-*.json`에 저장되며, 모든 검증에 성공해야 종료 코드가 0이 됩니다.

## 전체 영상 다운로드

클라우드에서 15초 테스트가 통과한 뒤 실행합니다.

```sh
python3 youtube_to_onedrive.py --full
```

전체 영상은 영상 ID를 파일명으로 사용합니다. 영상·음성 코덱을 재인코딩하지 않고 합치며, 기본 병합 형식은 MKV입니다. 기존 OneDrive 파일을 삭제하지 않으며, `rclone copyto --immutable`을 사용해 다른 내용으로 덮어쓰지 않습니다.

## 인증 파일과 실행 결과

인증 값은 실행 중 임시 디렉터리에 복원하고, 실행 종료 시 해당 디렉터리를 정리합니다. 쿠키 값, OAuth 토큰, 서명된 다운로드 주소는 결과와 오류 로그에서 출력하지 않습니다. 쿠키 파일과 rclone 설정은 Git에 넣지 않습니다.

쿠키 만료, YouTube 인증 요구 또는 영상 접근 권한 변경이 발생하면 실패를 보고합니다. 필요할 경우 yt-dlp의 [YouTube 안내](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)와 [PO Token 안내](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)에 따라 실행 환경을 점검합니다.

## 검증 상태

2026-10-12 현재 Windows PC에서 기본 영상의 1080p 영상·음성 15초 다운로드, 디코딩, 계정2를 통한 OneDrive 업로드 및 SHA256 재검증을 통과했습니다. 결과는 `tests/local_smoke_result.json`에 기록했습니다.

Codex Cloud에서의 실제 실행과 전체 영상 다운로드는 아직 검증하지 않았습니다. 주기 실행은 클라우드 검증과 대상 채널·주기 결정 후 별도로 설정합니다.
