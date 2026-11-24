"""MQTT 연결(실제) / 표준출력(dry-run) 두 가지 전송 방식."""
import json
import logging
import ssl
import threading

log = logging.getLogger("sim")


class StdoutLink:
    """브로커 없이 동작 확인용: 발행 내용을 한 줄씩 출력."""
    _lock = threading.Lock()

    def __init__(self, cfg, client_id: str, status_topic: str, will: bool = True, cmd_topic=None, on_cmd=None):
        self.client_id = client_id
        self.status_topic = status_topic
        self.alive = True
        self.on_cmd = on_cmd      # dry-run 에서는 simctl cmd 가 이 함수를 직접 호출한다
        if will:
            self._out("CONNECT", status_topic, "online", 1, True)
        if cmd_topic:
            self._out("SUBSCR", cmd_topic, "", 1, False)

    @property
    def connected(self):
        return self.alive

    def _out(self, kind, topic, payload, qos, retain):
        with self._lock:
            print(f"{kind:7s} qos={qos} retain={int(retain)} {topic} {payload}", flush=True)

    def publish(self, topic, payload, qos=0, retain=False):
        self._out("PUBLISH", topic, payload, qos, retain)

    def hard_drop(self):
        self.alive = False
        self._out("LWT", self.status_topic, "offline", 1, True)

    def close(self):
        self.alive = False
        self._out("DISCONN", self.status_topic, "offline", 1, True)


class MqttLink:
    """기기 1대 = MQTT 클라이언트 1개 (기기마다 고유 client_id 와 LWT)."""

    def __init__(self, cfg, client_id: str, status_topic: str, will: bool = True, cmd_topic=None, on_cmd=None):
        import paho.mqtt.client as mqtt

        self.client_id = client_id
        self.status_topic = status_topic
        self.alive = True
        b = cfg.broker
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id=client_id, protocol=mqtt.MQTTv311)
        if b.username:
            c.username_pw_set(b.username, b.password)
        if b.tls:
            c.tls_set(ca_certs=b.ca_file, cert_reqs=ssl.CERT_REQUIRED)
        if will:
            c.will_set(status_topic, "offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        self.cmd_topic, self.on_cmd = cmd_topic, on_cmd
        if cmd_topic:
            c.on_message = lambda _c, _u, msg: self.on_cmd(msg.payload)
        self._announce = will
        self.client = c
        c.connect_async(b.host, b.port, keepalive=b.keepalive)
        c.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            log.info("접속됨       %s", self.client_id)
            if self._announce:
                client.publish(self.status_topic, "online", qos=1, retain=True)
            if self.cmd_topic:                       # 재접속할 때마다 다시 구독 (clean session)
                client.subscribe(self.cmd_topic, qos=1)
        else:
            log.warning("접속 거부    %s: %s", self.client_id, reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.alive:
            log.warning("연결 끊김    %s (%s) — 재연결 시도", self.client_id, reason_code)

    @property
    def connected(self):
        return self.alive and self.client.is_connected()

    def publish(self, topic, payload, qos=0, retain=False):
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def hard_drop(self):
        """DISCONNECT 없이 소켓만 끊는다 → 브로커가 LWT(offline)를 발행."""
        self.alive = False
        self.client.loop_stop()
        sock = self.client.socket()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def close(self):
        """정상 종료: offline 을 직접 알리고 DISCONNECT (LWT 는 발행되지 않음)."""
        self.alive = False
        if self._announce:
            try:
                self.client.publish(self.status_topic, "offline", qos=1, retain=True).wait_for_publish(2)
            except Exception:  # noqa: BLE001 — 종료 중 오류는 무시
                pass
        self.client.disconnect()
        self.client.loop_stop()


def make_link(cfg, client_id, status_topic, dry_run, will=True, cmd_topic=None, on_cmd=None):
    cls = StdoutLink if dry_run else MqttLink
    return cls(cfg, client_id, status_topic, will, cmd_topic, on_cmd)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
