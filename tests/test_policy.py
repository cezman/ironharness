"""Access policy tests: host allowlist (modbus/mqtt) and enabled kinds (offline).

No network to real hosts: modbus goes through the local sim server, mqtt
through a fake paho client. Environment is shaped via monkeypatch.
"""

from types import SimpleNamespace

import pytest

from io_core import JsonlJournal, ModbusTransport, MqttTransport, Session, read_events
from io_core.errors import PolicyViolation
from io_core.modbus_sim import ModbusSimServer
from io_core.policy import (
    ALL_KINDS,
    ALLOWED_HOSTS_ENV,
    ENABLED_KINDS_ENV,
    AccessPolicy,
    HostAllowlist,
    parse_allowed_hosts,
    parse_enabled_kinds,
)

HOSTS = {"IRONHARNESS_ALLOWED_HOSTS": "127.0.0.1"}
KINDS = {"IRONHARNESS_ENABLED_KINDS": "modbus,mqtt"}


# --- parsing and matching (unit level, env as a plain dict) ---


def test_parse_allowed_hosts_entries_and_whitespace():
    assert parse_allowed_hosts(" broker.lan:1883 , plc.local ") == (
        ("broker.lan", 1883),
        ("plc.local", None),
    )


def test_parse_allowed_hosts_empty_means_unrestricted():
    assert parse_allowed_hosts(None) == ()
    assert parse_allowed_hosts("") == ()
    assert parse_allowed_hosts("  , ") == ()


def test_parse_allowed_hosts_invalid_port_raises():
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        parse_allowed_hosts("broker.lan:notaport")


def test_parse_enabled_kinds_unset_or_empty_enables_all():
    assert parse_enabled_kinds(None) == frozenset(ALL_KINDS)
    assert parse_enabled_kinds("") == frozenset(ALL_KINDS)
    assert parse_enabled_kinds(" , ") == frozenset(ALL_KINDS)


def test_parse_enabled_kinds_subset_and_unknown_raises():
    assert parse_enabled_kinds("modbus, serial") == frozenset({"modbus", "serial"})
    with pytest.raises(ValueError, match="unknown kind"):
        parse_enabled_kinds("modbus,gpio")  # typo must fail loudly


def test_allowlist_bare_host_matches_any_port():
    al = HostAllowlist.parse("broker.lan")
    assert al.restricted
    assert al.allows("broker.lan", 1883)
    assert al.allows("BROKER.LAN", 9999)  # host match is case-insensitive, any port
    assert not al.allows("other.lan", 1883)


def test_allowlist_host_port_entry_is_port_specific():
    al = HostAllowlist.parse("broker.lan:1883")
    assert al.allows("broker.lan", 1883)
    assert not al.allows("broker.lan", 8888)  # right host, wrong port


def test_policy_from_env_reads_both_variables():
    p = AccessPolicy.from_env({**HOSTS, **KINDS})
    assert p.allowed_hosts.restricted
    assert p.enabled_kinds == frozenset({"modbus", "mqtt"})


def test_denial_is_journaled_before_raise():
    events: list[tuple] = []
    p = AccessPolicy.from_env(HOSTS, on_event=lambda k, d: events.append((k, d)))
    with pytest.raises(PolicyViolation):
        p.check_host("evil.example", 502)
    assert events == [
        ("policy_violation", {"rule": "allowed_hosts", "host": "evil.example", "port": 502})
    ]


# --- modbus transport against the local sim server ---


@pytest.fixture()
def sim():
    with ModbusSimServer(port=0, registers=[7, 8, 9] + [0] * 61) as srv:
        yield srv


def test_modbus_unrestricted_without_env(sim, monkeypatch):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)
    with ModbusTransport("127.0.0.1", port=sim.port) as t:
        assert t.read_holding(0, 3) == [7, 8, 9]


def test_modbus_allows_listed_host(sim, monkeypatch, tmp_path):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "sim.example:502, 127.0.0.1")
    with JsonlJournal(tmp_path / "j.jsonl", actor="test") as jr, ModbusTransport(
        "127.0.0.1", port=sim.port, on_event=jr
    ) as t:
        assert t.read_holding(0) == [7]


def test_modbus_denies_unlisted_host(sim, monkeypatch, tmp_path):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "sim.example")
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t = ModbusTransport("127.0.0.1", port=sim.port, on_event=jr)
        with pytest.raises(PolicyViolation, match="ALLOWED_HOSTS"):
            t.open()
    kinds = [e["kind"] for e in read_events(jpath)]
    assert kinds == ["policy_violation"]  # denied before any network attempt


def test_modbus_port_entry_must_match_port(sim, monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, f"127.0.0.1:{sim.port + 1}")
    t = ModbusTransport("127.0.0.1", port=sim.port)
    with pytest.raises(PolicyViolation):
        t.open()


