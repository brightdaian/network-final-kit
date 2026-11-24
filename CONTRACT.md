# 기말 프로젝트 API 계약 (v1.3)

자동 채점기(`grader/grade.py`)는 **이 문서에 적힌 것만** 확인한다. 화면 디자인, DB 종류, 내부 구조, 이 문서에 없는 추가 API는 자유다.
여기에 적힌 형식 · 상태 코드 · 규칙은 정확히 지켜야 한다.

> 채점 때는 시뮬레이터가 **평가자 이미지 + 비밀 값**으로 바뀐다. 토픽 prefix 는 그대로지만
> **방 이름 · 정원 · 기기 배치 · 이상치 시점은 달라진다.** 방 이름이나 시점을 코드에 적어 두면 채점에서 틀린다.

---

## 0. 저장소 구성 규칙

| 규칙 | 내용 |
|---|---|
| **fresh clone 기동** | 평가자는 저장소를 **새 폴더에 clone 한 상태**에서 `docker compose up -d --build` **한 번만** 실행한다. `.env` 복사·계정 파일 생성 같은 추가 단계 없이 떠야 한다 |
| 비밀값 기본값 | 그래서 compose 에는 `${SIM_PW:-sim-dev-pass}` 처럼 **기본값**을 준다. 여기 쓰는 값은 채점용 테스트 계정이라 저장소에 남아도 감점하지 않는다. 단 **JWT 서명 키는 기본값을 적지 않는다** (비우고 앱이 무작위 생성) |
| 서비스 이름 | 시뮬레이터 `sim`, 브로커 `mosquitto` (이 두 이름은 고정) |
| 시뮬레이터 | `sensor-sim` 폴더를 그대로 쓴다 (코드 수정 금지 — 채점 때 평가자 이미지로 교체됨). `sim.yaml` 의 기기 구성만 바꾼다 |
| `topic_prefix` | **`auto` 로 둔다.** prefix 는 `<앞칸>/<뒷칸>` 두 칸이어야 하며, 칸 수를 바꾸면 시뮬레이터가 실행을 거부한다 |
| 브로커 | `eclipse-mosquitto:2` 이미지. 내부 네트워크에서 1883(평문) 리스너를 유지한다. 익명 접속 금지, 계정 + ACL 사용. 계정 파일(`passwd`)은 컨테이너가 시작할 때 만든다 (`sensor-sim/mosquitto/secure/entrypoint.sh`) |
| 채점용 브로커 계정 | `grading.json` 의 `mqtt_grader` 계정. ACL 에서 `<prefix>/#` **읽기 전용** |
| 호스트 포트 | 호스트에 공개하는 포트는 **FastAPI(uvicorn) 하나**. 그 번호를 `grading.json` 의 `api_port` 에 적는다 |
| `grading.json` | 저장소 루트에 둔다 (아래 형식). 여기 적는 계정은 채점용 테스트 계정이다 |
| `.env` | 저장소에 올리지 않는다 (`.env.example` 만). 올려도 스택이 뜨는 데 영향이 없어야 한다 |

출발점: 키트의 **`compose.template.yaml`** 을 내 저장소 루트에 `compose.yaml` 로 복사해서 쓴다.

`grading.json`:

```json
{
  "student_id": "202612345",
  "api_port": 8000,
  "compose_file": "compose.yaml",
  "accounts": {
    "viewer":  {"username": "viewer",  "password": "viewer-pass"},
    "manager": {"username": "manager", "password": "manager-pass"}
  },
  "mqtt_grader": {"username": "grader", "password": "grader-pass"}
}
```

기기 구성 최소 조건: `env` · `co2` · `vent` 는 각 1대 이상, 그 밖에 센서 종류 1가지 이상 더, 합계 8대 이상.

> **제출 전 반드시**: 내 저장소를 **다른 폴더에 clone** 해서 그 사본으로 공개 채점기를 한 번 돌린다.
> 내 작업 폴더에만 있는 `.env` · `passwd` 때문에 "나는 되는데 채점은 0점" 이 되는 사고를 막는 유일한 방법이다.

---

## 1. 공통

- 모든 경로는 `/api/v1` 아래, 요청·응답 본문은 JSON (SSE 제외).
- 시각은 모두 **Unix epoch 밀리초 정수**.
- 인증: `POST /auth/login` 으로 받은 JWT 를 `Authorization: Bearer <token>` 으로 보낸다.
  **SSE 는 `?token=<token>` 쿼리로만 인증한다** (브라우저 `EventSource` 가 헤더를 못 붙이기 때문 — 채점기도 헤더 없이 호출한다).
