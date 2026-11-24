"""고장 주입용 제어 서버 (컨테이너 내부 127.0.0.1 전용, 외부 미노출).

simctl 명령이 이 서버를 호출한다:  docker compose exec sim simctl ...
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .device import FAULT_MODES
from .truth import anomaly_schedule, fault_history


def select(devices, target: str):
    """target: 기기 ID(co2-01) | type:co2 | room:r301 | all"""
    if target == "all":
        return list(devices)
    if target.startswith("type:"):
        return [d for d in devices if d.kind == target[5:]]
    if target.startswith("room:"):
        return [d for d in devices if d.room.name == target[5:]]
    return [d for d in devices if d.device_id == target]


def serve(devices, host: str, port: int, clock=None, grade_mode: bool = False):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 접근 로그 숨김 (학생 로그에 고장 정보가 보이지 않도록)
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/devices":
                return self._send(200, [d.info() for d in devices])
            if u.path == "/modes":
                return self._send(200, FAULT_MODES)
            if u.path == "/truth":
                if grade_mode:
                    # 채점 모드에서는 정답(이상치 일정)을 알려주지 않는다.
                    # 채점기는 브로커에서 받은 원본으로 정답을 직접 계산하므로 이 경로를 쓰지 않는다.
                    return self._send(403, {"error": "채점 모드에서는 truth 를 제공하지 않습니다."})
                q = parse_qs(u.query)
                past = float(q.get("past", ["30"])[0]) * 60
                ahead = float(q.get("ahead", ["0"])[0]) * 60
                now = time.time()
                return self._send(200, {"now_ms": int(now * 1000), "started_ms": int(clock.r0 * 1000),
                                        "anomalies": anomaly_schedule(devices, clock, now - past, now + ahead),
                                        "faults": [f for f in fault_history() if f["ts"] >= (now - past) * 1000]})
            self._send(404, {"error": "없는 경로"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "잘못된 JSON"})
            targets = select(devices, req.get("target", ""))
            if not targets:
                return self._send(404, {"error": f"대상 없음: {req.get('target')}"})
            try:
                if self.path == "/fault":
                    targets = [d for d in targets if d.set_fault(req["mode"], req.get("duration"), req.get("param"))]
                    if not targets:
                        return self._send(400, {"error": f"{req['mode']} 을(를) 적용할 수 있는 기기가 없습니다 (vent 전용 모드?)"})
                elif self.path == "/clear":
                    for d in targets:
                        d.clear(req.get("mode"))
                elif self.path == "/cmd":
                    for d in targets:
                        d.inject_command(req["payload"])
                else:
                    return self._send(404, {"error": "없는 경로"})
            except (KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            self._send(200, {"ok": True, "devices": [d.device_id for d in targets]})

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="control").start()
    return srv
