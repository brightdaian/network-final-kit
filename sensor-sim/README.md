# 혼합 센서 시뮬레이터 (sensor-sim) v1.3

네트워크구조및설계 기말 프로젝트용 센서 시뮬레이터. 여러 종류의 가상 센서가 **MQTT로 데이터를 발행**하고,
환기장치는 **명령을 받아 실제로 방의 공기를 바꾼다**.
여러분은 이 데이터를 **수집 · 저장 · 가공 · 제어해서 사용자가 쓰는 서비스**를 만든다.

- 센서 종류 6가지 + 제어 기기 1가지 중 **원하는 것만 골라** 쓰고, **종류별 개수**도 정한다.
- 학번을 넣으면 방 이름 · 정원 · 데이터 패턴 · 이상치 발생 시점이 **학번마다 다르게** 만들어진다.
- 같은 방의 센서끼리는 연동된다: 수업 중 사람이 차면 CO2 · 온도 · 전력이 함께 오르고, 환기장치를 켜면 CO2 · 온도가 서서히 내려간다.
- 같은 학번 · 같은 설정이면 **실행할 때마다 같은 하루(월요일 09:00~)가 처음부터 재생**된다 → 버그 재현 · 실험 비교가 쉽다.
- **채점 때는 평가자가 비밀 값(`GRADE_SALT`)을 넣어 방 이름 · 정원 · 이상치 시점이 바뀐다.** 토픽 prefix 는 같다.
  방 이름이나 시점을 코드에 적지 말고, 받은 메시지로 판단할 것. 이 폴더의 코드는 고치지 않는다 (`sim.yaml` 만 수정).
- 서비스가 지켜야 할 API · 경보 규칙은 상위 폴더의 `CONTRACT.md` 에 있다.

## 1. 빠른 시작

```bash
cp sim.example.yaml sim.yaml          # 학번 · 센서 종류 · 개수 수정
docker compose -f compose.example.yaml up --build
docker compose -f compose.example.yaml exec mosquitto mosquitto_sub -v -t '#'   # 실제로 무엇이 오가는지 보기
```

브로커 없이 동작만 보고 싶다면 (Python 3.10+, `pip install -r requirements.txt`):

```bash
python -m simulator --config sim.yaml --list       # 기기 · 토픽 목록만
python -m simulator --config sim.yaml --dry-run    # 발행 내용을 화면에 출력
```

## 2. 기기 종류

| 종류 | 데이터 (`data` 필드) | 기본 주기 | 기본 QoS | 서비스 아이디어 |
|---|---|---|---|---|
| `env` | `temperature` °C, `humidity` % | 5초 | 0 | 쾌적도, 과열 경보 |
| `co2` | `co2` ppm | 10초 | 0 | 환기 알림, 공기질 순위 |
| `occupancy` | `count` 명, `capacity` 정원 | 10초 | 0 | 빈 강의실 찾기, 정원 초과 경고 |
| `door` | `state` open/closed | 이벤트 시 | 1 | 출입 기록, 문 열림 방치 알림 |
| `power` | `power` W, `energy` Wh(누적) | 2초 | 0 | 전력 대시보드, 대기전력 낭비 탐지 |
| `imu` | `hz`, `fields`, `samples`(묶음) | hz/batch | 0 | 활동량, 낙상 감지 |
| `vent` ★ | `state` on/off, `power` W, `last_cmd` | 30초 + 변경 즉시 | 1 | 원격 환기 제어, CO2 기반 자동 환기 |

- 주기는 `intervals:`, QoS 는 `qos:` 로 종류별 변경 가능.
- `imu` 는 초당 `hz` 개 샘플을 `batch` 개씩 묶어 보낸다 (기본 50Hz × 10개 = 초당 5메시지). 샘플 배열의 열 순서는 `fields` = `[ax, ay, az, gx, gy, gz]`.
- `vent` 는 **CO2 센서가 있는 방부터** 배치된다. 켜면 그 방 CO2 의 인원 기여분이 약 35% 로, 온도가 약 1.2°C 내려간다 (가상 시간 10분에 63% 반영 — 즉시 바뀌지 않는다).
- `time_scale`(기본 30): 데이터 패턴이 흐르는 속도. 30이면 실제 1초에 30초만큼 진행 → 90분 수업 주기를 3분 만에 볼 수 있다. 전송 주기와 `ts` 는 실제 시간 기준.

## 3. 토픽 규칙

```
<prefix>/<room>/<type>/<device_id>            데이터
<prefix>/<room>/<type>/<device_id>/status     연결 상태: online / offline (QoS 1, retain)
<prefix>/<room>/vent/<device_id>/cmd          명령 (여러분 → 기기, QoS 1 권장)
<prefix>/<room>/vent/<device_id>/ack          명령 결과 (기기 → 여러분, QoS 1)
```

- `prefix` 는 `inu/s<학번해시 4자리>` (실행 시 화면에 출력됨). **`topic_prefix` 는 `auto` 로 둔다** — 직접 지정하더라도 `<앞칸>/<뒷칸>` 두 칸이어야 하고, 칸 수가 다르면 시뮬레이터가 실행을 거부한다 (채점기·ACL 의 와일드카드가 두 칸을 가정한다).
- 예: `inu/s7a68/r302/co2/co2-01`, `inu/s7a68/r302/co2/co2-01/status`, `inu/s7a68/r302/vent/vent-01/cmd`
- 각 기기는 **고유 client_id** 로 따로 접속하며, 비정상 종료 시 브로커가 `offline` 을 대신 발행하도록 **LWT** 가 설정되어 있다.
- 와일드카드 주의: 데이터만 받으려면 `<prefix>/+/+/+`, 상태만 받으려면 `<prefix>/+/+/+/status`. `<prefix>/#` 은 cmd · ack 까지 모두 받는다.

