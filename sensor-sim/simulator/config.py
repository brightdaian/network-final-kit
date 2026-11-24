"""설정 로딩: YAML 파일 + 환경 변수 덮어쓰기."""
import hashlib
import os
import re
from dataclasses import dataclass, field

import yaml

SENSOR_TYPES = ("env", "co2", "occupancy", "door", "power", "imu", "vent")

# 종류별 기본 전송 주기(초, 시뮬레이터 실시간 기준)와 기본 QoS
DEFAULTS = {
    "env": {"interval": 5.0, "qos": 0},
    "co2": {"interval": 10.0, "qos": 0},
    "occupancy": {"interval": 10.0, "qos": 0},
    "door": {"interval": None, "qos": 1},      # 이벤트 발생 시에만 전송
    "power": {"interval": 2.0, "qos": 0},
    "imu": {"interval": None, "qos": 0},       # hz / batch 로 결정
    "vent": {"interval": 30.0, "qos": 1},      # 환기장치(제어 대상): 상태 보고 주기, 변경 시 즉시 보고
}


@dataclass
class BrokerConfig:
    host: str = "mosquitto"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_file: str | None = None
    keepalive: int = 30


@dataclass
class Config:
    student_id: str = "000000000"
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    topic_prefix: str = "auto"
    rooms: int = 3
    time_scale: float = 30.0
    sim_start: str = "09:00"                          # 가상 시각의 시작점 (월요일 HH:MM, KST)
    sensors: dict = field(default_factory=dict)       # type -> count
    intervals: dict = field(default_factory=dict)     # type -> seconds
    qos: dict = field(default_factory=dict)           # type -> 0|1|2
    imu_hz: int = 50
    imu_batch: int = 10
    control_host: str = "127.0.0.1"
    control_port: int = 9090
    log_faults: bool = False
    grade_salt: str = ""                              # 채점용 비밀 값: 방 배치 · 정원 · 이상치 시점을 바꾼다 (토픽 prefix 는 그대로)

    @property
    def seed(self) -> int:
        key = self.student_id + (f"|{self.grade_salt}" if self.grade_salt else "")
        return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)

    @property
    def prefix(self) -> str:
        if self.topic_prefix and self.topic_prefix != "auto":
            return self.topic_prefix.strip("/")
        tag = hashlib.sha256(self.student_id.encode()).hexdigest()[:4]
        return f"inu/s{tag}"

    @property
    def grade_mode(self) -> bool:
        """채점 모드 (평가자가 GRADE_SALT 를 넣고 실행) — 정답 조회 기능을 끈다."""
        return bool(self.grade_salt)

    def interval(self, kind: str) -> float | None:
        v = self.intervals.get(kind)
        return float(v) if v is not None else DEFAULTS[kind]["interval"]

    @property
    def sim_epoch(self) -> float:
        """가상 시계의 시작점(Unix 초). 고정된 날짜(2026-12-07 월요일, KST) + sim_start.
        → 같은 학번·같은 설정이면 실행할 때마다 같은 패턴이 처음부터 재현된다."""
        hh, _, mm = self.sim_start.partition(":")
        return 1796569200.0 + int(hh) * 3600 + int(mm) * 60   # 1796569200 = 2026-12-07 00:00 KST

    def qos_for(self, kind: str) -> int:
        return int(self.qos.get(kind, DEFAULTS[kind]["qos"]))


def _parse_sensor_spec(spec: str) -> dict:
    """'env:3,co2:2,imu:1' -> {'env':3,'co2':2,'imu':1}"""
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition(":")
        out[k.strip()] = int(v or 1)
    return out


