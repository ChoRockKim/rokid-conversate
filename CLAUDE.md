# rokid-conversate

Rokid 안경에서 대화를 실시간 자막으로 띄운다 (Even Realities G2 Conversate 가 목표). 사용자는 iPhone 이라 폰 앱 없이, 안경 앱이 폰 핫스팟으로 서버에 직접 WebSocket 으로 붙는다.

## 먼저 읽을 것
- `README.md` — 구조, 실행, 조작, 확인 상태
- `docs/00-summary.md` — G2 기능 정리, Rokid 근거, Whisper 실측, 열린 문제
- `docs/protocol.md` — 안경 ↔ 서버 메시지

## 구성
- `glasses/` Kotlin 안경 앱. 화면·입력은 `ChoRockKim/rokid-glass-ios-navigation` 의 `glasses-android` (브랜치 `feat/route-server`) 를 따른다: 480x282 논리 단위를 비율 유지해 화면 아래 가운데에, 여백 18, 색 `#00ff66`/`#4da86c`, R08 링은 HudView 하나를 접근성 노드로.
- `server/` Python (FastAPI + faster-whisper). 네비 서버는 TypeScript 지만 이 프로젝트는 사용자가 Python 으로 정했다.

## 작업 규칙
- 사실을 쓸 때 **직접 실행한 것**과 **문서·소스만 읽은 것**을 구분한다. 안경 실기·iPhone 핫스팟은 클라우드에서 못 하므로 "실기 미확인"으로 표시한다.
- 토큰·키는 환경 변수나 `local.properties` 로만. 저장소·로그·문서에 넣지 않는다.
- 화면을 바꾸면 `./gradlew :app:recordPaparazziDebug` 로 스냅샷을 다시 만들어 함께 커밋한다.
- 테스트: `cd server && .venv/bin/python -m pytest`, `cd glasses && ./gradlew :app:testDebugUnitTest`.
- Android SDK 가 없으면 `commandlinetools` 를 받아 `platforms;android-36`, `build-tools;36.0.0` 설치 후 `glasses/local.properties` 에 `sdk.dir`.
