"""Тесты MQTT-транспорта: фейковый paho-клиент вместо брокера (офлайн).

Фейковый клиент повторяет поверхность paho, которую использует MqttTransport:
connect/loop_*/publish/subscribe/disconnect + колбэки on_connect/on_subscribe/on_message.
"""

import json
from types import SimpleNamespace

import pytest

import io_core.session as session_mod
from io_core import JsonlJournal, MqttTransport, Session, read_events


def reason(code_value: int) -> SimpleNamespace:
    """Похоже на paho ReasonCode: is_failure = value >= 0x80."""
    return SimpleNamespace(value=code_value, is_failure=code_value >= 0x80)


class FakePublishInfo:
    def __init__(self, rc: int = 0, acked: bool = True) -> None:
        self.rc = rc
        self._acked = acked

    def wait_for_publish(self, timeout=None) -> None:
        pass  # реальный paho просто выходит по таймауту, не бросая исключений

    def is_published(self) -> bool:
        return self._acked


class FakeClient:
    def __init__(self) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_subscribe = None
        self.on_message = None
        self.published: list[tuple] = []
        self.subscriptions: list[tuple] = []
        self.disconnected = False
        self.silent_open = False  # True: CONNACK не приходит (брокер «завис»)
        self.connack_rc = reason(0)  # можно подменить на отказ, например reason(135)
        self.refuse = False  # True: connect бросает ConnectionRefusedError
        self.publish_rc = 0
        self.publish_acked = True  # False: PUBACK не приходит (умерший линк)
        self.subscribe_rc = 0
        self.suback_codes: list | None = None  # None: успех; иначе коды ответа брокера
        self.silent_suback = False  # True: SUBACK не приходит
        self._mid = 10

    def connect(self, host, port=1883, keepalive=60):
        if self.refuse:
            raise ConnectionRefusedError("broker down")
        if not self.silent_open:
            self.on_connect(self, None, None, self.connack_rc, None)

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return FakePublishInfo(self.publish_rc, acked=self.publish_acked)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        mid = self._mid
        self._mid += 1
        if not self.silent_suback:
            codes = self.suback_codes if self.suback_codes is not None else [reason(0)]
            self.on_subscribe(self, None, mid, codes, None)
        return (self.subscribe_rc, mid)

    def disconnect(self) -> None:
        self.disconnected = True
        if self.on_disconnect:
            self.on_disconnect(self, None, None, 0, None)

    # тестовый хелпер: брокер доставил сообщение (после open() колбэки привязаны)
    def deliver(self, topic: str, payload: bytes) -> None:
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))


def opened_transport(events=None, **kwargs) -> tuple[MqttTransport, FakeClient]:
    fake = FakeClient()
    t = MqttTransport(
        "broker.test",
        timeout=0.5,
        on_event=events,
        client_factory=lambda: fake,
        **kwargs,
    )
    t.open()
    return t, fake


def test_open_publish_subscribe_journal(tmp_path):
    jpath = tmp_path / "session.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        try:
            t.publish("sensors/temp", "22.5", qos=1, retain=True)
            t.subscribe("sensors/#", qos=0)
        finally:
            t.close()
    kinds = [e["kind"] for e in read_events(jpath)]
    assert kinds == ["mqtt_open", "mqtt_publish", "mqtt_subscribe", "mqtt_close"]
    assert fake.published[0] == ("sensors/temp", b"22.5", 1, True)
    assert fake.subscriptions == [("sensors/#", 0)]


def test_publish_without_puback_raises():
    t, fake = opened_transport()
    fake.publish_acked = False
    try:
        with pytest.raises(OSError, match="not acknowledged"):
            t.publish("t", "x")
    finally:
        t.close()


def test_subscribe_rejected_by_broker_raises():
    t, fake = opened_transport()
    fake.suback_codes = [reason(135)]  # Not authorized
    try:
        with pytest.raises(OSError, match="refused"):
            t.subscribe("secret/#")
    finally:
        t.close()


def test_subscribe_without_suback_raises():
    t, fake = opened_transport()
    fake.silent_suback = True
    try:
        with pytest.raises(OSError, match="SUBACK"):
            t.subscribe("t")
    finally:
        t.close()


def test_read_message_delivers_incoming(tmp_path):
    jpath = tmp_path / "session.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        try:
            fake.deliver("sensors/temp", b"22.5")
            msg = t.read_message(timeout=0.5)
        finally:
            t.close()
    assert msg == {"topic": "sensors/temp", "payload": "22.5"}
    kinds = [e["kind"] for e in read_events(jpath)]
    assert "mqtt_message" in kinds


