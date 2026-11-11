# 안경 ↔ 서버 프로토콜

WebSocket 하나로 오간다. 주소는 `ws://<서버>:8000/ws` (외부에서는 `wss://`).

## 인증
연결 요청 헤더 `Authorization: Bearer <토큰>`. 서버 환경 변수 `CONVERSATE_TOKEN` 과 다르면 서버가 연결을 받지 않는다(HTTP 403). 안경 앱은 403/401 을 받으면 재시도를 멈추고 `토큰이 맞지 않음` 을 띄운다.

토큰을 쿼리 파라미터가 아니라 헤더로 보내는 건, 주소가 로그에 남아도 토큰이 새지 않게 하려는 것 (네비 레포 보안 메모).

## 안경 → 서버
| 종류 | 내용 |
|---|---|
| 텍스트 `{"type":"hello","sample_rate":16000,"format":"pcm_s16le","channels":1}` | 연결마다 처음 한 번. 형식이 다르면 `error` |
| 텍스트 `"format":"opus"` (hello 안) | 배터리 때문에 소리를 Opus(32 kbps, 20 ms 패킷)로 보낸다. 바이너리 메시지 하나에 패킷 여러 개, 각 패킷 앞에 2바이트 빅엔디언 길이. 서버가 PCM 으로 풀어 똑같이 처리 (`opus.py`). 안경 인코더가 없으면 `pcm_s16le` |
| 텍스트 `"korean_captions":false` (hello 안) / `{"type":"settings","korean_captions":false}` | 한국어 자막 끔(메뉴 → 설정). 서버는 한국어도 계속 받아써서 AI 카드와 기록에 쓰지만 자막으로 보내지 않고, 한국어 미확정 자막은 받아쓰지도 않는다. 외국어는 원문과 번역을 그대로 보낸다 |
| 텍스트 `{"type":"battery","level":82,"charging":false,"per_hour":6.5}` | 배터리 잔량과 이 앱을 켠 뒤 시간당 소모(%). 서버 로그용 |
| 텍스트 `"mode":"daily"` (hello 안) / `{"type":"mode","mode":"lecture"}` | 듣기 모드: `daily`(일상), `lecture`(수업·영화), `media`(미디어). 메뉴에서 고를 때 `mode`, 다시 연결할 때마다 hello 에 실려 온다. 서버는 모드마다 문장 자르는 기준과 AI 카드를 바꾼다 (`modes.py`) |
| 바이너리 | 오디오. 16 kHz, 모노, 16비트 리틀엔디언 PCM. 안경은 100 ms(3200바이트)씩 보내지만 크기는 자유 (샘플이 두 조각에 걸쳐도 서버가 이어 붙임). 배터리 때문에 조용할 땐 보내지 않는다 (`SpeechGate`: 소음 바닥의 3배 이상이 0.2초 이어지면 직전 0.8초부터 보내고, 조용해진 뒤 1.5초 더 보낸 다음 멈춤). 서버는 소리가 끊겨도 실시간 번역 세션을 5초마다 확인해 닫는다 |
| 텍스트 `{"type":"stop"}` | 듣기 정지. 말하던 문장을 마저 받아쓴다 |

안경은 `ready` 를 받기 전의 오디오는 보내지 않고 버린다.