- 토큰 없음·잘못된 토큰 → **401**. 권한 부족 → **403**. 없는 기기 → **404**. 본문·쿼리 형식 오류 → **422**.
- 오류 본문은 자유 형식이지만 JSON 이어야 한다.

## 2. 엔드포인트

### `GET /api/v1/health` — 인증 없음
`200 {"status": "ok"}`

### `POST /api/v1/auth/login`
요청 `{"username": "...", "password": "..."}`
- 성공 `200 {"access_token": "<JWT>", "token_type": "bearer", "role": "viewer" | "manager"}`
- 실패 `401`

### `GET /api/v1/devices` — viewer · manager
```json
[{"device_id": "co2-01", "type": "co2", "room": "r302", "status": "online", "last_seen": 1795436052379}]
```
- 시뮬레이터의 **모든 기기**가 나와야 한다 (상태 토픽만 온 기기 포함).
- `status` 는 아래 3절 규칙. `last_seen` = 마지막 데이터 수신 시각(서버 기준), 없으면 `null`.

### `GET /api/v1/readings?device_id=&from=&to=&limit=&cursor=` — viewer · manager
```json
{"items": [{"device_id": "co2-01", "seq": 128, "ts": 1795436052379, "received_at": 1795436052391,
            "data": {"co2": 812}}],
 "next": "<cursor 또는 null>"}
```
- `device_id` 필수. `from`·`to` 는 **서버 수신 시각(received_at)** 기준, 생략하면 전체. `limit` 기본 1000, 최대 10000.
- `items` 는 `seq` 오름차순. 더 있으면 `next` 에 커서 문자열, 다음 요청에 `cursor=` 로 넘긴다. 끝이면 `null`.
- `data` 는 시뮬레이터가 보낸 `data` 그대로 (imu 는 `hz`·`fields`·`samples` 포함).
- **같은 `(device_id, seq)` 는 한 번만** 나와야 한다. 4절 검증을 통과한 메시지만 저장한다.
- 채점기는 **마지막 15초**에 받은 메시지는 채점하지 않는다 (저장 반영 시간). 그 앞의 메시지는 모두 조회돼야 한다.

### `GET /api/v1/alerts?from=&to=` — viewer · manager
```json
{"items": [{"alert_id": "a-17", "type": "co2_high", "device_id": "co2-01", "room": "r302",
            "started_at": 1795436100000, "created_at": 1795436101200, "message": "CO2 2731ppm"}]}
```
- `type` 은 5절의 여섯 가지 중 하나. `alert_id` 는 고유. `from`·`to` 는 `created_at` 기준.
- 채점기는 2초마다 이 API 를 불러 **새 `alert_id` 를 처음 본 시각**을 경보 시각으로 쓴다 (`created_at` 을 믿지 않는다).

### `POST /api/v1/devices/{device_id}/commands` — **manager 만** (viewer → 403)
요청 `{"action": "on" | "off"}`, 헤더 `Idempotency-Key: <문자열>` (선택)
```json
{"cmd_id": "c-9f2a", "result": "acked", "state": "on"}
```
- `result`: `acked`(ack 수신) · `state_confirmed`(ack 는 없었지만 데이터 토픽의 `state` 로 확인) · `failed`.
- **15초 안에** 응답한다. ack 를 기다리는 최대 시간과 그 뒤의 처리 방법은 각자 설계한다.
- 같은 `Idempotency-Key` 로 10분 안에 다시 오면 **같은 응답을 돌려주고 MQTT 명령을 다시 보내지 않는다.**
- vent 가 아닌 기기 → 422 (또는 404). 없는 기기 → 404. `action` 이 `on`·`off` 가 아니면 422.

### `GET /api/v1/events?token=<JWT>` — SSE (`text/event-stream`)
```
event: reading
data: {"device_id": "co2-01", "seq": 128, "ts": 1795436052379, "data": {"co2": 812}}

event: alert
data: {...alerts 항목과 같은 형식...}

event: status
data: {"device_id": "co2-01", "status": "stale"}
```
- `reading` 은 imu 를 제외한 기기의 새 데이터마다 보낸다 (imu 는 줄이거나 생략해도 됨).
- 채점기는 `reading` 의 `ts` 와 도착 시각의 차이(지연)를 잰다. 연결 후 **15초 안에** 첫 `reading` 이 와야 한다.

## 3. 기기 상태 규칙

| 상태 | 조건 |
|---|---|
| `offline` | 그 기기의 마지막 `…/status` 메시지가 `offline` |
| `stale` | `online` 인데 마지막 데이터 수신 후 **30초** 넘게 데이터 없음 (`vent` 는 **90초**, `door` 는 stale 판정 안 함) |
| `online` | 그 밖 (status 를 아직 못 받았어도 데이터가 오고 있으면 online) |

