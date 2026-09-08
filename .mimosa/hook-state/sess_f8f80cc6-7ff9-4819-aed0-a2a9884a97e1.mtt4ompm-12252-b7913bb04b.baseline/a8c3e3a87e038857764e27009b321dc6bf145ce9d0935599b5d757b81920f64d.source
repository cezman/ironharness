"""MQTT транспорт io-core: pub/sub с журналированием (бэклог 2026-09-07, по образцу Modbus).

Клиент поверх paho-mqtt (синхронный API, сетевой цикл paho — в фоновом потоке).
Входящие сообщения копятся в ограниченном буфере: при переполнении теряются самые
старые (для телеметрии устаревшее бесполезно). payload — текст (utf-8): MQTT-трафик
почти всегда текст/JSON; бинарные потоки — не для этого транспорта (есть serial).

Для офлайн-тестов paho-клиент инжектится через client_factory — реальный брокер
не нужен. Каждая операция и каждое входящее сообщение уходят в on_event (журнал).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Self

EventHook = Callable[[str, dict[str, Any]], None]

DEFAULT_QUEUE_SIZE = 256


class MqttTransport:
    def __init__(
        self,
        host: str,
        *,
        port: int = 1883,
        client_id: str = "",
        timeout: float = 3.0,
        keepalive: int = 60,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        on_event: EventHook | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._timeout = timeout
        self._keepalive = keepalive
        self._on_event = on_event
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any = None
        self._connected = threading.Event()
        self._closing = False
        self._inbox: deque[dict[str, str]] = deque(maxlen=queue_size)
        self._inbox_cond = threading.Condition()
        self._suback_cond = threading.Condition()  # mid → коды SUBACK (прийти может раньше, чем начнут ждать)
        self._suback_results: dict[int, list] = {}

    def _default_client_factory(self) -> Any:
        import paho.mqtt.client as mqtt

        return mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=mqtt.MQTTv311,
        )

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)

    # --- колбэки paho (сетевой поток) ---

    def _on_connect(self, _client: Any, _ud: Any, _flags: Any, rc: Any, _props: Any) -> None:
        if getattr(rc, "is_failure", False):  # CONNACK с ошибкой (креды/авторизация)
            return
        self._connected.set()

    def _on_disconnect(self, _client: Any, _ud: Any, _flags: Any, rc: Any, _props: Any) -> None:
        self._connected.clear()
        if not self._closing:  # наш close() журналируется как mqtt_close, без дубля
            self._emit("mqtt_disconnect", {"reason": str(rc)})

    def _on_subscribe(self, _client: Any, _ud: Any, mid: int, codes: list, _props: Any) -> None:
        with self._suback_cond:
            self._suback_results[mid] = list(codes)
            self._suback_cond.notify_all()

    def _on_message(self, _client: Any, _ud: Any, msg: Any) -> None:
        item = {"topic": msg.topic, "payload": msg.payload.decode("utf-8", "replace")}
        with self._inbox_cond:
            self._inbox.append(item)  # deque(maxlen) сам выкидывает самый старый
            self._inbox_cond.notify_all()
        self._emit("mqtt_message", item)

    # --- жизненный цикл ---

    def open(self) -> None:
        client = self._client_factory()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.connect(self._host, port=self._port, keepalive=self._keepalive)
        client.loop_start()
        self._client = client
        if not self._connected.wait(timeout=self._timeout):
            self._teardown()
            raise ConnectionError(
                f"failed to connect to mqtt broker {self._host}:{self._port}"
            )
        self._emit("mqtt_open", {"host": self._host, "port": self._port})

    def _teardown(self) -> None:
        if self._client is None:
            return
        self._closing = True
        self._client.disconnect()
        self._client.loop_stop()
        self._client = None
        self._closing = False

    def close(self) -> None:
        if self._client is None:
            return
        self._teardown()
        self._emit("mqtt_close", {})

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_client(self) -> Any:
        assert self._client is not None, "connection is not open"
        return self._client

    # --- операции ---

    def publish(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        info = self._require_client().publish(
            topic, payload=payload.encode("utf-8"), qos=qos, retain=retain
        )
        if info.rc != 0:
            raise OSError(f"mqtt publish {topic!r}: rc={info.rc}")
        info.wait_for_publish(timeout=self._timeout)
        # wait_for_publish молчит по таймауту: PUBACK не пришёл => публикация не подтверждена
        if not info.is_published():
            raise OSError(f"mqtt publish {topic!r}: not acknowledged within {self._timeout}s")
        self._emit("mqtt_publish", {"topic": topic, "payload": payload, "qos": qos, "retain": retain})

    def subscribe(self, topic: str, *, qos: int = 0) -> None:
        client = self._require_client()
        rc, mid = client.subscribe(topic, qos=qos)
        if rc != 0:
            raise OSError(f"mqtt subscribe {topic!r}: rc={rc}")
        # ждём SUBACK: без него отказ брокера (not authorized, кривой фильтр) был бы тихим «ok»
        deadline = time.monotonic() + self._timeout
        with self._suback_cond:
            while mid not in self._suback_results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError(f"mqtt subscribe {topic!r}: no SUBACK within {self._timeout}s")
                self._suback_cond.wait(remaining)
            codes = self._suback_results.pop(mid)
        if any(getattr(c, "is_failure", False) for c in codes):
            raise OSError(f"mqtt subscribe {topic!r}: broker refused ({[str(c) for c in codes]})")
        self._emit("mqtt_subscribe", {"topic": topic, "qos": qos})

    def read_message(self, timeout: float = 1.0) -> dict[str, str] | None:
        """Следующее входящее сообщение {"topic", "payload"}; None — таймаут."""
        deadline = time.monotonic() + timeout
        with self._inbox_cond:
            while not self._inbox:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._inbox_cond.wait(remaining)
            return self._inbox.popleft()
