"""계약(CONTRACT.md) 4·5절 규칙과 채점 계산. 표준 라이브러리만 사용.

채점기와 참조 구현이 같은 규칙을 쓰도록 이 파일 하나에 모았다.
"""
import json
import math

RANGES = {
    "temperature": (-40, 85), "humidity": (0, 100), "co2": (0, 10000),
    "count": (0, 1000), "capacity": (0, 1000), "power": (0, 20000), "energy": (0, float("inf")),
}
ALERT_TYPES = ("overheat", "co2_high", "overload", "over_capacity", "door_open_long", "fall")
TYPE_OF_ALERT = {"overheat": "env", "co2_high": "co2", "overload": "power",
                 "over_capacity": "occupancy", "door_open_long": "door", "fall": "imu"}
MERGE_GAP = 60.0          # 구간이 끝나고 이 시간 안에 다시 시작하면 같은 구간
ALERT_BUDGET = 30.0       # 구간 시작 후 경보가 보여야 하는 시간
DOOR_OPEN_LONG = 120.0
STALE_AFTER = {"vent": 90.0, "door": None}
STALE_DEFAULT = 30.0


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_message(payload):
    """payload(str|bytes) → dict 또는 None (JSON 아님·필수 필드 없음)."""
    try:
        m = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(m, dict) or not all(k in m for k in ("device_id", "seq", "ts", "data")):
        return None
    if not isinstance(m["data"], dict) or not _num(m["seq"]) or not _num(m["ts"]):
        return None
    return m


def valid_data(kind, data):
    """4절 범위 검증."""
    if kind == "door":
        return data.get("state") in ("open", "closed")
    if kind == "vent":
        return data.get("state") in ("on", "off")
    if kind == "imu":
        s = data.get("samples")
        if not isinstance(s, list) or not s:
            return False
        return all(isinstance(r, list) and len(r) >= 6 and all(_num(x) for x in r)
                   and all(abs(x) <= 16 for x in r[:3]) and all(abs(x) <= 2000 for x in r[3:6]) for r in s)
    if not data:
        return False
    for k, v in data.items():
        if k in RANGES:
            if not _num(v):
                return False
            lo, hi = RANGES[k]
            if not lo <= v <= hi:
                return False
    return True


def condition(kind, data):
    """5절: 메시지 하나가 경보 조건을 만족하는가 (door 제외)."""
    try:
        return _condition(kind, data)
    except (KeyError, TypeError, IndexError):
        return False


def _condition(kind, data):
    if kind == "env":
        return data["temperature"] >= 30.0
    if kind == "co2":
        return data["co2"] >= 2500
    if kind == "power":
        return data["power"] >= 600
    if kind == "occupancy":
        return data["count"] > data["capacity"]
    if kind == "imu":
        return any(math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2) >= 2.5 for r in data["samples"])
    return False


ALERT_OF_TYPE = {v: k for k, v in TYPE_OF_ALERT.items()}


def episodes(events, t_end):
    """events: 시간순 [(t, device_id, kind, data)] (검증 통과한 것만) → 정답 구간 목록."""
    out, state, door = [], {}, {}
    for t, did, kind, data in events:
        if kind == "door":
            st = data["state"]
            if st == "open":
                door.setdefault(did, t)
            else:
                t0 = door.pop(did, None)
                if t0 is not None and t - t0 >= DOOR_OPEN_LONG:
                    out.append({"device_id": did, "type": "door_open_long", "start": t0 + DOOR_OPEN_LONG, "end": t})
            continue
        if kind not in ALERT_OF_TYPE:
            continue
        hit = condition(kind, data)
        cur = state.get(did)
        if hit:
            if cur is None:
                state[did] = {"device_id": did, "type": ALERT_OF_TYPE[kind], "start": t, "end": t}
            else:
                cur["end"] = t
        elif cur is not None:
            cur["end"] = t
            out.append(cur)
            state.pop(did)
    for cur in state.values():
        out.append(cur)
    for did, t0 in door.items():
        if t_end - t0 >= DOOR_OPEN_LONG:
            out.append({"device_id": did, "type": "door_open_long", "start": t0 + DOOR_OPEN_LONG, "end": t_end})
    # 60초 이내 재발은 같은 구간으로 합침
    out.sort(key=lambda e: (e["device_id"], e["type"], e["start"]))
    merged = []
    for e in out:
        p = merged[-1] if merged else None
        if p and p["device_id"] == e["device_id"] and p["type"] == e["type"] and e["start"] - p["end"] <= MERGE_GAP:
            p["end"] = max(p["end"], e["end"])
        else:
            merged.append(dict(e))
    return sorted(merged, key=lambda e: e["start"])


def match_alerts(eps, alerts, poll=2.0, early=5.0, grace=ALERT_BUDGET, horizon=None, ignore_before=None):
    """eps: 정답 구간, alerts: [(first_seen, alert_dict)] → 탐지 · 놓침 · 오경보 · 중복.

    경보 1건은 구간 1개에만 배정한다. 여러 구간의 창에 걸리면 **가장 늦게 시작한(가까운) 구간**에
    배정한다 — 그러지 않으면 앞선 긴 구간이 뒤 구간의 경보를 가로채 "놓침 + 중복" 으로 잘못 센다.

    horizon:       이 시각 이후에 시작한 구간은 경보가 없어도 놓침으로 세지 않는다 (관찰 시간 부족).
    ignore_before: 이 시각 전에 시작한 구간은 채점하지 않고, 그 구간에 배정된 경보도 오경보로 세지 않는다.
    """
    order = sorted(range(len(eps)), key=lambda i: eps[i]["start"])
    assigned = {i: [] for i in range(len(eps))}
    unmatched = []
    for t, a in sorted(alerts, key=lambda x: x[0]):
        cand = [i for i in order
                if a.get("device_id") == eps[i]["device_id"] and a.get("type") == eps[i]["type"]
                and eps[i]["start"] - early <= t <= eps[i]["end"] + grace]
        if cand:
            assigned[max(cand, key=lambda i: eps[i]["start"])].append((t, a))
        else:
            unmatched.append(a)

    tp = late = fn = dup = 0
    detail = []
    for i in order:
        e, got = eps[i], assigned[i]
        if ignore_before is not None and e["start"] < ignore_before:
            continue
        if got:
            first = got[0][0]
            if first <= e["start"] + ALERT_BUDGET + poll:
                tp += 1
                res = "탐지"
            else:
                late += 1
                fn += 1
                res = "늦음"
            dup += len(got) - 1
        elif horizon is not None and e["start"] > horizon:
            continue
        else:
            fn += 1
            res = "놓침"
        detail.append({**e, "result": res, "alerts": len(got),
                       "delay": round(got[0][0] - e["start"], 1) if got else None})
    fp = len(unmatched)
    f1 = 1.0 if tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn)
    return {"tp": tp, "late": late, "fn": fn, "fp": fp, "dup": dup, "f1": round(f1, 3),
            "episodes": detail, "false_alerts": unmatched[:20]}


def lin(x, full, zero):
    """x ≤ full → 1, x ≥ zero → 0, 사이는 직선. (full > zero 이면 반대 방향)"""
    if x is None:
        return 0.0
    if full < zero:
        return 1.0 if x <= full else 0.0 if x >= zero else (zero - x) / (zero - full)
    return 1.0 if x >= full else 0.0 if x <= zero else (x - zero) / (full - zero)
