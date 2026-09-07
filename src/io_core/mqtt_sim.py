"""Мини-брокер MQTT 3.1.1 (подмножество) для офлайн-прогонов и тестов.

Аналог modbus_sim: не продакшн-брокер, а честная симуляция нужного харнессу
подмножества протокола на чистом stdlib — чтобы железо/прошивки можно было
проверять против реального MQTT-клиента без внешних сервисов.

Поддержано: CONNECT/CONNACK, PUBLISH QoS0 (+ QoS1 ack без ретрансмиссий),
retained-сообщения, SUBSCRIBE/SUBACK с масками '+' и '#', PINGREQ/PINGRESP,
DISCONNECT. Не поддержано: QoS2, will, TLS, keepalive-контроль.
"""

from __future__ import annotations

import socket
import threading

(CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT) = (
    1, 2, 3, 4, 8, 9, 12, 13, 14
)


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT-маска: '+' — один уровень, '#' — хвост (только последний уровень)."""
    p_levels = pattern.split("/")
    t_levels = topic.split("/")
    for i, p in enumerate(p_levels):
        if p == "#":
            return i == len(p_levels) - 1
        if i >= len(t_levels):
            return False
        if p != "+" and p != t_levels[i]:
            return False
    return len(p_levels) == len(t_levels)


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("клиент отключился")
        buf.extend(chunk)
    return bytes(buf)


def _read_varint(sock: socket.socket) -> int:
    """Remaining Length из фиксированного заголовка (до 4 байт, 7 бит на байт)."""
    multiplier = 1
    value = 0
    for _ in range(4):
        byte = _read_exact(sock, 1)[0]
        value += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            return value
        multiplier *= 128
    raise ValueError("некорректный Remaining Length (больше 4 байт)")


def _encode_packet(type_flags: int, body: bytes) -> bytes:
    length = len(body)
    varint = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        varint.append(byte)
        if not length:
            break
    return bytes([type_flags]) + bytes(varint) + body


class _ClientConn:
    """Один подключённый клиент: сокет, его подписки, исходящая блокировка."""

    def __init__(self, sock: socket.socket, broker: MqttSimBroker) -> None:
        self.sock = sock
        self.broker = broker
        self.subscriptions: list[str] = []
        self._send_lock = threading.Lock()

    def send(self, packet: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(packet)

    def _reply_connack(self) -> None:
        self.send(bytes([CONNACK << 4, 0x02, 0x00, 0x00]))

    def _handle_publish(self, flags: int, body: bytes) -> None:
        qos = (flags >> 1) & 0b11
        retain = bool(flags & 0x01)
        tlen = int.from_bytes(body[:2], "big")
        topic = body[2 : 2 + tlen].decode("utf-8")
        rest = body[2 + tlen :]
        if qos == 1:
            packet_id = rest[:2]
            rest = rest[2:]
            self.send(_encode_packet(PUBACK << 4, packet_id))
        elif qos != 0:
            return  # QoS2 не поддержан — молча игнорируем
        if retain:
            with self.broker._lock:
                if rest:
                    self.broker.retained[topic] = rest
                else:
                    self.broker.retained.pop(topic, None)  # пустой payload снимает retained
        self.broker._broadcast(topic, rest, exclude=self)

    def _handle_subscribe(self, body: bytes) -> None:
        packet_id = body[:2]
        filters: list[str] = []
        pos = 2
        while pos < len(body):
            tlen = int.from_bytes(body[pos : pos + 2], "big")
            pattern = body[pos + 2 : pos + 2 + tlen].decode("utf-8")
            pos += 2 + tlen + 1  # +1 байт запрошенного qos
            filters.append(pattern)
        self.subscriptions.extend(filters)
        self.send(_encode_packet(SUBACK << 4, packet_id + bytes([0x00] * len(filters))))
        # retained, подходящие новым подпискам, доставляются сразу после SUBACK
        with self.broker._lock:
            retained_now = [(t, p) for t, p in self.broker.retained.items()]
        for topic, payload in retained_now:
            if any(topic_matches(f, topic) for f in filters):
                self.send(_encode_packet(PUBLISH << 4, self._publish_body(topic, payload)))

    @staticmethod
    def _publish_body(topic: str, payload: bytes) -> bytes:
        t = topic.encode("utf-8")
        return len(t).to_bytes(2, "big") + t + payload

    def serve(self) -> None:
        """Цикл разбора пакетов одного клиента (поток accept-цикла).
        CONNACK шлём строго в ответ на CONNECT: безусловный CONNACK при
        подключении путал бы клиентов, ждущих ответа на свою команду."""
        while True:
            type_flags = _read_exact(self.sock, 1)[0]
            length = _read_varint(self.sock)
            body = _read_exact(self.sock, length) if length else b""
            ptype = type_flags >> 4
            if ptype == PUBLISH:
                self._handle_publish(type_flags & 0x0F, body)
            elif ptype == SUBSCRIBE:
                self._handle_subscribe(body)
            elif ptype == PINGREQ:
                self.send(bytes([PINGRESP << 4, 0x00]))
            elif ptype == DISCONNECT:
                return
            # CONNECT после handshake не ждём: клиент один раз подключается
            elif ptype == CONNECT:
                self._reply_connack()


class MqttSimBroker:
    """Мини-брокер: accept-цикл в потоке, клиенты обслуживаются в своих потоках."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self.port: int | None = None  # фактический порт после start()
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._clients: list[_ClientConn] = []
        self.retained: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def start(self) -> int:
        """Поднимает слушатель; возвращает фактический порт (port=0 → эфемерный)."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        self._sock.bind((self._host, self._port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(8)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self.port

    def stop(self) -> None:
        if self._sock is not None:
            with self._lock:
                for client in self._clients:
                    try:
                        client.sock.close()
                    except OSError:
                        pass
                self._clients.clear()
            self._sock.close()
            self._sock = None

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while True:
            try:
                sock, _addr = self._sock.accept()
            except OSError:
                return  # слушатель закрыт (stop)
            client = _ClientConn(sock, self)
            with self._lock:
                self._clients.append(client)
            threading.Thread(target=self._serve_client, args=(client,), daemon=True).start()

    def _serve_client(self, client: _ClientConn) -> None:
        try:
            client.serve()
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            try:
                client.sock.close()
            except OSError:
                pass

    def _broadcast(self, topic: str, payload: bytes, *, exclude: _ClientConn) -> None:
        body = _ClientConn._publish_body(topic, payload)
        with self._lock:
            targets = [c for c in self._clients if c is not exclude]
        for client in targets:
            if any(topic_matches(pattern, topic) for pattern in client.subscriptions):
                try:
                    client.send(_encode_packet(PUBLISH << 4, body))
                except OSError:
                    pass


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="мини-брокер MQTT 3.1.1 (mqtt_sim)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args()
    sim = MqttSimBroker(args.host, args.port)
    port = sim.start()
    print(f"mqtt-sim ready {port}", flush=True)
    try:
        threading.Event().wait()  # навсегда: завершение по SIGTERM/kill
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    sys.exit(0)
