"""Тесты мини-брокера MQTT (mqtt_sim) против реального клиента paho-mqtt:
конформность нужного подмножества — pub/sub, маски, retained, ping."""

from __future__ import annotations

import threading
import time

import paho.mqtt.client as mqtt
import pytest

from io_core.mqtt_sim import MqttSimBroker, topic_matches


@pytest.fixture()
def broker():
    broker = MqttSimBroker()
    port = broker.start()
    yield broker, port
    broker.stop()


def make_client(port: int, client_id: str, on_message=None) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id
    )
    if on_message is not None:
        client.on_message = on_message
    client.connect("127.0.0.1", port)
    client.loop_start()
    return client


def test_topic_matches_masks():
    assert topic_matches("sensors/#", "sensors/a/b")
    assert topic_matches("sensors/+", "sensors/temp")
    assert not topic_matches("sensors/+", "sensors/temp/x")
    assert not topic_matches("sensors/#", "actuators/led")
    assert topic_matches("exact", "exact")
    assert not topic_matches("+", "a/b")


def test_pub_sub_roundtrip_with_mask(broker):
    _broker, port = broker
    got = []
    event = threading.Event()

    def on_message(_c, _ud, msg):
        got.append((msg.topic, msg.payload))
        event.set()

    sub = make_client(port, "sub", on_message)
    sub.subscribe("sensors/#")
    time.sleep(0.3)  # SUBACK + подписка применены
    pub = make_client(port, "pub")
    pub.publish("sensors/temp", "21.5")
    assert event.wait(timeout=5), "подписчик не получил сообщение"
    assert got[-1] == ("sensors/temp", b"21.5")
    sub.loop_stop()
    pub.loop_stop()


def test_retained_delivered_on_subscribe(broker):
    _broker, port = broker
    pub = make_client(port, "pub")
    pub.publish("cmd/led", "on", retain=True)
    time.sleep(0.3)

    got = []
    event = threading.Event()

    def on_message(_c, _ud, msg):
        got.append((msg.topic, msg.payload))
        event.set()

    sub = make_client(port, "sub", on_message)
    sub.subscribe("cmd/#")
    assert event.wait(timeout=5), "retained не доставлен при подписке"
    assert got[-1] == ("cmd/led", b"on")
    sub.loop_stop()
    pub.loop_stop()


def test_retained_cleared_by_empty_payload(broker):
    _broker, port = broker
    pub = make_client(port, "pub")
    pub.publish("cmd/led", "on", retain=True)
    time.sleep(0.2)
    pub.publish("cmd/led", b"", retain=True)  # пустой payload снимает retained
    time.sleep(0.2)

    got = []
    event = threading.Event()

    def on_message(_c, _ud, msg):
        got.append(msg.topic)
        event.set()

    sub = make_client(port, "sub", on_message)
    sub.subscribe("cmd/#")
    assert not event.wait(timeout=1.0), "снятый retained не должен доставляться"
    sub.loop_stop()
    pub.loop_stop()


def test_qos1_publish_acked_and_delivered(broker):
    _broker, port = broker
    got = []
    event = threading.Event()

    def on_message(_c, _ud, msg):
        got.append(msg.payload)
        event.set()

    sub = make_client(port, "sub", on_message)
    sub.subscribe("#")
    time.sleep(0.3)
    pub = make_client(port, "pub")
    info = pub.publish("t/x", "q1", qos=1)
    info.wait_for_publish(timeout=5)
    assert event.wait(timeout=5), "QoS1 сообщение не доставлено"
    assert got[-1] == b"q1"
    sub.loop_stop()
    pub.loop_stop()


def test_keepalive_ping_stays_connected(broker):
    _broker, port = broker
    disconnected = threading.Event()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="pinger"
    )
    client.on_disconnect = lambda *a, **kw: disconnected.set()
    client.connect("127.0.0.1", port, keepalive=1)
    client.loop_start()
    assert not disconnected.wait(timeout=2.5), "брокер не отвечает на ping (соединение рвётся)"
    client.loop_stop()


def test_stop_closes_clients(broker):
    sim, port = broker
    disconnected = threading.Event()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="doomed"
    )
    client.on_disconnect = lambda *a, **kw: disconnected.set()
    client.connect("127.0.0.1", port)
    client.loop_start()
    time.sleep(0.2)
    sim.stop()
    assert disconnected.wait(timeout=5), "после stop клиент не отключился"
    client.loop_stop()