def load(path: str | None) -> Config:
    raw = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    cfg = Config()
    cfg.student_id = str(raw.get("student_id", cfg.student_id))
    b = raw.get("broker", {}) or {}
    cfg.broker = BrokerConfig(**{k: v for k, v in b.items() if k in BrokerConfig.__dataclass_fields__})
    cfg.topic_prefix = raw.get("topic_prefix", cfg.topic_prefix)
    cfg.rooms = int(raw.get("rooms", cfg.rooms))
    cfg.time_scale = float(raw.get("time_scale", cfg.time_scale))
    cfg.sim_start = str(raw.get("sim_start", cfg.sim_start))
    cfg.sensors = {k: int(v) for k, v in (raw.get("sensors") or {"env": 2, "co2": 1, "occupancy": 1}).items()}
    cfg.intervals = raw.get("intervals", {}) or {}
    cfg.qos = raw.get("qos", {}) or {}
    imu = raw.get("imu", {}) or {}
    cfg.imu_hz = int(imu.get("hz", cfg.imu_hz))
    cfg.imu_batch = int(imu.get("batch", cfg.imu_batch))
    ctl = raw.get("control", {}) or {}
    # control.host 는 설정 파일에서 바꿀 수 없다 (항상 컨테이너 내부 루프백).
    # 고장 주입·정답 조회 서버가 컨테이너 밖에서 보이면 채점의 의미가 없어진다.
    cfg.control_port = int(ctl.get("port", cfg.control_port))
    cfg.log_faults = bool(ctl.get("log_faults", cfg.log_faults))

    # 환경 변수 덮어쓰기 (docker compose environment: 에서 쓰기 편하도록)
    env = os.environ
    if env.get("STUDENT_ID"):
        cfg.student_id = env["STUDENT_ID"]
    if env.get("BROKER_HOST"):
        cfg.broker.host = env["BROKER_HOST"]
    if env.get("BROKER_PORT"):
        cfg.broker.port = int(env["BROKER_PORT"])
    if env.get("BROKER_USERNAME"):
        cfg.broker.username = env["BROKER_USERNAME"]
    if env.get("BROKER_PASSWORD"):
        cfg.broker.password = env["BROKER_PASSWORD"]
    if env.get("BROKER_TLS"):
        cfg.broker.tls = env["BROKER_TLS"].lower() in ("1", "true", "yes")
    if env.get("TOPIC_PREFIX"):
        cfg.topic_prefix = env["TOPIC_PREFIX"]
    if env.get("SENSORS"):
        cfg.sensors = _parse_sensor_spec(env["SENSORS"])
    if env.get("TIME_SCALE"):
        cfg.time_scale = float(env["TIME_SCALE"])
    if env.get("SIM_START"):
        cfg.sim_start = env["SIM_START"]
    if env.get("GRADE_SALT"):
        cfg.grade_salt = env["GRADE_SALT"]

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    for k, n in cfg.sensors.items():
        if k not in SENSOR_TYPES:
            raise ValueError(f"알 수 없는 센서 종류: {k} (가능: {', '.join(SENSOR_TYPES)})")
        if not 0 <= n <= 50:
            raise ValueError(f"{k} 개수는 0~50 이어야 합니다: {n}")
    if sum(cfg.sensors.values()) == 0:
        raise ValueError("센서를 최소 1개 이상 설정하세요.")
    for k, q in cfg.qos.items():
        if int(q) not in (0, 1, 2):
            raise ValueError(f"{k} 의 qos 는 0, 1, 2 중 하나여야 합니다.")
    if not 1 <= cfg.rooms <= 20:
        raise ValueError("rooms 는 1~20 이어야 합니다.")
    if not 1 <= cfg.imu_hz <= 200 or not 1 <= cfg.imu_batch <= 200:
        raise ValueError("imu.hz / imu.batch 는 1~200 이어야 합니다.")
    if cfg.time_scale <= 0:
        raise ValueError("time_scale 은 0보다 커야 합니다.")
    hh, _, mm = cfg.sim_start.partition(":")
    if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
        raise ValueError(f"sim_start 는 HH:MM 형식이어야 합니다: {cfg.sim_start}")
    parts = cfg.prefix.split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        raise ValueError(
            f"topic_prefix 는 '<앞칸>/<뒷칸>' 두 칸이어야 합니다 (현재: {cfg.prefix!r}).\n"
            "  기본값 auto 를 쓰는 것을 권장합니다 → inu/s<학번해시4자리>.\n"
            "  칸 수를 바꾸면 채점기·ACL 예시의 와일드카드와 맞지 않아 채점이 0점이 됩니다.")