def test_read_message_timeout_returns_none():
    t, _fake = opened_transport()
    try:
        assert t.read_message(timeout=0.05) is None
    finally:
        t.close()


def test_queue_overflow_drops_oldest():
    t, fake = opened_transport(queue_size=2)
    try:
        fake.deliver("t", b"m1")
        fake.deliver("t", b"m2")
        fake.deliver("t", b"m3")
        assert t.read_message(timeout=0.1) == {"topic": "t", "payload": "m2"}
        assert t.read_message(timeout=0.1) == {"topic": "t", "payload": "m3"}
        assert t.read_message(timeout=0.05) is None
    finally:
        t.close()


def test_bad_utf8_replaced_not_raised():
    t, fake = opened_transport()
    try:
        fake.deliver("t", b"\xff\xfe")
        assert t.read_message(timeout=0.1) == {"topic": "t", "payload": "\ufffd\ufffd"}
    finally:
        t.close()


def test_open_silent_broker_raises_connection_error():
    fake = FakeClient()
    fake.silent_open = True
    t = MqttTransport("broker.test", timeout=0.2, client_factory=lambda: fake)
    with pytest.raises(ConnectionError, match="mqtt broker"):
        t.open()
    assert t._client is None


def test_open_connack_failure_raises_connection_error():
    fake = FakeClient()
    fake.connack_rc = reason(134)  # Bad user name or password
    t = MqttTransport("broker.test", timeout=0.5, client_factory=lambda: fake)
    with pytest.raises(ConnectionError, match="mqtt broker"):
        t.open()
    assert t._client is None


def test_open_refused_propagates():
    fake = FakeClient()
    fake.refuse = True
    t = MqttTransport("broker.test", timeout=0.2, client_factory=lambda: fake)
    with pytest.raises(ConnectionRefusedError):
        t.open()


def test_default_factory_passes_client_id():
    t = MqttTransport("broker.test", client_id="agent-1")
    client = t._default_client_factory()
    assert client._client_id in ("agent-1", b"agent-1")  # paho хранит id строкой или байтами


def test_close_disconnects_and_second_close_is_noop(tmp_path):
    jpath = tmp_path / "session.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        t.close()
        t.close()  # повторный close — тихий no-op
    assert fake.disconnected
    kinds = [e["kind"] for e in read_events(jpath)]
    assert kinds.count("mqtt_close") == 1


def test_publish_error_rc_raises():
    t, fake = opened_transport()
    fake.publish_rc = 3
    try:
        with pytest.raises(OSError, match="publish"):
            t.publish("t", "x")
    finally:
        t.close()


def test_subscribe_error_rc_raises():
    t, fake = opened_transport()
    fake.subscribe_rc = 129
    try:
        with pytest.raises(OSError, match="subscribe"):
            t.subscribe("t")
    finally:
        t.close()


def test_operation_before_open_raises():
    t = MqttTransport("broker.test", client_factory=FakeClient)
    with pytest.raises(AssertionError, match="not open"):
        t.publish("t", "x")


# --- IH-29: a failed operation is journaled before the exception escapes ---


def test_failed_publish_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        fake.publish_acked = False
        try:
            with pytest.raises(OSError, match="not acknowledged"):
                t.publish("t", "x")
        finally:
            t.close()
    failed = [e for e in read_events(jpath) if e["kind"] == "mqtt_publish_failed"]
    assert len(failed) == 1
    assert failed[0]["topic"] == "t" and "not acknowledged" in failed[0]["error"]
    assert not [e for e in read_events(jpath) if e["kind"] == "mqtt_publish"]


def test_failed_subscribe_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        fake.suback_codes = [reason(135)]  # Not authorized
        try:
            with pytest.raises(OSError, match="refused"):
                t.subscribe("secret/#")
        finally:
            t.close()
    failed = [e for e in read_events(jpath) if e["kind"] == "mqtt_subscribe_failed"]
    assert len(failed) == 1 and "refused" in failed[0]["error"]
    assert not [e for e in read_events(jpath) if e["kind"] == "mqtt_subscribe"]


def test_refused_open_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    fake = FakeClient()
    fake.refuse = True
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ConnectionRefusedError):
        MqttTransport(
            "broker.test", timeout=0.5, on_event=jr, client_factory=lambda: fake
        ).open()
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["mqtt_open_failed"]
    assert "broker down" in events[0]["error"]


def test_open_timeout_is_journaled(tmp_path):
    # the previously-silent path: CONNACK never arrives -> teardown, and now
    # the failure lands in the journal too
    jpath = tmp_path / "j.jsonl"
    fake = FakeClient()
    fake.silent_open = True
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ConnectionError):
        MqttTransport(
            "broker.test", timeout=0.3, on_event=jr, client_factory=lambda: fake
        ).open()
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["mqtt_open_failed"]
    assert "broker" in events[0]["error"]


