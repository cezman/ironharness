"""The ops state judge (IH-104).

The judge is the A/B referee: after the agent claims success it inspects
the ACTUAL board state through fresh harness-owned connections and
decides the verdict. Design rules baked in here:

- independence: every check opens its own transport (reset included);
  nothing the agent printed can satisfy a check (the transcript is read
  only by inventory_match, which compares the agent's REPORTED answer
  against a fresh scan of the bus - the board, not the transcript, is
  the source of truth);
- effect over return code (checklist 7c): boot checks wait for runtime
  output, file checks pull bytes back off the device, the REPL check
  round-trips a marker the board must execute;
- containment: one broken check (dead board, protocol error) becomes a
  failed outcome with evidence, never a crash of the whole judge.
"""

from __future__ import annotations

import ast
import collections.abc
import dataclasses
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from io_core.incremental_text import Utf8StreamDecoder
from io_core.mprepl import MpRepl, MpReplError
from io_core.serial_transport import SerialTransport
from ironbench.ops_tasks import OpsCheck, OpsTask

# serial text kept by one check; a flooding board cannot grow a check's
# memory past this (the same cap class as the firmware runner)
_CHECK_TEXT_CAP = 256 * 1024
# the probe splits its own literal (IH-79 lesson): the cooked REPL echoes
# every accepted byte, so an echo-y line editor would satisfy a contiguous
# marker without ever executing the probe - only a real exec prints
# 'JUDGE-OK' contiguously, while the echo shows 'JUDGE-' + 'OK'
_REPL_MARKER = "JUDGE-OK"
_REPL_PROBE = b"print('JUDGE-' + 'OK')\r\n"
_INVENTORY_RE = re.compile(r"INVENTORY:\s*(\{.*\})")


@dataclasses.dataclass(frozen=True)
class CheckOutcome:
    kind: str
    passed: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class JudgeReport:
    passed: bool
    outcomes: tuple[CheckOutcome, ...]

    def failed(self) -> tuple[CheckOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.passed)


TransportFactory = collections.abc.Callable[[], Any]


def serial_factory(port: str) -> TransportFactory:
    """A factory of fresh connections to the board (one per check)."""

    def factory() -> SerialTransport:
        t = SerialTransport(port, timeout=0.5)
        t.open()
        return t

    return factory


