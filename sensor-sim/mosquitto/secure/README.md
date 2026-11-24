# 인증 · ACL 브로커로 바꾸기

개발용 브로커(`mosquitto/mosquitto.conf`)는 **익명 접속을 허용**한다. 제출본은 이 폴더의 설정으로 바꾼다.
계정 파일(`passwd`)은 **저장소에 두지 않고 컨테이너가 시작할 때 만든다** — `entrypoint.sh` 가 그 일을 한다.

## 1) ACL 파일 만들기

```bash
cp mosquitto/secure/acl.example mosquitto/secure/acl
# 파일 안의 inu/s7a68 을 전부 "내 prefix" 로 바꾼다 (시뮬레이터 첫 줄에 출력됨)
```

## 2) compose 에 붙이기

```yaml
  mosquitto:
    image: eclipse-mosquitto:2
    # \r 을 떼고 실행 — Windows 에서 CRLF 로 체크아웃돼도 동작한다
    entrypoint: ["sh", "-c", "tr -d '\\r' < /mosquitto/config/entrypoint.sh > /tmp/entry.sh && exec sh /tmp/entry.sh"]
    environment:                       # ${VAR:-기본값} → .env 가 없어도 뜬다
      SIM_PW:    "${SIM_PW:-sim-dev-pass}"
      BRIDGE_PW: "${BRIDGE_PW:-bridge-dev-pass}"
      GRADER_PW: "${GRADER_PW:-grader-pass}"     # grading.json 의 mqtt_grader 와 같아야 한다
    volumes:
      - ./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - ./mosquitto/acl:/mosquitto/config/acl:ro
      - ./mosquitto/entrypoint.sh:/mosquitto/config/entrypoint.sh:ro
    networks: [internal]               # 호스트 포트 없음
```

시뮬레이터와 앱에도 같은 값을 준다.

```yaml
  sim:
    environment:
      BROKER_USERNAME: sim
      BROKER_PASSWORD: "${SIM_PW:-sim-dev-pass}"
  app:
    environment:
      MQTT_USER: bridge
      MQTT_PW:   "${BRIDGE_PW:-bridge-dev-pass}"
```

> **왜 기본값을 적나?** 저장소를 새로 clone 한 사람(평가자)이 `docker compose up` 한 번으로 띄울 수 있어야 한다
> (CONTRACT.md 0절). 여기 적는 세 값은 **채점용 테스트 계정**이라 저장소에 남아도 감점하지 않는다.
> 운영 환경이라면 어떻게 달라야 하는지는 설계 문서에 적는다.
> 반대로 **JWT 서명 키는 기본값을 적지 않는다** — 비어 있으면 앱이 시작할 때 무작위로 만들어 쓴다.

## 3) 제대로 바뀌었는지 확인

```bash
docker compose exec mosquitto mosquitto_sub -t '#' -v                              # → 연결 거부 (익명 금지)
docker compose exec mosquitto mosquitto_sub -u bridge -P bridge-dev-pass -t 'inu/#' -v   # → 내 prefix 만 보임
docker compose exec mosquitto mosquitto_pub -u bridge -P bridge-dev-pass -t '<내 prefix>/r302/co2/co2-01' -m x
#   → 브로커 로그에 "Denied PUBLISH" (bridge 는 센서 토픽에 쓸 수 없다)
docker compose exec mosquitto mosquitto_sub -u grader -P grader-pass -t '<내 prefix>/#' -C 3 -v
#   → 3건 보이면 채점기가 원본을 받을 수 있다 (이게 안 되면 데이터 품질 · 경보 0점)
```

주의: ACL 에 걸린 구독 · 발행은 클라이언트 쪽에서 **눈에 띄는 오류 없이** 지나가기 쉽다
(QoS 0 발행은 거부돼도 알 방법이 없고, SUBACK 반환 코드를 확인하지 않는 코드가 많다).
"구독했는데 메시지가 안 온다" → 브로커 로그(`log_type subscribe`, `Denied …`)와 ACL 부터 확인.
