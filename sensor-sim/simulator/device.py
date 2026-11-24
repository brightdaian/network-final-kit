"""기기(센서 1대) 실행 스레드와 고장 모드."""
import json
import logging
import queue
import random
import threading
import time

from .sensors import DoorSensor, ImuSensor, VentActuator
from .transport import StdoutLink, dumps, make_link

log = logging.getLogger("sim")

FAULT_MODES = {
    "drop":       "DISCONNECT 없이 연결을 끊음 → 브로커가 LWT(offline) 발행. 해제 시 재접속",
    "silent":     "연결은 유지(PING 정상)하지만 데이터 발행·명령 처리를 멈춤 → LWT 없음, '조용한 장애'",
    "dup":        "같은 메시지(같은 seq)를 두 번씩 발행 → 중복 처리 확인",
    "burst":      "발행 주기를 param 배 빠르게 (기본 10배) → 폭주·버퍼 처리 확인",
    "malformed":  "깨진 JSON · 필드 누락 · 범위 밖 값 · 문자열 값을 섞어 발행 → 입력 검증 확인",
    "skew":       "ts 를 param 초만큼 어긋나게 (기본 +3600) → 시각 검증·정렬 확인",
    "stuck":      "같은 값을 계속 발행 (센서 고착) → 이상 탐지 확인",
    "topic-typo": "토픽의 방 이름을 대문자로 바꿔 발행 → 대소문자·구독 패턴 확인",
    "clash":      "같은 client_id 로 다른 클라이언트가 접속 → 서로 밀어내며 재접속 반복",
    "spike":      "데이터 수준 이상치를 강제로 발생 (과열·CO2 급증·과부하·정원 초과·문 열림 지속·낙상)",
    "no-ack":     "[vent 전용] 명령은 적용하지만 ack 를 보내지 않음 → 타임아웃·상태 재확인 설계 확인",
    "slow-ack":   "[vent 전용] ack 를 param 초 늦게 보냄 (기본 8초) → 타임아웃 · 재시도 · 멱등성 확인",
}
VENT_ONLY = {"no-ack", "slow-ack"}

# 고장 주입 이력 (simctl truth 에서 채점용으로 보여 줌, 로그에는 남기지 않음)
FAULT_LOG: list[dict] = []
_FAULT_LOG_LOCK = threading.Lock()


class Clock:
    """실제 시각 ↔ 시뮬레이션 시각.  sim = sim0 + (real - r0) × scale

    sim0 는 고정된 가상 날짜(설정 sim_start)라서, 같은 설정이면 언제 실행해도 같은 하루가 처음부터 재생된다.
    """

    def __init__(self, time_scale: float, sim0: float | None = None):
        self.scale = time_scale
        self.r0 = time.time()
        self.sim0 = self.r0 if sim0 is None else sim0

    def sim(self, r: float) -> float:
        return self.sim0 + (r - self.r0) * self.scale

    def real(self, t_sim: float) -> float:
        return self.r0 + (t_sim - self.sim0) / self.scale


