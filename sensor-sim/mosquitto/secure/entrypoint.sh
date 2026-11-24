#!/bin/sh
# 브로커가 시작될 때 계정 파일(passwd)을 환경 변수로부터 만든다.
#
# 왜 이렇게 하나:
#   - passwd 파일에는 해시가 들어가지만, 그래도 저장소에 올리지 않는 것이 원칙이다.
#   - 그런데 저장소에 없으면 clone 한 사람(평가자·동료)은 브로커를 띄울 수 없다.
#   → "저장소에는 만드는 방법만 두고, 값은 환경 변수로 주입" 이 양쪽을 동시에 만족한다.
#
# compose 에서 기본값을 주므로 .env 가 없어도 뜬다 (CONTRACT.md 0절: fresh clone 기동).
set -e
F=/mosquitto/data/passwd
: "${SIM_PW:?SIM_PW 환경 변수가 필요합니다 (compose 의 기본값 또는 .env)}"
: "${BRIDGE_PW:?BRIDGE_PW 환경 변수가 필요합니다}"
: "${GRADER_PW:?GRADER_PW 환경 변수가 필요합니다 (grading.json 의 mqtt_grader 와 같아야 함)}"
rm -f "$F"
mosquitto_passwd -c -b "$F" sim    "$SIM_PW"
mosquitto_passwd    -b "$F" bridge "$BRIDGE_PW"
mosquitto_passwd    -b "$F" grader "$GRADER_PW"
chown mosquitto:mosquitto "$F" 2>/dev/null || true
chmod 0600 "$F"
echo "passwd 생성 완료: sim, bridge, grader" >&2
exec mosquitto -c /mosquitto/config/mosquitto.conf
