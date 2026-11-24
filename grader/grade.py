#!/usr/bin/env python3
"""네트워크구조및설계 기말 프로젝트 자동 채점기 (v1.2)

    python3 grader/grade.py <저장소 경로> --sim-dir <sensor-sim 경로>            # 공개판 (자가 점검)
    python3 grader/grade.py <저장소 경로> --sim-dir <...> --profile hidden --salt <비밀> --seed <정수>

필요한 것: Python 3.10+, Docker (compose v2.20+). 추가 패키지 없음.
약 12분 걸리며, 끝나면 저장소 안 grade-report/ 에 report.md · report.json 을 남긴다.

하는 일
  1. 저장소의 compose 에 덮어쓰기 파일을 붙여 sim 을 --sim-dir 의 시뮬레이터로 교체하고 기동
  2. 채점용 MQTT 계정으로 브로커를 따로 구독해 원본 메시지를 기록 (정답 데이터)
  3. API 계약 확인 → 고장 주입(상태·데이터) → 경보 유발 → 제어 → 보안 확인
  4. 기록한 원본과 학생 API 결과를 대조해 점수 계산
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rules  # noqa: E402

# Windows(cp949) 콘솔 · 파이프로 출력할 때 인코딩 오류로 채점이 멈추지 않도록
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(errors="replace")

VERSION = "1.3.0"
SETTLE_S = 15.0        # 마지막 N초에 받은 메시지는 저장 반영 시간을 주고 채점하지 않는다
FAULT_GRACE_S = 20.0   # 고장이 끝난 뒤 이 시간 동안은 상태 오표시를 세지 않는다
CAPTURE_SLACK_S = 2.0  # 파이프 버퍼링을 감안한 원본 수신 시각 여유


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ─────────────────────────── 실행 환경 ───────────────────────────
class Stack:
    def __init__(self, repo: Path, cfg: dict, sim_dir: Path, salt: str):
        self.repo, self.cfg = repo, cfg
        self.project = "grade_" + re.sub(r"[^a-z0-9]", "", str(cfg["student_id"]).lower())
        self.override = repo / ".grader-override.yaml"
        env = {"STUDENT_ID": str(cfg["student_id"])}
        if salt:
            env["GRADE_SALT"] = salt
        env_lines = "".join(f'      {k}: "{v}"\n' for k, v in env.items())
        self.override.write_text(
            "services:\n  sim:\n    build:\n"
            f"      context: \"{sim_dir.resolve().as_posix()}\"\n"
            "      dockerfile: Dockerfile\n"
            f"    image: sensor-sim-grader:{VERSION}\n"
            f"    environment:\n{env_lines}", encoding="utf-8")
        self.warnings = []
        self.base = ["docker", "compose", "-p", self.project,
                     "-f", cfg.get("compose_file", "compose.yaml"), "-f", self.override.name]

    def run(self, *args, timeout=600, check=False):
        r = subprocess.run(self.base + list(args), cwd=self.repo, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise RuntimeError(f"docker compose {' '.join(args)} 실패:\n{r.stderr[-2000:]}")
        return r

    def exec(self, service, *cmd, timeout=30):
        return self.run("exec", "-T", service, *cmd, timeout=timeout)

    def simctl(self, *args):
        r = self.exec("sim", "simctl", *args)
        if r.returncode != 0:
            raise RuntimeError(f"simctl {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def popen(self, service, *cmd):
        return subprocess.Popen(self.base + ["exec", "-T", service, *cmd], cwd=self.repo,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)

    def up(self):
        self.run("down", "-v", "--remove-orphans", timeout=180)
        log("빌드 · 기동 중 (처음에는 몇 분 걸릴 수 있음)")
        r = self.run("up", "-d", "--build", timeout=1200, check=True)
        self.warnings = sorted(set(re.findall(r'The "([A-Za-z_][A-Za-z0-9_]*)" variable is not set', r.stderr)))
        return r

    def diagnose(self):
        """기동이 안 될 때 리포트에 붙일 근거를 모은다."""
        out = ["$ docker compose ps", self.run("ps", timeout=60).stdout.strip()]
        for svc in ("mosquitto", "sim", "app"):
            r = self.run("logs", "--tail", "25", svc, timeout=60)
            if r.returncode == 0 and (r.stdout.strip() or r.stderr.strip()):
                out += [f"$ docker compose logs --tail 25 {svc}", (r.stdout + r.stderr).strip()[-1500:]]
        return "\n".join(out)

    def down(self):
        self.run("down", "-v", "--remove-orphans", timeout=180)
        try:
            self.override.unlink()
        except OSError:
            pass


# ─────────────────────────── API 클라이언트 ───────────────────────────
class Api:
    def __init__(self, port, accounts):
        self.base = f"http://127.0.0.1:{port}/api/v1"
        self.accounts = accounts
        self.tokens = {}

    def req(self, method, path, body=None, token=None, headers=None, timeout=20):
        h = {"Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if token:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        t0 = time.time()
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        except Exception as e:  # noqa: BLE001 — 연결 실패 등
            return None, None, time.time() - t0, str(e)
        try:
            js = json.loads(raw) if raw else None
        except ValueError:
            js = None
        return code, js, time.time() - t0, None

    def login(self, role):
        acc = self.accounts[role]
        code, js, _, _ = self.req("POST", "/auth/login", {"username": acc["username"], "password": acc["password"]})
        if code == 200 and isinstance(js, dict) and js.get("access_token"):
            self.tokens[role] = js["access_token"]
            return js
        return None

    def get(self, path, role="viewer", **kw):
        code, js, dt, err = self.req("GET", path, token=self.tokens.get(role), **kw)
        if code == 401 and self.login(role):
            code, js, dt, err = self.req("GET", path, token=self.tokens.get(role), **kw)
        return code, js

    def all_readings(self, device_id, frm, to):
        items, cursor, pages = [], None, 0
        while pages < 200:
            q = f"/readings?device_id={device_id}&from={int(frm)}&to={int(to)}&limit=10000"
            if cursor:
                q += f"&cursor={urllib.parse.quote(str(cursor))}"
            code, js = self.get(q, timeout=60)
            if code != 200 or not isinstance(js, dict):
                break
            items += js.get("items") or []
            cursor = js.get("next")
            pages += 1
            if not cursor:
                break
        return items


# ─────────────────────────── 원본 기록 · 경보 수집 ───────────────────────────
class Capture(threading.Thread):
    """채점용 계정으로 브로커를 구독해 모든 메시지를 (호스트 수신 시각, 토픽, payload) 로 기록.

    파이프 버퍼링 때문에 수신 시각이 늦게 찍힐 수 있으므로, 데이터 메시지의 시각은
    payload 의 ts 를 호스트 시계로 옮긴 값(ts + offset)을 쓴다. offset = min(수신 시각 − ts).
    """

    def __init__(self, stack, prefix, acc):
        super().__init__(daemon=True)
        self.data_depth = len(prefix.split("/")) + 2      # prefix/room/type/device 의 '/' 개수
        self.p = stack.popen("mosquitto", "mosquitto_sub", "-h", "127.0.0.1", "-p", "1883",
                             "-u", acc["username"], "-P", acc["password"], "-q", "1",
                             "-t", f"{prefix}/#", "-v")
        self.lines, self.lock = [], threading.Lock()
        self.offset = None

    def run(self):
        for line in self.p.stdout:
            t = time.time()
            parts = line.rstrip("\n").split(" ", 1)
            if len(parts) != 2:
                continue
            topic, payload = parts
            if topic.count("/") == self.data_depth and '"ts"' in payload:
                try:
                    ts = json.loads(payload)["ts"] / 1000
                    if abs(t - ts) < 600:                       # skew 주입 메시지는 제외
                        self.offset = t - ts if self.offset is None else min(self.offset, t - ts)
                except (ValueError, KeyError, TypeError):
                    pass
            with self.lock:
                self.lines.append((t, topic, payload))

    def snapshot(self):
        with self.lock:
            return list(self.lines)

    def event_time(self, t_recv, msg):
        if self.offset is not None and isinstance(msg.get("ts"), (int, float)):
            te = msg["ts"] / 1000 + self.offset
            if abs(te - t_recv) < 60:
                return te
        return t_recv

    def stop(self):
        self.p.terminate()


class AlertPoller(threading.Thread):
    def __init__(self, api):
        super().__init__(daemon=True)
        self.api, self.seen, self.stop_evt, self.errors = api, {}, threading.Event(), 0

    def run(self):
        while not self.stop_evt.wait(2.0):
            code, js = self.api.get("/alerts", role="viewer")
            if code != 200 or not isinstance(js, dict):
                self.errors += 1
                continue
            now = time.time()
            for a in js.get("items") or []:
                if isinstance(a, dict) and a.get("alert_id") is not None and a["alert_id"] not in self.seen:
                    self.seen[a["alert_id"]] = (now, a)


# ─────────────────────────── 채점 ───────────────────────────
class Grader:
    def __init__(self, args):
        self.args = args
        self.repo = Path(args.repo).resolve()
        self.cfg = json.loads((self.repo / "grading.json").read_text(encoding="utf-8"))
        self.hidden = args.profile == "hidden"
        self.rng = random.Random(args.seed if self.hidden else 1)
        self.stack = Stack(self.repo, self.cfg, Path(args.sim_dir), args.salt if self.hidden else "")
        self.api = Api(self.cfg["api_port"], self.cfg["accounts"])
        self.scores, self.notes, self.marks = {}, [], []
        self.injected = []          # (t0, t1, device_ids, mode)

    # ---- 점수 기록 ----
    def mark(self, area, item, got, full, note=""):
        got = round(max(0.0, min(full, got)), 2)
        self.marks.append({"area": area, "item": item, "score": got, "max": full, "note": note})
        self.scores[area] = self.scores.get(area, 0) + got
        log(f"  [{area}] {item}: {got}/{full} {note}")

    # ---- 시뮬레이터 ----
    def fault(self, target, mode, duration=None, param=None):
        args = ["fault", target, mode]
        if duration:
            args += ["-d", str(round(duration, 1))]
        if param is not None:
            args += ["-p", str(param)]
        out = self.stack.simctl(*args)
        devs = out.split(":", 1)[1].strip().split(", ") if ":" in out else []
        self.injected.append((time.time(), time.time() + (duration or 0), devs, mode))
        return devs

    def scale(self, x):
        return x * self.rng.uniform(0.8, 1.3) if self.hidden else x

    def pick(self, devices, kinds, exclude=()):
        c = [d for d in devices if d["type"] in kinds and d["device_id"] not in exclude]
        return self.rng.choice(c) if c else None

    # ---- 전체 흐름 ----
    def run(self):
        st = self.stack
        st.up()
        if st.warnings:
            self.notes.append(
                "compose 가 참조하는 환경 변수에 값이 없다: " + ", ".join(st.warnings)
                + " — .env 없이 clone 한 상태에서는 빈 문자열이 된다."
                " compose 에 ${VAR:-기본값} 으로 기본값을 주어야 한다 (CONTRACT.md 0절).")
            log(self.notes[-1])
        log("health 대기")
        t0 = time.time()
        while time.time() - t0 < 300:
            code, js, _, _ = self.api.req("GET", "/health", timeout=5)
            if code == 200:
                break
            time.sleep(3)
        else:
            raise RuntimeError("5분 안에 /api/v1/health 가 200 을 돌려주지 않음 — 기동 실패")
        for _ in range(30):
            try:
                self.devices = json.loads(st.simctl("list", "--json"))
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        else:
            raise RuntimeError("시뮬레이터 제어(simctl)에 연결할 수 없음 — 서비스 이름이 sim 인지 확인")
        # 데이터 토픽 = <prefix>/<room>/<type>/<device_id> → 뒤 3칸을 떼면 prefix
        self.prefix = "/".join(self.devices[0]["topic"].split("/")[:-3])
        self.pfx_depth = len(self.prefix.split("/"))
        self.by_id = {d["device_id"]: d for d in self.devices}
        log(f"prefix={self.prefix} 기기 {len(self.devices)}대: {', '.join(self.by_id)}")
        kinds = {d["type"] for d in self.devices}
        need = [k for k in ("env", "co2", "vent") if k not in kinds]
        extra = kinds - {"env", "co2", "vent"}
        if need or not extra or len(self.devices) < 8:
            self.notes.append(
                f"기기 구성 최소 조건 미달 (CONTRACT.md 0절): 종류 {sorted(kinds)}, 합계 {len(self.devices)}대"
                + (f" / 없는 필수 종류: {need}" if need else "")
                + ("" if extra else " / env·co2·vent 외 센서 종류가 없음")
                + ("" if len(self.devices) >= 8 else " / 8대 미만"))
            log(self.notes[-1])

        self.cap = Capture(st, self.prefix, self.cfg["mqtt_grader"])
        self.cap.start()
        self.t_start = time.time()
        self.api.login("viewer")
        self.api.login("manager")
        self.alerts = AlertPoller(self.api)
        self.alerts.start()
        time.sleep(5)
        if not self.cap.snapshot():
            time.sleep(10)
        if not self.cap.snapshot():
            err = self.cap.p.stderr.read(500) if self.cap.p.poll() is not None else ""
            self.notes.append(f"채점용 MQTT 계정으로 구독 실패 — 데이터 품질·경보 채점 불가. {err.strip()}")
            log(self.notes[-1])

        log("기준 구간 (40초)")
        self.false_stale = 0
        self._wrong_since, self._wrong_counted = {}, set()
        self.watch_status(40)
        self.test_contract()
        phases = [self.test_status, self.test_data, self.test_alerts]
        if self.hidden:
            self.rng.shuffle(phases)
        for ph in phases:
            ph()
        self.test_control()
        self.test_security()
        log("마무리 대기 (40초)")
        self.watch_status(40)
        self.t_end = time.time()
        self.alerts.stop_evt.set()
        self.cap.stop()
        self.score_data()
        self.score_alerts()
        self.scan_code()

    # ---- 상태 관찰 ----
    def watch_status(self, seconds, exclude=()):
        """정상 기기를 stale · offline 으로 잘못 표시하는지 본다.

        같은 기기가 계속 잘못 표시돼도 1건으로 센다 (구간 단위). 2초 이하의 깜빡임은 무시한다.
        """
        end = time.time() + seconds
        while time.time() < end:
            code, js = self.api.get("/devices")
            now = time.time()
            if code == 200 and isinstance(js, list):
                busy = {d for (a, b, ds, m) in self.injected if b + FAULT_GRACE_S > now for d in ds}
                for d in js:
                    if not isinstance(d, dict):
                        continue
                    did = d.get("device_id")
                    wrong = (d.get("status") in ("stale", "offline") and d.get("type") != "door"
                             and did not in busy and did not in exclude)
                    if wrong:
                        if did not in self._wrong_since:
                            self._wrong_since[did] = now           # 첫 관측 — 아직 세지 않는다
                        elif now - self._wrong_since[did] >= 3 and did not in self._wrong_counted:
                            self._wrong_counted.add(did)
                            self.false_stale += 1
                            self.notes.append(f"정상 기기 오표시: {did} → {d.get('status')}")
                    else:
                        self._wrong_since.pop(did, None)
                        self._wrong_counted.discard(did)
            time.sleep(2)

    def wait_status(self, device_id, want, limit, bad=()):
        t0 = time.time()
        while time.time() - t0 < limit:
            code, js = self.api.get("/devices")
            if code == 200 and isinstance(js, list):
                for d in js:
                    if isinstance(d, dict) and d.get("device_id") == device_id:
                        if d.get("status") == want:
                            return time.time() - t0, None
                        if d.get("status") in bad:
                            return None, d.get("status")
            time.sleep(1)
        return None, None

    # ---- 1. 기동·계약 (15) ----
    def test_contract(self):
        log("▶ 기동 · 계약")
        A = "기동·계약"
        code, js, _, _ = self.api.req("GET", "/health")
        self.mark(A, "health", 1 if code == 200 and isinstance(js, dict) and js.get("status") == "ok" else 0, 1)
        ok = 0
        for role in ("viewer", "manager"):
            r = self.api.login(role)
            ok += 1 if r and r.get("role") == role else 0.5 if r else 0
        self.mark(A, "로그인 viewer·manager", ok, 2)
        bad, _, _, _ = self.api.req("POST", "/auth/login", {"username": self.cfg["accounts"]["viewer"]["username"],
                                                            "password": "wrong-" + uuid.uuid4().hex[:6]})
        self.mark(A, "잘못된 비밀번호 401", 1 if bad == 401 else 0, 1, f"→ {bad}")
        c1, _, _, _ = self.api.req("GET", "/devices")
        c2, _, _, _ = self.api.req("GET", "/devices", token="x.y.z")
        self.mark(A, "토큰 없음·위조 401", (c1 == 401) * 0.5 + (c2 == 401) * 0.5, 1, f"→ {c1}, {c2}")
        code, js = self.api.get("/devices")
        got = {d.get("device_id") for d in js} if code == 200 and isinstance(js, list) else set()
        want = set(self.by_id)
        ratio = len(got & want) / len(want)
        fmt = code == 200 and isinstance(js, list) and all(
            isinstance(d, dict) and {"device_id", "type", "room", "status", "last_seen"} <= set(d) for d in js)
        self.mark(A, "기기 목록", (2 * ratio if fmt else ratio) + (1 if got == want and fmt else 0), 3,
                  f"{len(got & want)}/{len(want)}대" + ("" if fmt else ", 필드 누락"))
        order = {"power": 0, "env": 1, "co2": 2, "occupancy": 3}
        dev = min(self.devices, key=lambda d: order.get(d["type"], 9))
        pts, note = 0, ""
        code, js = self.api.get(f"/readings?device_id={dev['device_id']}&limit=5")
        if code == 200 and isinstance(js, dict) and isinstance(js.get("items"), list):
            items = js["items"]
            fields = all({"device_id", "seq", "ts", "received_at", "data"} <= set(i) for i in items)
            seqs = [i.get("seq") for i in items]
            pts += 1 if fields and items else 0
            pts += 1 if len(items) <= 5 and seqs == sorted(seqs) and len(set(seqs)) == len(seqs) else 0
            if js.get("next"):
                c2, j2 = self.api.get(f"/readings?device_id={dev['device_id']}&limit=5&cursor="
                                      f"{urllib.parse.quote(str(js['next']))}")
                s2 = [i.get("seq") for i in (j2 or {}).get("items", [])] if c2 == 200 and isinstance(j2, dict) else []
                pts += 1 if s2 and seqs and min(s2) > max(seqs) else 0
                note = "" if pts == 3 else "다음 페이지가 이어지지 않음"
            else:
                c3, j3 = self.api.get(f"/readings?device_id={dev['device_id']}&limit=10000")
                n_all = len((j3 or {}).get("items") or []) if c3 == 200 and isinstance(j3, dict) else 0
                if n_all > len(items):
                    note = f"next 없음 (전체 {n_all}개인데 {len(items)}개만 반환)"
                else:
                    pts += 1
        else:
            note = f"→ {code}"
        self.mark(A, "readings 형식·limit·페이지", pts, 3, note)
        code, js = self.api.get("/alerts")
        self.mark(A, "alerts 형식", 1 if code == 200 and isinstance(js, dict) and isinstance(js.get("items"), list) else 0, 1)
        self.test_sse(A)

    def test_sse(self, A):
        tok = self.api.tokens.get("viewer", "")
        url = f"{self.api.base}/events?token={urllib.parse.quote(tok)}"
        lat, got, first = [], 0, None
        t0 = time.time()
        try:
            # 헤더를 일부러 보내지 않는다 — 계약 1절: SSE 는 ?token= 으로도 인증되어야 한다
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=10) as r:
                ev = None
                while time.time() - t0 < 25:
                    line = r.readline()
                    if not line:
                        break
                    line = line.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:") and ev in (None, "reading"):
                        try:
                            d = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if isinstance(d, dict) and _isnum(d.get("ts")) and d.get("device_id") in self.by_id \
                                and self.by_id[d["device_id"]]["type"] != "imu":
                            got += 1
                            first = first or time.time() - t0
                            lat.append(time.time() - self.clock_offset() - d["ts"] / 1000)
                    elif line == "":
                        ev = None
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"SSE 연결 오류: {e}")
        self.mark(A, "SSE reading 수신", 1 if got and first is not None and first <= 15 else 0, 1, f"{got}건")
        p95 = sorted(lat)[int(0.95 * (len(lat) - 1))] if lat else None
        self.mark(A, "SSE 지연 p95", 2 * rules.lin(p95, 2.0, 10.0) if p95 is not None else 0, 2,
                  f"{p95:.2f}초" if p95 is not None else "측정 불가")

    def clock_offset(self):
        return self.cap.offset or 0.0

    # ---- 2. 상태 판정 (10) ----
    def test_status(self):
        log("▶ 상태 판정")
        A = "상태 판정"
        kinds = ("env", "co2", "occupancy", "power")
        d1 = self.pick(self.devices, kinds)
        d2 = self.pick(self.devices, kinds, exclude=(d1["device_id"],) if d1 else ())
        if d1:
            dur = self.scale(45)
            self.fault(d1["device_id"], "drop", dur)
            dt, _ = self.wait_status(d1["device_id"], "offline", 60)
            self.mark(A, "끊김 → offline", 3 * rules.lin(dt, 10, 60), 3, f"{d1['device_id']} {_fmt(dt)}")
            time.sleep(max(0, dur - (dt or 60)))
            dt2, _ = self.wait_status(d1["device_id"], "online", 60)
            self.mark(A, "복귀 → online", 2 * rules.lin(dt2, 15, 60), 2, _fmt(dt2))
        if d2:
            dur = self.scale(100)
            self.fault(d2["device_id"], "silent", dur)
            dt, badst = self.wait_status(d2["device_id"], "stale", 120, bad=("offline",))
            note = f"{d2['device_id']} {_fmt(dt)}" + (" (offline 로 잘못 표시)" if badst else "")
            self.mark(A, "멈춤 → stale", 3 * rules.lin(dt, 40, 120) if not badst else 0, 3, note)
            self.stack.simctl("clear", d2["device_id"])
            self.injected[-1] = (self.injected[-1][0], time.time(), self.injected[-1][2], "silent")
            self.wait_status(d2["device_id"], "online", 30)

    # ---- 3. 데이터 품질 주입 (점수는 끝에서) ----
    def test_data(self):
        log("▶ 데이터 품질 (중복 · 깨진 메시지 · 폭주)")
        kinds = ("env", "co2", "occupancy", "power")
        d = self.pick(self.devices, kinds)
        self.dup_dev = d["device_id"] if d else None
        dup_dur = self.scale(40)
        if d:
            self.fault(d["device_id"], "dup", dup_dur)
        self.dup_window = (time.time(), time.time() + dup_dur)
        time.sleep(dup_dur + 5)
        dur = self.scale(15)
        t0 = time.time()
        self.fault("all", "malformed", dur)
        alive = 0
        checks = 0
        while time.time() - t0 < dur + 5:
            code, _, _, _ = self.api.req("GET", "/health", timeout=5)
            alive += code == 200
            checks += 1
            time.sleep(2)
        self.malformed_alive = alive / max(checks, 1)
        self.t_malformed_end = time.time()
        bk = self.rng.choice([k for k in kinds if any(x["type"] == k for x in self.devices)] or ["env"])
        factor = self.rng.randint(5, 15) if self.hidden else 10
        burst_dur = self.scale(25)
        self.fault(f"type:{bk}", "burst", burst_dur, factor)
        time.sleep(burst_dur + 10)

    # ---- 4. 경보 유발 ----
    def test_alerts(self):
        log("▶ 경보 유발 (spike)")
        plan = []
        for kind in ("env", "co2", "power", "occupancy", "imu", "door"):
            d = self.pick(self.devices, (kind,))
            if d:
                plan.append((d["device_id"], 170 if kind == "door" else self.scale(45)))
        self.rng.shuffle(plan)
        t_last = time.time()
        for did, dur in plan:
            self.fault(did, "spike", dur)
            t_last = max(t_last, time.time() + dur)
            time.sleep(self.rng.uniform(10, 25) if self.hidden else 15)
        self.watch_status(max(0, t_last - time.time()) + 10)

    # ---- 5. 제어 (10) ----
    def test_control(self):
        log("▶ 제어")
        A = "제어"
        vent = self.pick(self.devices, ("vent",))
        if not vent:
            self.mark(A, "vent 기기 없음", 0, 10, "계약 위반: vent 1대 이상 필요")
            return
        vid = vent["device_id"]
        path = f"/devices/{vid}/commands"
        code, _, _, _ = self.api.req("POST", path, {"action": "on"}, token=self.api.tokens.get("viewer"))
        self.mark(A, "viewer 명령 → 403", 1 if code == 403 else 0, 1, f"→ {code}")
        mtok = self.api.tokens.get("manager")

        def cmd(action, key):
            t = time.time()
            c, js, dt, err = self.api.req("POST", path, {"action": action}, token=mtok,
                                          headers={"Idempotency-Key": key}, timeout=25)
            return c, js if isinstance(js, dict) else {}, dt, t

        k1 = "g-" + uuid.uuid4().hex[:10]
        c, js, dt, t1 = cmd("on", k1)
        time.sleep(3)
        state_on = self.vent_state_after(vid, t1) == "on"
        ok_resp = c == 200 and js.get("result") in ("acked", "state_confirmed") and js.get("state") == "on" and dt <= 15
        self.mark(A, "정상 명령", 1.5 * ok_resp + 1.5 * state_on, 3, f"→ {c} {js.get('result')} {dt:.1f}초")
        n_before = len(self.cmd_msgs(vid, t1))
        c2, js2, _, _ = cmd("on", k1)
        time.sleep(3)
        n_after = len(self.cmd_msgs(vid, t1))
        self.mark(A, "같은 Idempotency-Key 재요청", (c2 == 200 and js2.get("cmd_id") == js.get("cmd_id")) * 1
                  + (n_after == n_before) * 1, 2, f"cmd 메시지 {n_before}→{n_after}")
        self.fault(vid, "no-ack", 40)
        c, js, dt, t2 = cmd("off", "g-" + uuid.uuid4().hex[:10])
        time.sleep(2)
        st = self.vent_state_after(vid, t2)
        res = js.get("result")
        pts = 2 if (c == 200 and res == "state_confirmed" and st == "off" and dt <= 15) else \
            1 if (st == "off" and c == 200 and dt <= 15) else 0
        self.mark(A, "ack 없음", pts, 2, f"→ {c} {res} {dt:.1f}초, 실제 상태 {st}")
        self.stack.simctl("clear", vid)
        delay = self.rng.uniform(9, 13) if self.hidden else 12
        self.fault(vid, "slow-ack", 40, round(delay, 1))
        c, js, dt, t3 = cmd("on", "g-" + uuid.uuid4().hex[:10])
        time.sleep(delay + 4)
        ids = {m.get("cmd_id") for m in self.cmd_msgs(vid, t3)}
        flips = self.vent_flips(vid, t3)
        self.mark(A, "늦은 ack", (c == 200 and dt <= 15) * 1 + (len(ids) <= 1 and flips <= 1) * 1, 2,
                  f"→ {c} {js.get('result')} {dt:.1f}초, 보낸 명령 {len(ids)}개, 상태 변화 {flips}번")
        self.stack.simctl("clear", vid)

    def cmd_msgs(self, vid, since):
        out = []
        for t, topic, p in self.cap.snapshot():
            if t >= since - CAPTURE_SLACK_S and topic.endswith(f"/{vid}/cmd"):
                try:
                    out.append(json.loads(p))
                except ValueError:
                    out.append({})
        return out

    def vent_state_after(self, vid, since):
        st = None
        for t, topic, p in self.cap.snapshot():
            if t >= since and topic.endswith(f"/vent/{vid}"):
                m = rules.parse_message(p)
                if m:
                    st = m["data"].get("state")
        return st

    def vent_flips(self, vid, since):
        prev, flips = None, 0
        for t, topic, p in self.cap.snapshot():
            if t >= since - CAPTURE_SLACK_S and topic.endswith(f"/vent/{vid}"):
                m = rules.parse_message(p)
                if m:
                    s = m["data"].get("state")
                    if prev is not None and s != prev:
                        flips += 1
                    prev = s
        return flips

    # ---- 6. 보안 (5) ----
    def test_security(self):
        log("▶ 보안")
        A = "보안"
        r = self.stack.run("ps", "--format", "json")
        ports = set()
        txt = r.stdout.strip()
        rows = []
        try:
            rows = json.loads(txt) if txt.startswith("[") else [json.loads(x) for x in txt.splitlines() if x.strip()]
        except ValueError:
            pass
        for row in rows:
            for p in row.get("Publishers") or []:
                if p.get("PublishedPort"):
                    ports.add(int(p["PublishedPort"]))
        self.mark(A, "호스트 공개 포트 = API 하나", 2 if ports == {int(self.cfg["api_port"])} else 0, 2,
                  f"공개 포트 {sorted(ports)}")
        # 대조군: 계정을 주면 받아진다는 것을 먼저 확인한다 (브로커가 조용한 것과 거부를 구분)
        acc = self.cfg["mqtt_grader"]
        ok = self.stack.exec("mosquitto", "mosquitto_sub", "-h", "127.0.0.1", "-p", "1883",
                             "-u", acc["username"], "-P", acc["password"],
                             "-t", f"{self.prefix}/#", "-C", "1", "-W", "6", timeout=25)
        anon = self.stack.exec("mosquitto", "mosquitto_sub", "-h", "127.0.0.1", "-p", "1883", "-t", "#",
                               "-C", "1", "-W", "6", timeout=25)
        refused = bool(anon.returncode) or "not authoris" in (anon.stdout + anon.stderr).lower() \
            or "connection refused" in (anon.stdout + anon.stderr).lower()
        if not ok.stdout.strip():
            pts, note = 0, "채점용 계정으로도 메시지를 받지 못해 판정 불가 (ACL · 계정 확인 필요)"
        elif anon.stdout.strip():
            pts, note = 0, "익명으로 메시지가 수신됨 (allow_anonymous true)"
        elif refused:
            pts, note = 2, "익명 접속 거부 확인"
        else:
            pts, note = 1, "익명 수신은 없지만 거부 근거를 확인하지 못함 (ACL 로만 막힌 것으로 보임)"
        self.mark(A, "익명 MQTT 거부", pts, 2, note)
        g = subprocess.run(["git", "-C", str(self.repo), "ls-files"], capture_output=True, text=True)
        tracked = [f for f in g.stdout.splitlines() if f == ".env" or f.endswith("/.env")]
        okgit = g.returncode == 0 and not tracked
        self.mark(A, ".env 미포함", 1 if okgit else 0, 1,
                  "git 저장소 아님" if g.returncode else (f"추적 중: {tracked}" if tracked else ""))

    # ---- 데이터 품질 점수 (15) ----
    def score_data(self):
        log("▶ 데이터 품질 · 상태 점수 계산")
        A = "데이터 품질"
        snap = self.cap.snapshot()
        cap_valid, cap_invalid, self.events = {}, {}, []
        cutoff = self.t_end - SETTLE_S
        sent_after = set()
        for t, topic, p in snap:
            parts = topic.split("/")                    # <prefix>/<room>/<type>/<device>
            if len(parts) != self.pfx_depth + 3 or t > cutoff:
                continue
            kind, did = parts[-2], parts[-1]
            m = rules.parse_message(p)
            if m is None or not rules.valid_data(kind, m["data"]):
                if m is not None:
                    cap_invalid.setdefault(did, set()).add(m["seq"])
                continue
            te = self.cap.event_time(t, m)
            seen = cap_valid.setdefault(did, {})
            if m["seq"] in seen:
                continue                                  # dup 고장 — 정답 계산에는 한 번만 쓴다
            seen[m["seq"]] = m["data"]
            self.events.append((te, did, kind, m["data"]))
            if te > getattr(self, "t_malformed_end", 1e18) + 2:
                sent_after.add(did)
        self.events.sort(key=lambda e: e[0])
        frm, to = (self.t_start - 600) * 1000, (time.time() + 600) * 1000
        total = hit = fabricated = mismatch = dups = stored_invalid = 0
        after_malformed = 0
        per_dev = {}
        for did in self.by_id:
            items = [i for i in self.api.all_readings(did, frm, to) if isinstance(i, dict)]
            cv = cap_valid.get(did, {})
            if not cv:
                continue
            # 채점 대상 seq 범위: 원본에서 본 정상 seq + 잘못된 메시지의 seq 까지 포함해야
            # "잘못된 값을 저장했는지" 를 셀 수 있다 (malformed 구간이 끝에 올 수 있다).
            inv = cap_invalid.get(did, set())
            lo = min(cv)
            hi = max(max(cv), max(inv)) if inv else max(cv)
            seqs = [i.get("seq") for i in items]
            dups += len(seqs) - len(set(seqs))
            api = {}
            for i in items:
                api.setdefault(i.get("seq"), i)
            total += len(cv)
            got = len(set(cv) & set(api))
            hit += got
            per_dev[did] = got / len(cv)
            for s, i in api.items():
                if not _isnum(s) or not lo <= s <= hi:
                    continue
                if s in inv and s not in cv:
                    stored_invalid += 1
                elif s not in cv:
                    fabricated += 1
                elif not _same(i.get("data"), cv[s]):
                    mismatch += 1
            if did in sent_after and any(_isnum(i.get("received_at"))
                                         and i["received_at"] / 1000 > self.t_malformed_end for i in items):
                after_malformed += 1
        pooled = hit / total if total else 0.0
        cov = sum(per_dev.values()) / len(per_dev) if per_dev else 0.0
        worst = min(per_dev.items(), key=lambda kv: kv[1]) if per_dev else None
        self.mark(A, "저장률", 6 * rules.lin(cov, 0.99, 0.85), 6,
                  f"기기별 평균 {cov:.1%} (전체 {pooled:.1%}, {hit}/{total}건"
                  + (f", 최저 {worst[0]} {worst[1]:.1%}" if worst else "") + ")")
        bad = fabricated + mismatch
        self.mark(A, "값 일치 (위조·변형 없음)", 3 if bad == 0 and hit else 3 * max(0, 1 - bad / max(hit, 1) * 20) if hit else 0,
                  3, f"없는 seq {fabricated}, 값 불일치 {mismatch}")
        self.mark(A, "중복 저장 없음", 3 if dups == 0 and hit else 1 if dups <= 5 and hit else 0, 3, f"중복 {dups}건")
        n_dev = len(sent_after)
        surv = (getattr(self, "malformed_alive", 0) >= 0.9) + (stored_invalid == 0 and hit > 0) \
            + (n_dev > 0 and after_malformed / n_dev >= 0.8)
        self.mark(A, "깨진 메시지 처리", surv, 3,
                  f"주입 중 health {getattr(self, 'malformed_alive', 0):.0%}, 잘못된 값 저장 {stored_invalid}, "
                  f"이후 수신 기기 {after_malformed}/{n_dev}")
        self.mark("상태 판정", "정상 기기를 stale·offline 로 잘못 표시 없음",
                  2 if self.false_stale == 0 else 1 if self.false_stale <= 3 else 0, 2, f"{self.false_stale}회")

    # ---- 경보 점수 (15) ----
    def score_alerts(self):
        log("▶ 경보 점수 계산")
        A = "경보"
        eps = rules.episodes(self.events, self.t_end - SETTLE_S)
        alerts = sorted(self.alerts.seen.values(), key=lambda x: x[0])
        alerts = [(t, a) for t, a in alerts if t <= self.t_end]
        m = rules.match_alerts(eps, alerts, horizon=self.t_end - 35, ignore_before=self.t_start + 10)
        self.alert_result = m
        if not self.events:
            # 원본을 한 건도 못 받았다 → 정답을 만들 수 없다 (계약 위반: grader 계정 · ACL)
            m["f1"] = 0.0
            self.notes.append("원본 메시지를 한 건도 받지 못함 — 채점용 MQTT 계정(grading.json 의 mqtt_grader)이"
                              " ACL 에서 읽기 거부되었을 가능성. 데이터 품질 · 경보를 채점할 수 없음.")
        elif not eps:
            m["f1"] = 1.0 if m["fp"] == 0 else m["f1"]
            self.notes.append("경보 정답 구간이 없었다 (이상치 미발생). 오경보만 확인했다.")
        elif not m["episodes"]:
            self.notes.append("정답 구간이 모두 채점 구간(시작 10초 ~ 종료 35초 전) 밖이었다 — 오경보만 반영.")
            m["f1"] = 1.0 if m["fp"] == 0 else m["f1"]
        self.mark(A, "정답 구간 대비 F1", 12 * m["f1"], 12,
                  f"구간 {len(m['episodes'])} · 탐지 {m['tp']} · 늦음 {m['late']} · 놓침 {m['fn'] - m['late']} · 오경보 {m['fp']}")
        found = m["tp"] + m["late"]
        rate = m["dup"] / found if found else 1.0
        self.mark(A, "중복 경보 억제", 3 * max(0.0, 1 - rate) if found else 0, 3, f"중복 {m['dup']}건")

    # ---- 코드 흔적 검사 (점수 없음, 평가자 확인용) ----
    def scan_code(self):
        pat = re.compile(r"simctl|/truth|GRADE_SALT|anomaly_windows|anomaly_active|grade-report|grader")
        hits = []
        for p in self.repo.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".js", ".ts", ".html") and "sensor-sim" not in p.parts \
                    and "grader" not in p.parts and p.stat().st_size < 2_000_000:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if pat.search(line):
                        hits.append(f"{p.relative_to(self.repo)}:{i}: {line.strip()[:120]}")
        if hits:
            self.notes.append("평가자 확인 필요 — 채점·시뮬레이터 내부를 참조하는 코드:\n  " + "\n  ".join(hits[:20]))


AREAS = [("기동·계약", 15), ("데이터 품질", 15), ("상태 판정", 10), ("경보", 15), ("제어", 10), ("보안", 5)]


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _same(a, b):
    if not isinstance(a, dict):
        return False
    for k, v in b.items():
        w = a.get(k)
        if _isnum(v):
            if not _isnum(w) or abs(w - v) > 1e-6 * max(1.0, abs(v)):
                return False
        elif k == "samples":
            if not isinstance(w, list) or len(w) != len(v):
                return False
        elif w != v:
            return False
    return True


def _fmt(dt):
    return f"{dt:.0f}초" if dt is not None else "감지 못함"


def report(g, error=None):
    out = g.repo / "grade-report"
    out.mkdir(exist_ok=True)
    total = sum(min(g.scores.get(a, 0), m) for a, m in AREAS)
    data = {"version": VERSION, "profile": g.args.profile, "student_id": g.cfg.get("student_id"),
            "total": round(total, 2), "max": 70, "areas": {a: round(g.scores.get(a, 0), 2) for a, _ in AREAS},
            "items": g.marks, "notes": g.notes, "error": error,
            "alerts": getattr(g, "alert_result", None)}
    (out / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    L = [f"# 자동 채점 결과 — {g.cfg.get('student_id')} ({g.args.profile})", "",
         f"**합계 {total:.1f} / 70**", ""]
    if error:
        L += [f"> 채점 중단: {error}", ""]
    L += ["| 영역 | 점수 |", "|---|---|"] + [f"| {a} | {g.scores.get(a, 0):.1f} / {m} |" for a, m in AREAS]
    L += ["", "| 영역 | 항목 | 점수 | 비고 |", "|---|---|---|---|"]
    L += [f"| {m['area']} | {m['item']} | {m['score']} / {m['max']} | {m['note']} |" for m in g.marks]
    if getattr(g, "alert_result", None):
        L += ["", "## 경보 구간별 결과", "", "| 시작 | 기기 | 종류 | 결과 | 지연(초) | 경보 수 |", "|---|---|---|---|---|---|"]
        for e in g.alert_result["episodes"]:
            L.append(f"| {time.strftime('%H:%M:%S', time.localtime(e['start']))} | {e['device_id']} | {e['type']} | "
                     f"{e['result']} | {e['delay'] if e['delay'] is not None else '-'} | {e['alerts']} |")
        if g.alert_result["false_alerts"]:
            L += ["", "오경보 예: " + ", ".join(f"{a.get('device_id')}/{a.get('type')}" for a in g.alert_result["false_alerts"][:10])]
    if g.notes:
        L += ["", "## 참고"] + [f"- {n}" for n in g.notes]
    (out / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"합계 {total:.1f}/70 → {out / 'report.md'}")


def check_config(repo: Path):
    """grading.json 을 미리 검사한다 — 채점 시작 뒤에 KeyError 로 죽지 않도록."""
    f = repo / "grading.json"
    if not f.exists():
        raise ValueError(f"{repo} 에 grading.json 이 없습니다 (CONTRACT.md 0절)")
    try:
        cfg = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"grading.json 이 올바른 JSON 이 아닙니다: {e}") from None
    miss = []
    if not str(cfg.get("student_id", "")).strip():
        miss.append("student_id")
    if not isinstance(cfg.get("api_port"), int):
        miss.append("api_port (정수)")
    for role in ("viewer", "manager"):
        a = (cfg.get("accounts") or {}).get(role)
        if not (isinstance(a, dict) and a.get("username") and a.get("password")):
            miss.append(f"accounts.{role}.username / password")
    gr = cfg.get("mqtt_grader")
    if not (isinstance(gr, dict) and gr.get("username") and gr.get("password")):
        miss.append("mqtt_grader.username / password")
    if miss:
        raise ValueError("grading.json 에 다음이 없거나 형식이 틀렸습니다 (CONTRACT.md 0절): " + ", ".join(miss))
    cf = cfg.get("compose_file", "compose.yaml")
    if not (repo / cf).exists():
        raise ValueError(f"{repo / cf} 가 없습니다 (grading.json 의 compose_file)")
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("--sim-dir", required=True, help="채점에 쓸 sensor-sim 폴더 (평가자 것)")
    ap.add_argument("--profile", choices=["public", "hidden"], default="public")
    ap.add_argument("--salt", default="", help="hidden 전용: 이상치 시점·방 배치를 바꾸는 비밀 값")
    ap.add_argument("--seed", type=int, default=0, help="hidden 전용: 고장 순서·대상·강도 난수 시드")
    ap.add_argument("--keep", action="store_true", help="끝난 뒤 컨테이너를 내리지 않음 (디버깅용)")
    args = ap.parse_args()
    if args.profile == "hidden" and not args.salt:
        ap.error("hidden 에는 --salt 가 필요합니다")
    if not (Path(args.sim_dir) / "Dockerfile").exists():
        ap.error(f"--sim-dir {args.sim_dir} 에 sensor-sim 이 없습니다")
    try:
        check_config(Path(args.repo))
    except ValueError as e:
        ap.error(str(e))
    g = Grader(args)
    err = None
    try:
        g.run()
    except Exception as e:  # noqa: BLE001
        err = str(e)
        log(f"채점 중단: {e}")
        try:
            g.notes.append("기동 진단:\n```\n" + g.stack.diagnose() + "\n```")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            report(g, err)
        finally:
            if not args.keep:
                g.stack.down()


if __name__ == "__main__":
    main()