## 서버 → 안경
| 메시지 | 뜻 |
|---|---|
| `{"type":"ready"}` | hello 확인. 이제 오디오를 보내도 됨 |
| `{"type":"level","rms":123.4,"speech":false,"received_ms":5000}` | `hello` 에 `"levels":true` 를 보낸 클라이언트에만 0.25초마다. 서버가 받은 소리 크기(int16 RMS), 말하는 중인지, 지금까지 받은 길이. 점검 도구용 (안경은 배터리 때문에 받지 않는다) |
| `{"type":"caption","id":3,"text":"...","final":false}` | 자막. 같은 `id` 의 `final:false` 는 계속 덮어쓰고, `final:true` 가 오면 확정 |
| `{"type":"caption","id":3,"text":"Where is it?","final":true,"translation":"어디예요?"}` | 외국어였을 때, 확정 자막 뒤에 같은 `id` 로 한국어 번역이 따로 온다. 안경은 원문(연한 회색) 아래 번역(흰색). 외국어가 들리면 그 뒤 20초 동안 실시간 번역(gpt-realtime-translate)이 열려, 다음 발화부터는 미확정 자막(`final:false`)에도 `translation` 이 붙어 말하는 동안 번역이 자라고. 원문은 서버 STT(부분: 첫 부분 자막(0.8초)에서 Whisper base 로 언어 감지 후 그 언어로, 확정: gpt-transcribe). 외국어로 감지되면 그 자리에서 실시간 번역을 열고 그때까지의 소리를 다시 보낸다. 번역 조각은 도착 순서가 아니라 조각의 `elapsed_ms`(번역 세션에 넣은 소리 기준 시각)로, 그 시각에 말하던 문장에 붙인다. 확정 후 문장 번역을 만들어, 실시간 번역과 뜻이 비슷하면 그대로 두고(글이 튀지 않게) 많이 다르면 바꾼다. 받아쓰기가 외국어라도 소리로 다시 판정(Whisper base)해 한국어면 한국어로 다시 받아쓴다 |
| `{"type":"error","message":"..."}` | 오류. 연결은 유지 |
| `{"type":"assist_status","busy":true}` | AI 요청 시작(true)/끝(false). 안경은 그동안 `생각 중…` |
| `{"type":"assist","title":"...","text":"..."}` | 화면 위쪽 30% 의 AI 결과 (용어 설명, 검색 답, 인물·회사 배경). `text` 가 비면 지움. 서버에 `OPENAI_API_KEY` 가 있을 때만 |
| `{"type":"assist_image","title":"...","png":"<base64>"}` | 방금 보낸 카드(같은 `title`)의 그림. 사람·장소·동물·음식·제품처럼 보면 아는 대상일 때 위키백과 썸네일을 최대 160px 흑백 PNG 로 (`images.py`, `IMAGES=0` 으로 끔). 안경은 카드 오른쪽 바깥에 AI 영역 높이로 초록으로 |

## 서버 안에서 일어나는 일
1. 30 ms 단위로 소리 크기(RMS)를 재서, 배경 소음의 3배이면서 300 이상이면 "말하는 중"으로 본다 (`segmenter.py`).
2. 말이 시작되면 직전 0.3초부터 모은다. 0.7초 조용하면 문장 끝, 15초가 넘으면 강제로 끊는다. 0.3초보다 짧은 소리는 버린다.
3. 말하는 동안 1.5초마다 지금까지 모은 소리로 부분 자막(`final:false`, beam 1)을, 문장이 끝나면 확정 자막(`final:true`, beam 5)을 만든다.
4. 받아쓰기는 한 번에 하나씩. 확정 자막이 밀려 있으면 부분 자막은 건너뛴다 (늦게 온 부분 자막은 의미가 없으므로).
5. AI 결과 (`assist.py`): 확정 자막이 새로 20자 넘게 쌓이고 지난 요청에서 15초가 지나면(질문은 바로) 판단 단계(`ASSIST_TRIAGE_MODEL`, 기본 `gpt-6-luna`, 추론 없음 `ASSIST_TRIAGE_REASONING=none`, 최근 12줄만)가 띄울 것을 고르고 웹 검색이 필요한지(`search`)도 정한다. 답 단계(`ASSIST_MODEL`, 기본 `gpt-6.1-terra`)는 그때만 `web_search` 도구를 쓴다 (검색이 비용의 절반 이상이었음). 모델은 `{"title","text"}` 또는 `{"skip":true}` 를 돌려주고, skip·오류면 아무것도 보내지 않는다. 한 번에 한 요청만.

## 나중에 붙일 것 (아직 없음)
G2 Conversate 의 나머지 기능에 해당하는 메시지. 형식은 그때 정한다.
- `summary`: 대화 끝난 뒤 요약·할 일
- `translation`: 번역 자막

## OTA (HTTP)
같은 서버의 HTTP 주소 (`wss://host/ws` → `https://host`).
| 요청 | 인증 | 응답 |
|---|---|---|
| `GET /app/latest` | 안경 토큰 | `{"versionCode":2,"versionName":"0.2.0","size":…,"sha256":"…","uploadedAt":"…"}`, 없으면 404 |
| `GET /app/download` | 안경 토큰 | APK (`x-sha256` 헤더) |
| `PUT /app/upload?versionCode=2&versionName=0.2.0` | `OTA_UPLOAD_TOKEN` | 위와 같은 메타. 본문은 APK (80MB 이하, zip 머리 확인) |
