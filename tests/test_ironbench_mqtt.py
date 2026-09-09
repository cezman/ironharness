"""Tests of MQTT orchestration on the unix target: the mqtt_sim broker + the harness
client + mqtt-publish/mqtt-collect steps. The fake device is a real paho client,
everything is local (no WSL) - it checks the orchestration, not the MicroPython client."""

from __future__ import annotations

import sys
import textwrap

from io_core.mqtt_sim import MqttSimBroker
from ironbench.runner import run_task
from ironbench.tasks import load_task

# Fake device: behaves like the golden mqtt-device - publishes a counter to
# dev1/value, applies "add <n>" from dev1/cmd, prints markers to serial.
FAKE_DEVICE = textwrap.dedent(
    """
    import sys, time
    import paho.mqtt.client as mqtt

    port = int(sys.argv[1])
    state = {"offset": 0, "n": 0}

    def on_msg(_c, _ud, msg):
        parts = msg.payload.decode().split()
        if parts and parts[0] == "add":
            state["offset"] += int(parts[1])
            print("applied", flush=True)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="dev1"
    )
    client.on_message = on_msg
    client.connect("127.0.0.1", port)
    client.loop_start()
    client.subscribe("dev1/cmd")
    print("mqtt ready", flush=True)
    for _ in range(10):
        state["n"] += 1
        client.publish("dev1/value", str(state["n"] + state["offset"]))
        time.sleep(0.2)
    print("mqtt done " + str(state["offset"]), flush=True)
    client.loop_stop()
    """
)


def make_mqtt_task(tmp_path) -> object:
    d = tmp_path / "t"
    d.mkdir()
    text = """
name: fake-mqtt
description: fake
entry: solution.py
target: unix
timeout_sec: 15
mqtt:
  client_id: ironbench-fake
expect:
  - 'mqtt ready'
  - 'mqtt: dev1/value 1\\n'
  - 'applied'
  - 'mqtt: dev1/value 4[0-9]'
  - 'mqtt done 40'
fail:
  - 'Traceback'
stimulus:
  - mqtt-collect: {topic: dev1/value, count: 3, timeout_sec: 15}
  - mqtt-publish: {topic: dev1/cmd, payload: "add 40", retain: true}
  - mqtt-collect: {topic: dev1/value, count: 3, timeout_sec: 15}
  - wait-serial: "mqtt done"
"""
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text("print('noop')\n", encoding="utf-8")
    return load_task(d)


def test_unix_mqtt_roundtrip(tmp_path):
    broker = MqttSimBroker()
    port = broker.start()
    try:
        task = make_mqtt_task(tmp_path)
        device = tmp_path / "device.py"
        device.write_text(FAKE_DEVICE, encoding="utf-8")
        cmd = [sys.executable, str(device), str(port)]
        res = run_task(task, out_dir=tmp_path / "out", unix_cmd=cmd, mqtt_broker=broker)
        assert res.passed, (res.error, res.missed)
        log = res.serial_log.read_text("utf-8")
        assert "mqtt: dev1/value 1" in log
        assert "mqtt: dev1/value 4" in log  # the offset is applied to the publications
        assert "applied" in log
    finally:
        broker.stop()


def test_unix_mqtt_collect_timeout_is_logged_and_fails(tmp_path):
    # the device is silent over MQTT: collect never reaches count, the expects never match
    broker = MqttSimBroker()
    port = broker.start()
    try:
        task = make_mqtt_task(tmp_path)
        silent = tmp_path / "silent.py"
        silent.write_text("print('mqtt ready')\n", encoding="utf-8")
        cmd = [sys.executable, str(silent), str(port)]
        res = run_task(task, out_dir=tmp_path / "out", unix_cmd=cmd, mqtt_broker=broker)
        assert not res.passed
        log = res.serial_log.read_text("utf-8")
        assert "received 0 of 3" in log
    finally:
        broker.stop()


def test_mqtt_task_schema_requires_section(tmp_path):
    from ironbench.tasks import load_task as lt

    d = tmp_path / "bad"
    d.mkdir()
    text = """
name: bad-mqtt
description: fake
entry: solution.py
target: unix
timeout_sec: 5
expect:
  - 'x'
stimulus:
  - mqtt-publish: {topic: a/b, payload: hi}
"""
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text("pass\n", encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="non-empty mqtt section"):
        lt(d)
