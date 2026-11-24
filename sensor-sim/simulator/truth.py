"""채점용 정답(ground truth): 언제 어떤 '실제 이상 상황'이 생기는지, 어떤 고장이 주입됐는지.

학생의 경보 기록과 비교해 탐지율(놓친 경보) · 오경보를 셀 때 쓴다.
"""
from .device import FAULT_LOG, _FAULT_LOG_LOCK
from .sensors import ANOMALIES, anomaly_windows

LABELS = {
    "overheat": "과열 (+8°C)",
    "vent-fail": "CO2 급증 (+900ppm)",
    "left-open": "문 열림 지속 (이 구간에 열리면 오래 열려 있음)",
    "surge": "전력 과부하 (×2.5)",
    "fall": "낙상 충격 (2초)",
}


def anomaly_schedule(devices, clock, r_from: float, r_to: float):
    """실제 시각 [r_from, r_to] 와 겹치는 이상치 구간 (실제 시각, 초)."""
    r_from = max(r_from, clock.r0)          # 시작 전 구간은 의미 없음
    out = []
    for d in devices:
        for name, period, length, rate, base in ANOMALIES.get(d.kind, []):
            if base == "sim":
                ws = [(clock.real(a), clock.real(b))
                      for a, b in anomaly_windows(d.model.seed, d.device_id, name, period, length, rate,
                                                  clock.sim(r_from), clock.sim(r_to))]
            else:
                ws = [(clock.r0 + a, clock.r0 + b)
                      for a, b in anomaly_windows(d.model.seed, d.device_id, name, period, length, rate,
                                                  r_from - clock.r0, r_to - clock.r0)]
            for a, b in ws:
                out.append({"device_id": d.device_id, "room": d.room.name, "type": d.kind, "anomaly": name,
                            "label": LABELS.get(name, name),
                            "start_ms": int(a * 1000), "end_ms": int(b * 1000),
                            "start_offset_s": round(a - clock.r0, 1), "end_offset_s": round(b - clock.r0, 1)})
    return sorted(out, key=lambda x: x["start_ms"])


def fault_history():
    with _FAULT_LOG_LOCK:
        return list(FAULT_LOG)