채점기는 **정상 기기를 stale · offline 으로 잘못 표시하는지**도 본다.
기준: 고장이 끝난 뒤 20초의 유예를 주고, 3초 넘게 잘못 표시된 기기를 **1건**으로 센다 (같은 기기가 계속 틀려도 1건).
0건 만점, 1건 절반, 2건 이상 0점.

## 4. 입력 검증 — 저장·경보 전에 거른다

다음 중 하나라도 해당하면 **잘못된 메시지**로 버리고(개수는 세어 두면 좋다) 저장·경보·SSE 에 쓰지 않는다.

- JSON 이 아님, `device_id`·`seq`·`ts`·`data` 중 하나라도 없음, `data` 의 값이 숫자여야 할 곳에 문자열
- 값의 범위를 벗어남:

| 필드 | 허용 범위 |
|---|---|
| `temperature` | -40 ~ 85 |
| `humidity` | 0 ~ 100 |
| `co2` | 0 ~ 10000 |
| `count` · `capacity` | 0 ~ 1000 |
| `power` | 0 ~ 20000 |
| `energy` | 0 이상 |
| imu `samples` | 1개 이상, 각 샘플 6개 값. 가속도(앞 3개) 절댓값 16 이하, 자이로(뒤 3개) 절댓값 2000 이하 |
| door `state` | `open` · `closed` |
| vent `state` | `on` · `off` |

## 5. 경보 규칙

채점기는 자기가 따로 구독한 원본 데이터에 아래 규칙을 똑같이 적용해 **정답 구간**을 만들고, 여러분의 경보와 대조한다.

| `type` | 기기 | 조건 (한 메시지 기준) |
|---|---|---|
| `overheat` | env | `temperature` ≥ 30.0 |
| `co2_high` | co2 | `co2` ≥ 2500 |
| `overload` | power | `power` ≥ 600 |
| `over_capacity` | occupancy | `count` > `capacity` |
| `door_open_long` | door | `open` 이 된 뒤 `closed` 없이 120초 경과 (그 시점이 구간 시작) |
| `fall` | imu | 한 메시지의 샘플 중 하나라도 √(ax²+ay²+az²) ≥ 2.5 |

- **구간(episode):** 조건을 만족하는 메시지가 오면 시작, 만족하지 않는 메시지가 오면 끝. 끝난 뒤 **60초 안에** 다시 만족하면 같은 구간으로 이어 본다.
- **경보 1건 = 구간 1개.** 구간이 시작되고 **30초 안에** 경보가 `/alerts` 에 보여야 제때 낸 것이다 (채점기 폴링 2초를 더해 32초까지 인정).
- 같은 구간에서 경보를 또 내면 **중복**으로 감점된다. 어느 구간에도 들어가지 않는 경보는 **오경보**다.
- 시각은 서버가 메시지를 받은 시각 기준이다 (기기 `ts` 는 어긋날 수 있다).

## 6. 채점 영역 요약 (자동 70점)

| 영역 | 배점 | 무엇을 재나 |
|---|---|---|
| 기동 · 계약 | 15 | health 1 · 로그인 2 · 잘못된 비밀번호 401 1 · 토큰 없음·위조 401 1 · 기기 목록 3 · readings 형식·페이지 3 · alerts 1 · SSE 수신 1 · SSE 지연 2 |
| 데이터 품질 | 15 | 저장률 6 · 값 일치 3 · 중복 제거 3 · 깨진 메시지 처리 3 |
| 상태 판정 | 10 | offline 감지 3 · 복귀 2 · stale 감지 3 · 오표시 없음 2 |
| 경보 | 15 | 정답 구간 대비 F1 12 · 중복 억제 3 |
| 제어 | 10 | viewer 403 1 · 정상 명령 3 · 멱등성 2 · ack 없음 2 · 늦은 ack 2 |
| 보안 | 5 | 공개 포트 1개 2 · 익명 MQTT 거부 2 · `.env` 미포함 1 |

**저장률은 기기별 저장률의 평균**이다 (전체 합계 비율이 아니다). imu 처럼 많이 보내는 기기 하나가 점수를 가리지 않도록 하고,
반대로 `door` 처럼 드물게 보내는 기기를 놓치면 그만큼 드러난다. 99% 만점, 85% 에서 0점.

공개 채점기로 제출 전에 스스로 확인한다:

```bash
python3 grader/grade.py /path/to/my-repo --sim-dir sensor-sim
```

실제 채점은 같은 항목을 **고장 순서 · 시점 · 강도 · 대상 기기를 바꾸고 비밀 값을 넣어** 돌린다. 공개판 점수와 다를 수 있다.