class Device(threading.Thread):
    def __init__(self, cfg, clock, kind, index, room, model, prefix, tag, dry_run):
        super().__init__(daemon=True, name=f"{kind}-{index:02d}")
        self.cfg, self.clock, self.kind, self.room, self.model = cfg, clock, kind, room, model
        self.device_id = f"{kind}-{index:02d}"
        self.client_id = f"{tag}-{self.device_id}"
        self.topic = f"{prefix}/{room.name}/{kind}/{self.device_id}"
        self.status_topic = f"{self.topic}/status"
        self.is_actuator = isinstance(model, VentActuator)
        self.cmd_topic = f"{self.topic}/cmd" if self.is_actuator else None
        self.ack_topic = f"{self.topic}/ack" if self.is_actuator else None
        self.qos = cfg.qos_for(kind)
        self.dry_run = dry_run
        self.seq = 0
        self.sent = 0                       # 실제로 발행한 데이터 메시지 수 (dup 포함) — 수신 측 손실률 계산용
        self.t_start = time.time()
        self.faults: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.link = None
        self.intruder = None
        self.last_data = None
        self.cmds: queue.Queue = queue.Queue()
        self.pending_acks: list[tuple[float, dict, float]] = []
        self.rng = random.Random(f"fault-{self.client_id}")
        self.next_due = time.time() + random.Random(self.client_id).uniform(0.2, 2.0)

    # ---------- 고장 제어 ----------
    def set_fault(self, mode: str, duration: float | None, param: float | None) -> bool:
        if mode not in FAULT_MODES:
            raise ValueError(f"알 수 없는 고장 모드: {mode}")
        if mode in VENT_ONLY and not self.is_actuator:
            return False
        now = time.time()
        with self.lock:
            self.faults[mode] = {"until": now + duration if duration else None, "param": param}
        with _FAULT_LOG_LOCK:
            FAULT_LOG.append({"ts": int(now * 1000), "device_id": self.device_id, "mode": mode,
                              "duration": duration, "param": param, "action": "fault"})
        if self.cfg.log_faults:
            log.info("고장 적용 %s → %s (%s초)", mode, self.device_id, duration)
        return True

    def clear(self, mode: str | None = None):
        with self.lock:
            if mode:
                self.faults.pop(mode, None)
            else:
                self.faults.clear()
        with _FAULT_LOG_LOCK:
            FAULT_LOG.append({"ts": int(time.time() * 1000), "device_id": self.device_id, "mode": mode or "*",
                              "action": "clear"})

    def active(self, mode):
        return self.faults.get(mode) is not None

    def param(self, mode, default):
        f = self.faults.get(mode) or {}
        return f.get("param") if f.get("param") is not None else default

    def info(self):
        now = time.time()
        with self.lock:
            fl = {m: (round(v["until"] - now) if v["until"] else None) for m, v in self.faults.items()}
        d = {"device_id": self.device_id, "type": self.kind, "room": self.room.name,
             "topic": self.topic, "qos": self.qos, "seq": self.seq, "sent": self.sent,
             "rate": round(self.sent / max(now - self.t_start, 1e-6), 2),
             "connected": bool(self.link and self.link.connected), "faults": fl}
        if self.is_actuator:
            d["state"] = self.model.state
        return d

    # ---------- 명령 (vent) ----------
    def on_command(self, payload: bytes):
        """MQTT 수신 스레드에서 호출됨 → 큐에 넣고 기기 스레드에서 처리 (스레드 안전)."""
        self.cmds.put(payload)

    def inject_command(self, payload: str):
        """simctl cmd: 실제 모드에서는 브로커를 거쳐 cmd 토픽으로 발행, dry-run 에서는 바로 전달."""
        if not self.is_actuator:
            raise ValueError(f"{self.device_id} 는 명령을 받지 않는 기기입니다 (vent 만 가능).")
        if self.dry_run:
            self.on_command(payload.encode())
        elif self.link and self.link.connected:
            self.link.publish(self.cmd_topic, payload, qos=1)
        else:
            raise ValueError(f"{self.device_id} 가 브로커에 연결되어 있지 않습니다.")

    def _handle_commands(self, now):
        while True:
            try:
                raw = self.cmds.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                silent, no_ack = self.active("silent"), self.active("no-ack")
                delay = float(self.param("slow-ack", 8)) if self.active("slow-ack") else 0.0
                skew = float(self.param("skew", 3600)) if self.active("skew") else 0.0
            if silent:
                continue                                         # 기기가 멈춘 상태: 명령도 무시
            try:
                cmd = json.loads(raw)
                cmd_id = str(cmd["cmd_id"])
            except (ValueError, KeyError, TypeError):
                log.warning("%s: 해석할 수 없는 명령 무시 (cmd_id 없음 또는 JSON 오류)", self.device_id)
                continue
            action = cmd.get("action")
            ack = {"device_id": self.device_id, "cmd_id": cmd_id}
            if action not in ("on", "off"):
                ack.update(result="rejected", reason=f"unknown action: {action!r}", state=self.model.state)
            else:
                state, dup = self.model.apply(action, cmd_id, self.clock.sim(now))
                ack.update(result="ok", state=state, duplicate=dup)
                if not dup:
                    self.next_due = now                          # 상태가 바뀌면 바로 보고
            if not no_ack:
                self.pending_acks.append((now + delay, ack, skew))

    def _flush_acks(self, now):
        if not self.pending_acks or not self.link.alive:
            return
        due = [a for a in self.pending_acks if a[0] <= now]
        self.pending_acks = [a for a in self.pending_acks if a[0] > now]
        for _, ack, skew in due:
            ack["ts"] = int((now + skew) * 1000)                  # ack 를 실제로 보낸 시각
            self.link.publish(self.ack_topic, dumps(ack), qos=1)

    # ---------- 주기 ----------
    def _gap(self, now):
        if isinstance(self.model, ImuSensor):
            base = self.model.batch / self.model.hz
        elif isinstance(self.model, DoorSensor):
            base = max(2.0, self.model.next_gap(self.clock.sim(now)) / self.clock.scale)
        else:
            base = self.cfg.interval(self.kind)
        if self.active("burst"):
            base /= max(float(self.param("burst", 10)), 1.0)
        return base

    def _connect(self, will=True):
        return make_link(self.cfg, self.client_id, self.status_topic, self.dry_run, will=will,
                         cmd_topic=self.cmd_topic if will else None,
                         on_cmd=self.on_command if will else None)

    # ---------- 실행 ----------
    def run(self):
        self.link = self._connect()
        while not self.stop_evt.is_set():
            now = time.time()
            with self.lock:
                for m in [m for m, v in self.faults.items() if v["until"] and v["until"] <= now]:
                    self.faults.pop(m)
                drop, clash = self.active("drop"), self.active("clash")
            if drop and self.link.alive:
                self.link.hard_drop()
            elif not drop and not self.link.alive:
                self.link = self._connect()
            if clash and self.intruder is None:
                self.intruder = self._connect(will=False)
                if isinstance(self.intruder, StdoutLink):
                    print(f"CLASH   같은 client_id 로 두 번째 클라이언트 접속: {self.client_id}", flush=True)
            elif not clash and self.intruder is not None:
                self.intruder.close() if not isinstance(self.intruder, StdoutLink) else None
                self.intruder = None
            if self.is_actuator and self.link.alive:
                self._handle_commands(now)
                self._flush_acks(now)
            if now >= self.next_due:
                if self.link.alive:
                    self._publish_once(now)
                self.next_due = now + self._gap(now)
            self.stop_evt.wait(min(0.05, max(self.next_due - time.time(), 0.0)))
        if self.link and self.link.alive:
            self.link.close()
        if self.intruder is not None and not isinstance(self.intruder, StdoutLink):
            self.intruder.close()

    def _publish_once(self, now):
        with self.lock:
            silent = self.active("silent")
            stuck, dup, spike = self.active("stuck"), self.active("dup"), self.active("spike")
            malformed, typo = self.active("malformed"), self.active("topic-typo")
            skew = float(self.param("skew", 3600)) if self.active("skew") else 0.0
        if silent:
            return
        t_sim = self.clock.sim(now)
        if isinstance(self.model, ImuSensor):
            data = self.model.sample(t_sim, now, force_fall=spike)
        else:
            data = self.model.sample(t_sim)
            if spike:
                data = self._spike(data)
        if stuck and self.last_data is not None:
            data = self.last_data
        self.last_data = data
        self.seq += 1
        msg = {"device_id": self.device_id, "type": self.kind, "room": self.room.name,
               "seq": self.seq, "ts": int((now + skew) * 1000), "unit": self.model.unit, "data": data}
        payload = self._break(msg) if malformed else dumps(msg)
        topic = self.topic.replace(f"/{self.room.name}/", f"/{self.room.name.upper()}/") if typo else self.topic
        self.link.publish(topic, payload, qos=self.qos)
        self.sent += 1
        if dup:
            self.link.publish(topic, payload, qos=self.qos)
            self.sent += 1

    def _spike(self, d):
        """계약 5절의 경보 조건을 **항상** 넘는 값으로 바꾼다 (범위 검증은 통과하도록).

        경계값이 나오면 채점기가 만드는 정답 구간이 들쭉날쭉해지므로, 조건을 넉넉히 넘긴다.
        """
        d = dict(d)
        if self.kind == "env":                                   # overheat: 30.0 이상
            d["temperature"] = round(max(d["temperature"] + 10.0, 32.0) + self.rng.random() * 2, 2)
        elif self.kind == "co2":                                 # co2_high: 2500 이상
            d["co2"] = min(d["co2"] + 2500, 9900)
        elif self.kind == "power":                               # overload: 600 이상
            d["power"] = round(min(max(d["power"] * 3, 900.0), 19000.0), 1)
        elif self.kind == "occupancy":                            # over_capacity: count > capacity
            d["count"] = min(int(d["capacity"] * 1.6) + 1, 1000)
        elif self.kind == "door":                                 # door_open_long: 열린 채 유지
            d["state"] = "open"
            self.model.state = "open"
        return d

    def _break(self, msg):
        """계약 4절 검증에 **반드시** 걸리는 메시지를 만든다 (기기 종류와 무관하게)."""
        choice = self.rng.randrange(4)
        if choice == 0:
            return dumps(msg)[:-7]                                  # 잘린 JSON
        m = dict(msg)
        if choice == 1:
            m.pop("data")                                           # 필수 필드 누락
        elif choice == 2:
            m["data"] = self._out_of_range(msg["data"])             # 범위 · 허용값 밖
        else:
            m["data"] = {k: "N/A" for k in msg["data"]}             # 숫자 자리에 문자열
        return dumps(m)

    def _out_of_range(self, data):
        if self.kind == "imu":
            return {"hz": data["hz"], "fields": data.get("fields"), "samples": []}   # 샘플 0개
        if self.kind == "door":
            return {"state": "ajar"}                                # open · closed 가 아님
        if self.kind == "vent":
            return {**data, "state": "maybe", "power": 99999}       # on · off 가 아님 + 범위 밖
        return {k: (99999 if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                for k, v in data.items()}

    def stop(self):
        self.stop_evt.set()
