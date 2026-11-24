"""실행:  python -m simulator --config sim.yaml   (브로커 없이 확인: --dry-run)"""
import argparse
import hashlib
import logging
import signal
import sys
import threading

from . import __version__
from .config import SENSOR_TYPES, load
from .control import serve
from .device import Clock, Device
from .sensors import MODELS, ImuSensor, Room
from .truth import anomaly_schedule

ROOM_POOL = ["r301", "r302", "r405", "r512", "lab1", "lab2", "lab3", "lib1", "hall", "studio",
             "r101", "r102", "r203", "r204", "r610", "r611", "lab4", "lab5", "lounge", "gym"]


def build(cfg, dry_run):
    seed = cfg.seed
    order = sorted(ROOM_POOL, key=lambda r: hashlib.sha256(f"{seed}{r}".encode()).hexdigest())
    rooms = [Room(name, seed) for name in order[: cfg.rooms]]
    clock = Clock(cfg.time_scale, cfg.sim_epoch)
    tag = cfg.prefix.split("/")[-1]
    devices, slot = [], seed % len(rooms)
    co2_rooms = []
    for kind in SENSOR_TYPES:
        for i in range(1, cfg.sensors.get(kind, 0) + 1):
            if kind == "vent" and co2_rooms:
                room = co2_rooms[(i - 1) % len(co2_rooms)]        # 환기장치는 CO2 센서가 있는 방부터 배치
            else:
                room = rooms[slot % len(rooms)]
                slot += 1
            if kind == "co2" and room not in co2_rooms:
                co2_rooms.append(room)
            did = f"{kind}-{i:02d}"
            if kind == "imu":
                model = ImuSensor(did, room, seed, hz=cfg.imu_hz, batch=cfg.imu_batch)
                model.r0 = clock.r0
            else:
                model = MODELS[kind](did, room, seed)
            devices.append(Device(cfg, clock, kind, i, room, model, cfg.prefix, tag, dry_run))
    return rooms, devices, clock


def main(argv=None):
    ap = argparse.ArgumentParser(prog="simulator", description="INU 네트워크구조및설계 혼합 센서 시뮬레이터")
    ap.add_argument("--config", default="/config/sim.yaml")
    ap.add_argument("--dry-run", action="store_true", help="브로커 없이 발행 내용을 화면에 출력")
    ap.add_argument("--list", action="store_true", help="기기·토픽 목록만 출력하고 종료")
    ap.add_argument("--duration", type=float, default=0, help="N초 후 종료 (테스트용)")
    ap.add_argument("--truth", type=float, metavar="MIN",
                    help="(평가자용) 시작 후 MIN 분 동안 생기는 이상 상황 일정을 출력하고 종료")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    log = logging.getLogger("sim")
    try:
        cfg = load(args.config)
    except ValueError as e:
        log.error("설정 오류: %s", e)
        return 2

    rooms, devices, clock = build(cfg, args.dry_run)
    print(f"# sensor-sim {__version__}  student={cfg.student_id}  prefix={cfg.prefix}  "
          f"broker={'(dry-run)' if args.dry_run else f'{cfg.broker.host}:{cfg.broker.port}'}  "
          f"time_scale={cfg.time_scale}  sim_start=월 {cfg.sim_start}{'  (채점 모드)' if cfg.grade_salt else ''}",
          file=sys.stderr)
    print("# rooms: " + ", ".join(f"{r.name}(정원 {r.capacity})" for r in rooms), file=sys.stderr)
    for d in devices:
        print(f"#   {d.device_id:13s} qos={d.qos}  {d.topic}", file=sys.stderr)
        if d.cmd_topic:
            print(f"#   {'':13s} cmd → {d.cmd_topic}   ack ← {d.ack_topic}", file=sys.stderr)
    if args.truth and cfg.grade_mode:
        log.error("채점 모드(GRADE_SALT)에서는 --truth 를 쓸 수 없습니다.")
        return 2
    if args.truth:
        for w in anomaly_schedule(devices, clock, clock.r0, clock.r0 + args.truth * 60):
            a, b = w["start_offset_s"], w["end_offset_s"]
            print(f"+{int(a // 60):3d}:{int(a % 60):02d} ~ +{int(b // 60):3d}:{int(b % 60):02d}  "
                  f"{w['device_id']:13s} {w['room']:7s} {w['label']}")
        return 0
    if args.list:
        return 0

    serve(devices, cfg.control_host, cfg.control_port, clock, grade_mode=cfg.grade_mode)
    for d in devices:
        d.start()

    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    done.wait(args.duration or None)
    for d in devices:
        d.stop()
    for d in devices:
        d.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
