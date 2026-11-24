# CHANGELOG

## v1.3.0 — 채점 공정성 · 재현성

- `topic_prefix` 는 `<앞칸>/<뒷칸>` 두 칸만 허용 (칸 수를 바꾸면 실행 거부). 채점기·ACL 와일드카드와의 불일치로
  데이터 품질·경보가 통째로 0점이 되던 함정 제거.
- `control.host` 를 설정 파일에서 바꿀 수 없게 고정. 채점 모드(`GRADE_SALT`)에서는 `simctl truth` · `--truth` 를 막는다
  (정답 이상치 일정을 앱이 읽어 가는 역이용 차단).
- `malformed` 고장은 **모든 기기 종류에서 반드시** 계약 4절 검증에 걸리는 메시지를 만든다 (door·vent 포함).
- `spike` 고장은 계약 5절 조건을 **항상** 넘긴다 (env 온도 32°C 이상 보장, imu 낙상 충격 강화, 범위 검증은 통과).
- 브로커 계정 파일을 컨테이너 시작 시 생성하는 `mosquitto/secure/entrypoint.sh` 추가.

## v1.2.0 — 자동 채점 지원

- `GRADE_SALT` 환경 변수: 채점 때 평가자가 넣는 비밀 값. 토픽 prefix 는 그대로 두고 **방 이름 · 정원 · 기기 배치 · 이상치 시점**을 바꾼다
  → 시뮬레이터 코드로 이상치 시점을 미리 계산하거나 방 이름을 하드코딩하면 채점에서 틀린다.
- `spike` 강화: CO2 +2500ppm, 전력 최소 900W → 계약(CONTRACT.md)의 경보 조건을 항상 넘도록.
- `simctl list --json`, `simctl stats --json` (채점기용).
- 채점 때는 이 폴더를 평가자 이미지로 교체한다. **코드를 고치지 말 것** (`sim.yaml` 만 수정).

## v1.1.0 — 기말 프로젝트 배포본

### 새 기능
- **환기장치 `vent` (제어 기기)**: `…/vent/<id>/cmd` 로 on/off 명령을 받고 `…/ack` 로 결과를 보낸다.
  켜면 같은 방의 CO2 · 온도가 서서히 내려간다 (폐루프 제어 · 명령 멱등성 · 인가 설계용). CO2 센서가 있는 방부터 배치.
- 고장 모드 2종 추가 (vent 전용): `no-ack`(적용은 하되 ack 없음), `slow-ack -p 초`(ack 지연).
- `simctl stats` / `list` 에 기기별 **발행 수(`sent`)** · 초당 발행률 → 수신 측 손실률 계산.
- `simctl cmd <vent> on|off | --raw JSON` : 브리지 없이 명령 테스트.
- (평가자용) `simctl scenario` 계열 명령. 채점 모드에서는 정답 조회 기능이 비활성화된다.
- (평가자용) `simctl scenario <yaml>` : 시간표대로 고장을 차례로 주입 (모든 학생에게 같은 점검).
- `mosquitto/secure/` : 인증 · ACL 브로커 설정 예시와 안내.

### 수정
- **재현성**: 가상 시계가 실행 시각이 아니라 고정된 가상 날짜(월요일 `sim_start`, 기본 09:00)에서 시작 →
  같은 학번 · 같은 설정이면 언제 실행해도 같은 패턴이 처음부터 나온다. 낙상 시점도 "시작 후 경과 시간" 기준.
- `simctl list` 의 연결 표시가 실제 브로커 연결 상태(`is_connected`)를 반영.
- Windows/Dropbox 에서 복사 시 `simctl` 실행 권한 · CRLF 문제로 `docker compose exec sim simctl` 이 실패하던 문제 → Dockerfile 에서 보정, `.gitattributes` 추가.
- `sent` 카운터, 고장 대상이 맞지 않으면(vent 전용 모드를 센서에) 명확한 오류.

### 호환성
- 데이터 메시지 형식은 v1.0 과 동일. 이상치 발생 시점은 v1.0 과 달라짐 (내부 키 `vent` → `vent-fail`, 시계 기준 변경).
