"""Access policy for io-core transports: host allowlist and enabled kinds.

Two environment variables shape the policy, evaluated at the Session/transport
boundary right before an operation reaches the outside world:

- IRONHARNESS_ALLOWED_HOSTS — comma-separated exact hosts or host:port entries
  (e.g. "broker.lan:1883, plc.local"). Unset or empty means every host is
  allowed (backward compatible). When set, modbus/mqtt connections to any other
  host are denied with PolicyViolation; "broker.lan:1883" matches that exact
  host and port, a bare "broker.lan" matches the host on any port.
- IRONHARNESS_ENABLED_KINDS — comma-separated subset of serial,modbus,mqtt,esp,
  file. Unset or empty means all kinds are enabled; a disabled kind makes the
  Session open methods, esp_* and file_* methods raise PolicyViolation.
- IRONHARNESS_MAX_CONNECTIONS — ceiling on simultaneously open transports per
  Session (unset = unlimited). A denial is a PolicyViolation, journaled like
  every other policy denial.

Every denial is reported through the on_event hook as a "policy_violation"
event before the exception is raised — the "no log = didn't happen" convention
applies to denied operations too.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from io_core.errors import PolicyViolation

EventHook = Callable[[str, dict[str, Any]], None]

ALLOWED_HOSTS_ENV = "IRONHARNESS_ALLOWED_HOSTS"
ENABLED_KINDS_ENV = "IRONHARNESS_ENABLED_KINDS"
MAX_CONNECTIONS_ENV = "IRONHARNESS_MAX_CONNECTIONS"

ALL_KINDS: tuple[str, ...] = ("serial", "modbus", "mqtt", "esp", "file")


def parse_max_connections(raw: str | None) -> int | None:
    """Parses IRONHARNESS_MAX_CONNECTIONS into a limit (None/empty = unlimited).

    A non-numeric or negative value raises ValueError: a broken limit must fail
    loudly, not silently disable itself (same philosophy as parse_enabled_kinds).
    """
    if raw is None or not raw.strip():
        return None
    n = int(raw)
    if n < 0:
        raise ValueError(f"{MAX_CONNECTIONS_ENV} must be >= 0, got {raw!r}")
    return n


def parse_allowed_hosts(raw: str | None) -> tuple[tuple[str, int | None], ...]:
    """Parses IRONHARNESS_ALLOWED_HOSTS into (host, port) entries.

    "broker.lan:1883" -> ("broker.lan", 1883); a bare "broker.lan" ->
    ("broker.lan", None) — matches any port. None/empty -> () (unrestricted).
    Whitespace around entries is ignored; an invalid port raises ValueError.
    """
    entries: list[tuple[str, int | None]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        host, sep, port = part.rpartition(":")
        if not sep:
            entries.append((part.casefold(), None))
            continue
        if not port.isdigit():
            raise ValueError(f"invalid port in {ALLOWED_HOSTS_ENV} entry {part!r}")
        entries.append((host.strip().casefold(), int(port)))
    return tuple(entries)


def parse_enabled_kinds(raw: str | None) -> frozenset[str]:
    """Parses IRONHARNESS_ENABLED_KINDS into the set of enabled kinds.

    None/empty -> all kinds enabled. Unknown names raise ValueError: a typo
    must not silently disable (or enable) a transport.
    """
    if raw is None or not raw.strip():
        return frozenset(ALL_KINDS)
    kinds: set[str] = set()
    for part in raw.split(","):
        part = part.strip().casefold()
        if not part:
            continue
        if part not in ALL_KINDS:
            known = ", ".join(ALL_KINDS)
            raise ValueError(f"unknown kind {part!r} in {ENABLED_KINDS_ENV}; known kinds: {known}")
        kinds.add(part)
    return frozenset(kinds) if kinds else frozenset(ALL_KINDS)


class HostAllowlist:
    """Exact host/port allowlist for TCP-based transports.

    An empty allowlist is unrestricted (allow-all, backward compatible);
    a non-empty one denies every host:port pair that is not listed.
    """

    def __init__(self, entries: Iterable[tuple[str, int | None]] = ()) -> None:
        self._entries = tuple(entries)

    @classmethod
    def parse(cls, raw: str | None) -> HostAllowlist:
        return cls(parse_allowed_hosts(raw))

    @property
    def restricted(self) -> bool:
        """True when at least one entry is listed (deny-by-default mode)."""
        return bool(self._entries)

    def allows(self, host: str, port: int) -> bool:
        """Exact match: host is case-insensitive; port must match unless the entry is bare."""
        key = host.casefold()
        return any(h == key and p in (None, port) for h, p in self._entries)


class AccessPolicy:
    """Policy checks applied before an operation reaches the outside world.

    Built from the environment (from_env) at each session/transport open, so
    env changes apply to the next operation without a process restart.
    """

    def __init__(
        self,
        allowed_hosts: HostAllowlist | None = None,
        enabled_kinds: frozenset[str] | None = None,
        *,
        on_event: EventHook | None = None,
    ) -> None:
        self.allowed_hosts = allowed_hosts if allowed_hosts is not None else HostAllowlist()
        self.enabled_kinds = enabled_kinds if enabled_kinds is not None else frozenset(ALL_KINDS)
        self._on_event = on_event

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        on_event: EventHook | None = None,
    ) -> AccessPolicy:
        env = os.environ if environ is None else environ
        return cls(
            HostAllowlist(parse_allowed_hosts(env.get(ALLOWED_HOSTS_ENV))),
            parse_enabled_kinds(env.get(ENABLED_KINDS_ENV)),
            on_event=on_event,
        )

    def check_host(self, host: str, port: int) -> None:
        """Host allowlist gate for TCP-based transports (modbus, mqtt)."""
        if not self.allowed_hosts.restricted or self.allowed_hosts.allows(host, port):
            return
        self._deny(
            f"host {host}:{port} is not in {ALLOWED_HOSTS_ENV}",
            rule="allowed_hosts",
            host=host,
            port=port,
        )

    def check_kind(self, kind: str) -> None:
        """Enabled-kinds gate for session open/esp/file operations."""
        if kind in self.enabled_kinds:
            return
        # detail key is "transport": JsonlJournal merges data over the record,
        # so a "kind" key here would clobber the event type "policy_violation"
        self._deny(
            f"transport kind {kind!r} is disabled by {ENABLED_KINDS_ENV}",
            rule="enabled_kinds",
            transport=kind,
        )

    def _deny(self, message: str, **details: Any) -> None:
        if self._on_event is not None:
            self._on_event("policy_violation", details)
        raise PolicyViolation(message)