## 4. 메시지 형식 (JSON)

데이터:

```json
{
  "device_id": "co2-01",
  "type": "co2",
  "room": "r302",
  "seq": 128,
  "ts": 1794896061627,
  "unit": {"co2": "ppm"},
  "data": {"co2": 812}
}
```

| 필드 | 의미 | 활용 |
|---|---|---|
| `seq` | 기기별 1부터 증가하는 번호 | 누락 · 중복 감지 |
| `ts` | 발행 시각 (Unix epoch, 밀리초) | 지연 측정, 시간순 정렬 |
| `unit` | `data` 각 값의 단위 | 화면 표시 |

명령과 결과 (환기장치):

```json
// → <prefix>/<room>/vent/vent-01/cmd
{"cmd_id": "a1b2c3", "action": "on"}

// ← <prefix>/<room>/vent/vent-01/ack
{"device_id": "vent-01", "cmd_id": "a1b2c3", "result": "ok", "state": "on", "duplicate": false, "ts": 1794896062001}
```

| 규칙 | 설명 |
|---|---|
| `cmd_id` 필수 | 명령마다 고유한 문자열. 없거나 JSON 이 깨지면 기기는 **아무 응답 없이 무시**한다 |
| `action` | `on` 또는 `off`. 그 외 값은 `result: "rejected"` + `reason` |
| 같은 `cmd_id` 재전송 | 상태를 다시 바꾸지 않고 `duplicate: true` 로 ack (기기 측 멱등 처리) |
| 상태 확인 | ack 와 별개로 데이터 토픽의 `state` 가 바로 갱신된다. ack 가 안 와도 상태로 확인할 수 있다 |

생각해 볼 것: 사용자가 버튼을 두 번 누르면? ack 가 안 오면 언제까지 기다리고, 다시 보내도 되는가? (Week 4 멱등성 · Week 7 QoS)
누가 환기장치를 켤 수 있어야 하는가? (Week 6 인가)

## 5. 설계할 때 생각할 것

시뮬레이터는 "정상적인 센서"만 흉내 내지 않는다. 실제 현장처럼 다음 상황이 생길 수 있으니, 여러분의 서비스가 어떻게 대응할지 설계 문서에 적어 두자.

- 기기가 갑자기 끊기거나, 연결은 살아 있는데 데이터만 안 오는 경우
- 같은 메시지가 두 번 오거나, 순간적으로 메시지가 몰려오는 경우
- 형식이 깨졌거나 범위를 벗어난 값, 시각이 어긋난 값
- 명령을 보냈는데 응답(ack)이 안 오거나 늦게 오는 경우
- 과열 · CO2 급증 · 과부하 · 정원 초과 · 문 열림 방치 · 낙상 같은 실제 이상 상황

`simctl` 명령으로 이런 상황을 직접 만들어 테스트해 볼 수 있다 (장애 시나리오 실험에 활용):

```bash
docker compose exec sim simctl list                       # 기기 · 연결 · 발행 수
docker compose exec sim simctl stats                      # 기기별 발행 수 · 초당 발행률
docker compose exec sim simctl modes                      # 고장 모드 목록
docker compose exec sim simctl fault co2-01 silent -d 60  # 60초 동안 고장
docker compose exec sim simctl fault vent-01 no-ack       # 해제할 때까지 고장
docker compose exec sim simctl clear
docker compose exec sim simctl cmd vent-01 on             # 브리지 없이 명령 테스트 (브로커 경유)
```

`simctl stats` 의 `sent` 와 여러분이 받은(저장한) 메시지 수를 비교하면 **손실률**을 잴 수 있다 (QoS 0 vs 1 실험).

## 6. 브로커 보안

`mosquitto/mosquitto.conf` 는 **익명 접속을 허용하는 개발용**이다. 제출본에서는 `mosquitto/secure/` 의
설정(계정 · ACL · `entrypoint.sh`)을 내 저장소의 `mosquitto/` 로 복사해 고쳐 쓴다 → `mosquitto/secure/README.md`.

계정 파일(`passwd`)은 **저장소에 두지 않고 컨테이너가 시작할 때 만든다.** 그래야 평가자가 clone 한 사본에서도 그대로 뜬다
(CONTRACT.md 0절 — fresh clone 기동).

## 7. 설정 파일 전체

`sim.example.yaml` 의 주석 참고. 환경 변수 `STUDENT_ID`, `BROKER_HOST`, `BROKER_PORT`, `BROKER_USERNAME`, `BROKER_PASSWORD`, `BROKER_TLS`,
`TOPIC_PREFIX`, `SENSORS`(예: `env:3,co2:2,vent:1`), `TIME_SCALE`, `SIM_START` 이 파일보다 우선한다.
`control.host` 는 설정 파일로 바꿀 수 없다 (항상 컨테이너 내부 루프백 — 고장 주입 서버가 밖에서 보이면 안 된다).

## 8. 바뀐 점

`CHANGELOG.md` 참고. 메시지 형식은 v1.0 부터 같다.