def test_modbus_denied_open_keeps_name_free(monkeypatch, tmp_path):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "plc.lan")
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    try:
        with pytest.raises(PolicyViolation):
            s.modbus_open("m", "127.0.0.1", port=502)
        assert "m" not in s._transports
    finally:
        s.close()


# --- mqtt transport against a fake paho client (no broker) ---


class FakeMqttClient:
    """Minimal stand-in for the paho client surface used by MqttTransport."""

    def __init__(self) -> None:
        self.on_connect = None
        self.connected_to: tuple[str, int] | None = None

    def connect(self, host, port=1883, keepalive=60):
        self.connected_to = (host, port)
        self.on_connect(self, None, None, SimpleNamespace(value=0, is_failure=False), None)

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


def test_mqtt_unrestricted_without_env(monkeypatch):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)
    fake = FakeMqttClient()
    t = MqttTransport("broker.test", timeout=0.5, client_factory=lambda: fake)
    t.open()
    try:
        assert fake.connected_to == ("broker.test", 1883)
    finally:
        t.close()


def test_mqtt_allows_listed_host_and_port(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "broker.test:1883")
    fake = FakeMqttClient()
    t = MqttTransport("broker.test", port=1883, timeout=0.5, client_factory=lambda: fake)
    t.open()
    try:
        assert fake.connected_to == ("broker.test", 1883)
    finally:
        t.close()


def test_mqtt_denies_unlisted_host_without_dialing(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "other.lan")
    made: list[FakeMqttClient] = []

    def factory() -> FakeMqttClient:
        made.append(FakeMqttClient())
        return made[-1]

    t = MqttTransport("broker.test", timeout=0.5, client_factory=factory)
    with pytest.raises(PolicyViolation, match="broker.test"):
        t.open()
    assert not made  # the client (and the socket) was never created


def test_mqtt_port_entry_must_match_port(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "broker.test:8888")
    t = MqttTransport("broker.test", port=1883, timeout=0.5, client_factory=FakeMqttClient)
    with pytest.raises(PolicyViolation):
        t.open()


# --- kinds policy at the session boundary ---


@pytest.fixture()
def session(tmp_path):
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    yield s
    s.close()


def test_kinds_unset_enables_everything(session, monkeypatch):
    monkeypatch.delenv(ENABLED_KINDS_ENV, raising=False)
    session.serial_open("s", "loop://", timeout=0.5)
    session.file_write("f.txt", "data")
    assert session.file_read("f.txt") == "data"


def test_kinds_denies_serial_open_but_allows_modbus(session, monkeypatch, sim):
    monkeypatch.setenv(ENABLED_KINDS_ENV, "modbus")
    with pytest.raises(PolicyViolation, match="serial"):
        session.serial_open("s", "loop://", timeout=0.5)
    session.modbus_open("m", "127.0.0.1", port=sim.port)
    assert session.modbus_read("m", 0) == [7]


def test_kinds_denies_esp_before_any_hardware_or_import(session, monkeypatch):
    monkeypatch.setenv(ENABLED_KINDS_ENV, "serial,modbus,mqtt,file")
    with pytest.raises(PolicyViolation, match="esp"):
        session.esp_flash("COM4", "fw.bin")
    with pytest.raises(PolicyViolation):
        session.esp_erase("COM4")


def test_kinds_denies_file_ops(session, monkeypatch):
    monkeypatch.setenv(ENABLED_KINDS_ENV, "serial")
    with pytest.raises(PolicyViolation):
        session.file_write("f.txt", "data")
    with pytest.raises(PolicyViolation):
        session.file_list()


def test_kinds_denials_are_journaled(session, monkeypatch, tmp_path):
    monkeypatch.setenv(ENABLED_KINDS_ENV, "mqtt")
    with pytest.raises(PolicyViolation):
        session.serial_open("s", "loop://", timeout=0.5)
    events = read_events(tmp_path / "journal.jsonl")
    assert [e["kind"] for e in events] == ["policy_violation"]
    assert events[0]["rule"] == "enabled_kinds"
    assert events[0]["transport"] == "serial"


def test_parse_max_connections():
    from io_core.policy import parse_max_connections

    assert parse_max_connections(None) is None
    assert parse_max_connections("") is None
    assert parse_max_connections(" 3 ") == 3
    assert parse_max_connections("0") == 0
    with pytest.raises(ValueError):
        parse_max_connections("two")
    with pytest.raises(ValueError):
        parse_max_connections("-1")


def test_allowed_hosts_rejects_ipv6_literal():
    # an IPv6 literal would be silently split into a bogus host:port that never
    # matches - rejected loudly instead (IH-15 review follow-up)
    from io_core.policy import parse_allowed_hosts

    with pytest.raises(ValueError, match="IPv6"):
        parse_allowed_hosts("fd00::1")