class OpsJudge:
    """Runs state checks against the board; pass = every check passed."""

    def __init__(
        self,
        *,
        port: str | None = None,
        transport_factory: TransportFactory | None = None,
        transcript: str = "",
        assets: dict[str, Path] | None = None,
        broker_factory: collections.abc.Callable[[str, int], Any] | None = None,
    ) -> None:
        if (port is None) == (transport_factory is None):
            raise ValueError("exactly one of port/transport_factory is required")
        self._factory = transport_factory or serial_factory(port)  # type: ignore[arg-type]
        self._transcript = transcript
        self._assets = dict(assets or {})
        self._broker_factory = broker_factory

    def run_task(self, task: OpsTask) -> JudgeReport:
        return self.run(task.judge)

    def run(self, checks: tuple[OpsCheck, ...]) -> JudgeReport:
        outcomes = []
        for check in checks:
            runner = getattr(self, f"_check_{check.kind}")
            try:
                outcome = runner(check.params)
            except Exception as e:  # noqa: BLE001 - a judge error is evidence, not a crash
                outcome = CheckOutcome(check.kind, False, f"judge error: {type(e).__name__}: {e}")
            outcomes.append(outcome)
        passed = all(o.passed for o in outcomes)
        return JudgeReport(passed=passed, outcomes=tuple(outcomes))

    # -- transport plumbing -------------------------------------------------

    def _collect(self, seconds: float, *, stop_at: tuple[str, ...] = ()) -> str:
        """Drains the fresh connection for up to `seconds`, returning the
        text. `stop_at` literals end the drain early (boot checks do not
        need to out-wait a board that already said everything)."""
        t = self._factory()
        try:
            t.reset()
            deadline = time.monotonic() + seconds
            dec = Utf8StreamDecoder()
            text = ""
            while time.monotonic() < deadline:
                in_waiting = getattr(t, "in_waiting", None)
                if in_waiting:
                    data = t.read(int(in_waiting))
                else:
                    data = t.read(256)
                if data:
                    text += dec.decode(data)
                    if len(text) >= _CHECK_TEXT_CAP:
                        text = text[:_CHECK_TEXT_CAP]
                        deadline = 0.0  # cap hit: stop ingesting, judge what we have
                    elif stop_at and all(lit in text for lit in stop_at):
                        break
                time.sleep(0.05)
            return text
        finally:
            t.close()

    def _probe(self, command: str) -> str:
        """One raw-REPL round trip on a fresh connection (MpRepl handles the
        mode dance from any board state)."""
        t = self._factory()
        try:
            repl = MpRepl(t)
            repl.enter()
            return repl.exec_(command)
        finally:
            t.close()

    # -- checks ---------------------------------------------------------

    def _check_boot_expect(self, params: dict[str, object]) -> CheckOutcome:
        within = float(params.get("within_sec", 30))
        literals = [str(x) for x in params.get("literals", [])]
        text = self._collect(within, stop_at=tuple(literals))
        missing = [lit for lit in literals if lit not in text]
        if missing:
            return CheckOutcome("boot_expect", False, f"boot output misses {missing!r}")
        if params.get("repl_echo"):
            t = self._factory()
            try:
                t.reset()
                time.sleep(3.0)  # boot quiet
                t.write(_REPL_PROBE)
                deadline = time.monotonic() + 10
                dec = Utf8StreamDecoder()
                echo = ""
                while time.monotonic() < deadline:
                    in_waiting = getattr(t, "in_waiting", None)
                    data = t.read(int(in_waiting)) if in_waiting else t.read(64)
                    if data:
                        echo += dec.decode(data)
                        if _REPL_MARKER in echo:
                            break
                    time.sleep(0.05)
                if _REPL_MARKER not in echo:
                    return CheckOutcome(
                        "boot_expect",
                        False,
                        "board did not execute the REPL probe (no interactive shell?)",
                    )
            finally:
                t.close()
        return CheckOutcome("boot_expect", True, f"all {literals!r} present in boot output")

    def _check_device_file(self, params: dict[str, object]) -> CheckOutcome:
        path = str(params["path"])
        t = self._factory()
        try:
            content = MpRepl(t).get_file(path)
        except MpReplError as e:
            return CheckOutcome("device_file", False, f"{path}: board refused: {e}")
        finally:
            t.close()
        if "asset" in params:
            asset = self._assets.get(str(params["asset"]))
            if asset is None:
                return CheckOutcome("device_file", False, f"asset {params['asset']!r} unresolved")
            expected = asset.read_bytes()
            if content != expected:
                return CheckOutcome(
                    "device_file",
                    False,
                    f"{path}: content mismatch (device {len(content)}B vs golden {len(expected)}B)",
                )
            return CheckOutcome("device_file", True, f"{path} matches the golden bytes")
        pattern = str(params.get("contains", ""))
        if not re.search(pattern, content.decode("utf-8", "replace")):
            return CheckOutcome("device_file", False, f"{path}: no match for {pattern!r}")
        return CheckOutcome("device_file", True, f"{path} matches {pattern!r}")

    def _scan_bus(self, params: dict[str, object]) -> tuple[list[int], list[list[int]]]:
        scl = int(params.get("i2c_scl", 22))
        sda = int(params.get("i2c_sda", 21))
        ow_pin = int(params.get("onewire_pin", 4))
        i2c_out = self._probe(
            f"from machine import SoftI2C, Pin; print(SoftI2C(scl=Pin({scl}), sda=Pin({sda})).scan())"
        )
        i2c = self._parse_brackets(i2c_out)
        # the OneWire pin mirrors the station's own wiring (open-drain with
        # pull-up): a default-configured pin does not scan real DS devices
        onewire_out = self._probe(
            "import onewire, ds18x20; from machine import Pin; "
            f"print([list(r) for r in ds18x20.DS18X20(onewire.OneWire("
            f"Pin({ow_pin}, Pin.OPEN_DRAIN, pull=Pin.PULL_UP))).scan()])"
        )
        return i2c, self._parse_brackets(onewire_out)

    def _check_bus_scan(self, params: dict[str, object]) -> CheckOutcome:
        i2c, onewire = self._scan_bus(params)
        details = [f"i2c={i2c}", f"onewire_roms={onewire}"]
        if "i2c_expected" in params:
            expected = sorted(int(x) for x in params["i2c_expected"])  # type: ignore[arg-type]
            if sorted(i2c) != expected:
                return CheckOutcome("bus_scan", False, f"i2c {sorted(i2c)} != expected {expected}")
        if "onewire_family" in params:
            family = int(params["onewire_family"])  # type: ignore[arg-type]
            if not onewire or onewire[0][0] != family:
                return CheckOutcome(
                    "bus_scan", False, f"onewire family {family} not found in {onewire}"
                )
        return CheckOutcome("bus_scan", True, "; ".join(details))

    def _check_inventory_match(self, params: dict[str, object]) -> CheckOutcome:
        matches = _INVENTORY_RE.findall(self._transcript)
        if not matches:
            return CheckOutcome(
                "inventory_match", False, "transcript has no INVENTORY: {...} line"
            )
        try:
            reported = json.loads(matches[-1])
        except json.JSONDecodeError as e:
            return CheckOutcome("inventory_match", False, f"INVENTORY is not JSON: {e}")
        if not isinstance(reported, dict):
            return CheckOutcome("inventory_match", False, "INVENTORY must be a JSON object")
        i2c, onewire = self._scan_bus(params)
        problems = []
        if params.get("i2c", True):
            got = reported.get("i2c")
            if not isinstance(got, list) or sorted(int(x) for x in got) != sorted(i2c):
                problems.append(f"i2c reported {got!r}, bus has {sorted(i2c)!r}")
        if params.get("onewire", False):
            got_family = reported.get("onewire_family")
            actual = onewire[0][0] if onewire else None
            if got_family != actual:
                problems.append(
                    f"onewire_family reported {got_family!r}, bus has {actual!r}"
                )
        if problems:
            return CheckOutcome("inventory_match", False, "; ".join(problems))
        return CheckOutcome("inventory_match", True, "reported inventory matches the bus")

    def _check_mqtt_collect(self, params: dict[str, object]) -> CheckOutcome:
        host = params.get("host") or os.environ.get("OPS_AB_MQTT_HOST")
        port = params.get("port") or os.environ.get("OPS_AB_MQTT_PORT")
        if not host or not port:
            raise ValueError("mqtt_collect: host/port params or OPS_AB_MQTT_HOST/PORT required")
        if self._broker_factory is None:
            from io_core.mqtt_transport import MqttTransport

            def broker_factory(h: str, p: int) -> Any:
                return MqttTransport(h, port=p, client_id="ops-judge")

        else:
            broker_factory = self._broker_factory
        pattern = re.compile(str(params["expect_regex"]))
        within = float(params.get("within_sec", 60))
        broker = broker_factory(str(host), int(port))
        try:
            broker.open()
            broker.subscribe(str(params["topic"]))
            deadline = time.monotonic() + within
            seen = 0
            while time.monotonic() < deadline:
                msg = broker.read_message(timeout=1.0)
                if msg is None:
                    continue
                seen += 1
                if pattern.search(str(msg.get("payload", ""))):
                    return CheckOutcome(
                        "mqtt_collect", True, f"matched after {seen} message(s) on {params['topic']}"
                    )
            return CheckOutcome(
                "mqtt_collect", False, f"{seen} message(s) in {within}s, none matched"
            )
        finally:
            with_context = getattr(broker, "close", None)
            if with_context:
                with_context()

    @staticmethod
    def _parse_brackets(text: str) -> list[Any]:
        """The last list literal printed by the board, parsed defensively.
        Nested lists (OneWire ROM dumps) parse whole; the innermost-bracket
        fallback covers noisy output around the value."""
        cleaned = text.strip().lstrip(">").strip()
        for candidate in (cleaned, *reversed(re.findall(r"\[[^\[\]]*\]", text))):
            try:
                value = ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, list):
                return value
        raise ValueError(f"no parsable list in board output: {text[-200:]!r}")