def test_publish_error_rc_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        fake.publish_rc = 3
        try:
            with pytest.raises(OSError, match="rc=3"):
                t.publish("t", "x")
        finally:
            t.close()
    failed = [e for e in read_events(jpath) if e["kind"] == "mqtt_publish_failed"]
    assert len(failed) == 1 and "rc=3" in failed[0]["error"]


def test_subscribe_without_suback_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t, fake = opened_transport(jr)
        fake.silent_suback = True
        try:
            with pytest.raises(OSError, match="SUBACK"):
                t.subscribe("t")
        finally:
            t.close()
    failed = [e for e in read_events(jpath) if e["kind"] == "mqtt_subscribe_failed"]
    assert len(failed) == 1 and "SUBACK" in failed[0]["error"]


def test_invalid_topic_valueerror_is_journaled(tmp_path):
    # paho rejects invalid topics with ValueError before touching the wire -
    # the refusal still must be journaled
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        fake = FakeClient()

        def boom(topic, payload=None, qos=0, retain=False):
            raise ValueError("Invalid topic.")

        fake.publish = boom
        t = MqttTransport(
            "broker.test", timeout=0.5, on_event=jr, client_factory=lambda: fake
        )
        t.open()
        try:
            with pytest.raises(ValueError, match="Invalid topic"):
                t.publish("bad+", "x")
        finally:
            t.close()
    failed = [e for e in read_events(jpath) if e["kind"] == "mqtt_publish_failed"]
    assert len(failed) == 1 and "Invalid topic" in failed[0]["error"]


def test_invalid_subscribe_topic_valueerror_is_journaled(tmp_path):
    # review N3: the subscribe ValueError path shares the publish-shaped
    # except, and gets its own direct pin
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        fake = FakeClient()

        def boom(topic, qos=0):
            raise ValueError("Invalid subscription.")

        fake.subscribe = boom
        t = MqttTransport(
            "broker.test", timeout=0.5, on_event=jr, client_factory=lambda: fake
        )
        t.open()
        try:
            with pytest.raises(ValueError, match="Invalid subscription"):
                t.subscribe("bad+")
        finally:
            t.close()
    failed = [e for e in read_events(jpath) if e["kind"] == "mqtt_subscribe_failed"]
    assert len(failed) == 1 and "Invalid subscription" in failed[0]["error"]


# --- сессия: те же операции через Session (транспорт подменён) ---


class FakeSessionTransport:
    """Минимальная замена MqttTransport с тем же контрактом конструктора/методов."""

    def __init__(self, host, *, port=1883, client_id="", timeout=3.0, on_event=None):
        self.host = host
        self.on_event = on_event
        self.inbox: list[dict[str, str]] = []

    def open(self) -> None:
        self.on_event("mqtt_open", {"host": self.host})

    def close(self) -> None:
        self.on_event("mqtt_close", {})

    def publish(self, topic, payload, *, qos=0, retain=False):
        self.on_event("mqtt_publish", {"topic": topic, "payload": payload})

    def subscribe(self, topic, *, qos=0) -> None:
        self.on_event("mqtt_subscribe", {"topic": topic})

    def read_message(self, timeout: float = 1.0):
        return self.inbox.pop(0) if self.inbox else None


@pytest.fixture()
def fake_session_transport(monkeypatch):
    created: list[FakeSessionTransport] = []

    class Factory(FakeSessionTransport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(session_mod, "MqttTransport", Factory)
    return created


def test_session_mqtt_flow(tmp_path, fake_session_transport):
    jpath = tmp_path / "session.jsonl"
    s = Session(jpath, tmp_path / "sandbox", actor="test")
    s.mqtt_open("bus", "broker.test", client_id="agent-1")
    s.mqtt_subscribe("bus", "cmd/#", qos=1)
    s.mqtt_publish("bus", "cmd/led", "on")
    fake_session_transport[-1].inbox.append({"topic": "cmd/led", "payload": "done"})
    assert s.mqtt_read("bus", timeout=0.1) == {"topic": "cmd/led", "payload": "done"}
    assert s.mqtt_read("bus", timeout=0.05) is None
    s.close()
    kinds = [json.loads(line)["kind"] for line in jpath.read_text(encoding="utf-8").splitlines()]
    assert kinds == [
        "mqtt_open",
        "mqtt_subscribe",
        "mqtt_publish",
        "mqtt_close",
    ]
