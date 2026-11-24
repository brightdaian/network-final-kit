"""센서 데이터 모델.

모든 값은 (학번 시드, 방, 기기, 시뮬레이션 시각)으로 결정된다.
→ 같은 학번이면 같은 패턴이 재현되고, 학번이 다르면 패턴·이상치 시점이 달라진다.
같은 방의 센서끼리는 재실 인원(occupancy)을 공유해 CO2·온도가 함께 움직인다.
같은 방의 환기장치(vent)를 켜면 그 방의 CO2·온도가 서서히 내려간다 (제어 → 결과 확인).
"""
import hashlib
import math
import random
import threading

CLASS_MIN = 75          # 수업 75분
BREAK_MIN = 15          # 쉬는 시간 15분
CYCLE_S = (CLASS_MIN + BREAK_MIN) * 60
VENT_TAU = 600.0        # 환기 효과 시정수(시뮬레이션 초): 켠 뒤 약 10분이면 63% 반영
VENT_FACTOR_ON = 0.35   # 환기 중 인원에 의한 CO2 증가분이 35% 로 줄어듦


def _h(*parts) -> float:
    """결정적 0~1 난수 (같은 입력 → 같은 값)."""
    d = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(d[:12], 16) / float(16 ** 12)


class Room:
    def __init__(self, name: str, seed: int):
        self.name = name
        self.seed = seed
        self.capacity = 20 + int(_h(seed, name, "cap") * 40)        # 20~60명
        self.phase = _h(seed, name, "phase") * CYCLE_S
        self.base_temp = 21.0 + _h(seed, name, "temp") * 3.0
        # 환기 상태: vent 기기가 바꾼다. (변경 시각, 그 순간의 계수, 목표 계수)
        self._vlock = threading.Lock()
        self._v = (None, 1.0, 1.0)

    # ---------- 재실 ----------
    def occupancy(self, t: float) -> int:
        """시뮬레이션 시각 t(초)의 재실 인원."""
        x = (t + self.phase) % CYCLE_S
        block = int((t + self.phase) // CYCLE_S)
        if _h(self.seed, self.name, "empty", block) < 0.25:          # 25%는 빈 강의실
            return 0
        if x < CLASS_MIN * 60:
            fill = 0.6 + 0.4 * _h(self.seed, self.name, "fill", block)
            ramp = min(1.0, x / 300.0)                                 # 입실 5분
            return int(self.capacity * fill * ramp)
        return int(self.capacity * 0.1 * _h(self.seed, self.name, "brk", block))

    def occupancy_smoothed(self, t: float, window: float = 900.0) -> float:
        """CO2·온도는 인원 변화를 늦게 따라간다 (15분 평균)."""
        n = 6
        return sum(self.occupancy(t - window * i / n) for i in range(n)) / n

    # ---------- 환기 ----------
    def vent_factor(self, t: float) -> float:
        """1.0 = 환기 꺼짐(효과 없음) … VENT_FACTOR_ON = 환기 효과 최대. 지수적으로 천천히 변한다."""
        with self._vlock:
            t_c, f_c, target = self._v
        if t_c is None:
            return target
        return target + (f_c - target) * math.exp(-max(t - t_c, 0.0) / VENT_TAU)

    def set_vent(self, on: bool, t: float):
        f_now = self.vent_factor(t)
        with self._vlock:
            self._v = (t, f_now, VENT_FACTOR_ON if on else 1.0)


# ---------- 이상치(실제 이상 상황) ----------
# (이름, 주기, 길이, 발생 확률, 시계) — 시계 "sim" 은 시뮬레이션 시각, "real" 은 시작 후 경과한 실제 초 기준
ANOMALIES = {
    "env":   [("overheat", 6 * 3600, 1200, 0.5, "sim")],     # 난방기 이상: +8°C, 20분
    "co2":   [("vent-fail", 4 * 3600, 1800, 0.5, "sim")],    # 환기 계통 고장: +900ppm, 30분
    "door":  [("left-open", 3 * 3600, 900, 0.4, "sim")],     # 이 구간에 열리면 오래 열려 있음
    "power": [("surge", 5 * 3600, 600, 0.5, "sim")],         # 과부하: ×2.5, 10분
    "imu":   [("fall", 600, 2.0, 0.6, "real")],              # 낙상 충격: 실제 시간 10분 주기, 2초
}


def anomaly_active(seed: int, device_id: str, kind: str, t: float, period: float, length: float, rate: float) -> bool:
    """학번·기기별로 정해진 시점에 이상치 구간이 생긴다."""
    slot = int(t // period)
    if _h(seed, device_id, kind, slot) >= rate:
        return False
    start = _h(seed, device_id, kind, "start", slot) * (period - length)
    return start <= (t % period) < start + length


def anomaly_windows(seed: int, device_id: str, kind: str, period: float, length: float, rate: float,
                    t0: float, t1: float):
    """[t0, t1] 과 겹치는 이상치 구간 목록 [(시작, 끝)] — 채점용 정답(ground truth)."""
    out = []
    for slot in range(int(t0 // period), int(t1 // period) + 1):
        if _h(seed, device_id, kind, slot) >= rate:
            continue
        s = slot * period + _h(seed, device_id, kind, "start", slot) * (period - length)
        if s + length > t0 and s < t1:
            out.append((s, s + length))
    return out


class SensorModel:
    kind = "base"
    unit = {}

    def __init__(self, device_id: str, room: Room, seed: int):
        self.device_id = device_id
        self.room = room
        self.seed = seed
        self.rng = random.Random(f"{seed}-{device_id}")

    def _anomaly(self, name: str, t: float) -> bool:
        for n, period, length, rate, _ in ANOMALIES.get(self.kind, []):
            if n == name:
                return anomaly_active(self.seed, self.device_id, n, t, period, length, rate)
        return False

    def sample(self, t: float) -> dict:
        raise NotImplementedError


class EnvSensor(SensorModel):
    kind = "env"
    unit = {"temperature": "°C", "humidity": "%"}

    def sample(self, t):
        day = math.sin(2 * math.pi * ((t / 86400.0) - 0.3))           # 하루 주기
        occ = self.room.occupancy_smoothed(t)
        vent_cool = 1.2 * (1.0 - self.room.vent_factor(t)) / (1.0 - VENT_FACTOR_ON)
        temp = self.room.base_temp + 1.2 * day + 0.04 * occ - vent_cool + self.rng.gauss(0, 0.15)
        if self._anomaly("overheat", t):
            temp += 8.0                                                  # 난방기 이상
        hum = 45 - 8 * day + 0.1 * occ + self.rng.gauss(0, 1.0)
        return {"temperature": round(temp, 2), "humidity": round(min(max(hum, 15), 95), 1)}


class Co2Sensor(SensorModel):
    kind = "co2"
    unit = {"co2": "ppm"}

    def sample(self, t):
        occ = self.room.occupancy_smoothed(t)
        ppm = 420 + 28 * occ * self.room.vent_factor(t) + self.rng.gauss(0, 15)
        if self._anomaly("vent-fail", t):
            ppm += 900                                                   # 환기 계통 고장 (환기장치를 켜도 남음)
        return {"co2": int(max(ppm, 380))}


class OccupancySensor(SensorModel):
    kind = "occupancy"
    unit = {"count": "people"}

    def sample(self, t):
        n = self.room.occupancy(t)
        n = max(0, n + int(self.rng.gauss(0, 1.0))) if n else 0
        return {"count": n, "capacity": self.room.capacity}


class DoorSensor(SensorModel):
    """이벤트형: 열림/닫힘이 생길 때만 전송."""
    kind = "door"
    unit = {}

    def __init__(self, *a):
        super().__init__(*a)
        self.state = "closed"

    def next_gap(self, t: float) -> float:
        """다음 이벤트까지 시뮬레이션 시간(초). 쉬는 시간 직후엔 자주, 수업 중엔 드물게."""
        busy = self.room.occupancy(t) > 0 and ((t + self.room.phase) % CYCLE_S) < 300
        mean = 30 if busy else 400
        if self.state == "open":
            mean = 8
            if self._anomaly("left-open", t):
                mean = 1200                                              # 문 열어둠
        return self.rng.expovariate(1.0 / mean)

    def sample(self, t):
        self.state = "open" if self.state == "closed" else "closed"
        return {"state": self.state}


class PowerSensor(SensorModel):
    kind = "power"
    unit = {"power": "W", "energy": "Wh"}

    def __init__(self, *a):
        super().__init__(*a)
        self.load_w = 40 + _h(self.seed, self.device_id, "load") * 400   # 장비 정격
        self.energy_wh = 0.0
        self.last_t = None

    def sample(self, t):
        on = self.room.occupancy(t) > 0 or _h(self.seed, self.device_id, "standby") < 0.3
        w = (self.load_w if on else 3.0) * (1 + self.rng.gauss(0, 0.03))
        if self._anomaly("surge", t):
            w *= 2.5                                                     # 과부하
        if self.last_t is not None:
            self.energy_wh += w * max(t - self.last_t, 0) / 3600.0
        self.last_t = t
        return {"power": round(w, 1), "energy": round(self.energy_wh, 1)}


class ImuSensor(SensorModel):
    """고빈도 IMU: 가속도(g) 3축 + 자이로(deg/s) 3축. batch 개씩 묶어 전송."""
    kind = "imu"
    unit = {"acc": "g", "gyro": "deg/s"}
    fields = ["ax", "ay", "az", "gx", "gy", "gz"]

    def __init__(self, *a, hz: int = 50, batch: int = 10):
        super().__init__(*a)
        self.hz = hz
        self.batch = batch
        self.step_hz = 1.6 + _h(self.seed, self.device_id, "gait") * 0.6
        self.r0 = 0.0            # 시뮬레이터 시작 실제 시각 (낙상 시점을 '시작 후 몇 초'로 재현하기 위해)

    def one(self, t_sim: float, r: float, force_fall: bool = False):
        """t_sim: 활동 여부(수업 중 이동)를 정하는 시뮬레이션 시각, r: 파형을 만드는 실제 시각."""
        walking = self.room.occupancy(t_sim) > 0 and math.sin(t_sim / 600.0) > -0.2
        a = 0.25 if walking else 0.02
        ph = 2 * math.pi * self.step_hz * r
        s = [a * math.sin(ph) + self.rng.gauss(0, 0.02),
             0.5 * a * math.sin(ph / 2) + self.rng.gauss(0, 0.02),
             1.0 + a * abs(math.sin(ph)) + self.rng.gauss(0, 0.02),
             40 * a * math.cos(ph) + self.rng.gauss(0, 1.5),
             15 * a * math.sin(ph) + self.rng.gauss(0, 1.5),
             5 * a * math.cos(ph / 2) + self.rng.gauss(0, 1.5)]
        # 낙상 충격: 실제 시간 기준 약 10분 주기 중 60% 확률로 2초간
        if force_fall or self._anomaly("fall", r - self.r0):
            s = [s[0] * 6 + self.rng.gauss(0, 1.5), s[1] * 6 + self.rng.gauss(0, 1.5),
                 s[2] + abs(self.rng.gauss(2.2, 0.5)) + 0.3, s[3] * 6 + self.rng.gauss(0, 60),
                 s[4] * 6 + self.rng.gauss(0, 60), s[5] * 6 + self.rng.gauss(0, 60)]
        return [round(x, 3) for x in s]

    def sample(self, t_sim: float, r: float, force_fall: bool = False):
        """batch 개 샘플, 실제 시간 간격 1/hz."""
        return {"hz": self.hz, "fields": self.fields,
                "samples": [self.one(t_sim, r - (self.batch - 1 - i) / self.hz, force_fall) for i in range(self.batch)]}


class VentActuator(SensorModel):
    """환기장치(제어 대상). cmd 토픽으로 on/off 명령을 받고, ack 토픽으로 결과를, 데이터 토픽으로 상태를 보낸다.

    명령 payload:  {"cmd_id": "<고유 ID>", "action": "on" | "off"}
    ack payload:   {"cmd_id", "result": "ok" | "rejected", "reason"?, "duplicate"?, "state", "ts"}
    같은 cmd_id 가 다시 오면(QoS 1 재전송 등) 상태를 다시 바꾸지 않고 duplicate=true 로 ack 한다.
    """
    kind = "vent"
    unit = {"power": "W"}
    RATED_W = 85.0

    def __init__(self, *a):
        super().__init__(*a)
        self.state = "off"
        self.last_cmd = None
        self.seen = []            # 최근 cmd_id (중복 판정용, 최대 64개)

    def apply(self, action: str, cmd_id: str, t: float) -> tuple[str, bool]:
        """명령 적용 → (결과 상태, 중복 여부)."""
        if cmd_id in self.seen:
            return self.state, True
        self.seen = (self.seen + [cmd_id])[-64:]
        self.last_cmd = cmd_id
        if action != self.state:
            self.state = action
            self.room.set_vent(action == "on", t)
        return self.state, False

    def sample(self, t):
        w = self.RATED_W * (1 + self.rng.gauss(0, 0.02)) if self.state == "on" else 0.8
        return {"state": self.state, "power": round(w, 1), "last_cmd": self.last_cmd}


MODELS = {"env": EnvSensor, "co2": Co2Sensor, "occupancy": OccupancySensor,
          "door": DoorSensor, "power": PowerSensor, "imu": ImuSensor, "vent": VentActuator}
