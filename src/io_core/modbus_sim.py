"""Мини-симулятор Modbus TCP: FC3 (чтение holding), FC6 (запись одного), FC16 (запись блока).

Зачем: «sim before real» — цель для тестов транспорта и тренажёр для агентов,
когда реального устройства нет. Держит блок holding-регистров в памяти.
"""

from __future__ import annotations

import socketserver
import struct
import threading
from collections.abc import Sequence
from typing import Self


class _ModbusSimHandler(socketserver.StreamRequestHandler):
    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client closed the connection")
            buf += chunk
        return buf

    def handle(self) -> None:
        while True:
            try:
                tid, pid, length, uid = struct.unpack(">HHHB", self._recv_exact(7))
            except (ConnectionError, OSError):
                return
            if pid != 0:
                return
            try:
                pdu = self._recv_exact(length - 1)
            except (ConnectionError, OSError):
                return
            resp = self._handle_pdu(pdu)
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, uid) + resp)

    def _handle_pdu(self, pdu: bytes) -> bytes:
        regs: list[int] = self.server.registers  # type: ignore[attr-defined]
        fc = pdu[0]
        if fc == 3 and len(pdu) >= 5:  # чтение holding-регистров
            start, qty = struct.unpack(">HH", pdu[1:5])
            if start + qty > len(regs):
                return bytes([0x83, 0x02])
            return bytes([3, qty * 2]) + struct.pack(f">{qty}H", *regs[start : start + qty])
        if fc == 6 and len(pdu) >= 5:  # запись одного регистра (эхо-ответ)
            addr, value = struct.unpack(">HH", pdu[1:5])
            if addr >= len(regs):
                return bytes([0x86, 0x02])
            regs[addr] = value
            return pdu[:5]
        if fc == 16 and len(pdu) >= 6:  # запись блока регистров
            start, qty = struct.unpack(">HH", pdu[1:5])
            if start + qty > len(regs):
                return bytes([0x90, 0x02])
            regs[start : start + qty] = list(struct.unpack(f">{qty}H", pdu[6 : 6 + qty * 2]))
            return struct.pack(">BHH", 16, start, qty)
        return bytes([fc | 0x80, 0x01])  # unsupported function code


class ModbusSimServer:
    """Потоковый TCP-сервер-симулятор. port=0 → выбрать свободный (см. .port)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        registers: Sequence[int] = (0,) * 64,
    ) -> None:
        outer = self

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server: socketserver.ThreadingTCPServer = _Server((host, port), _ModbusSimHandler)
        self._server.registers = list(registers)  # type: ignore[attr-defined]
        self._host = host
        self.port: int = self._server.server_address[1]
        self._thread: threading.Thread | None = None
        assert outer is self

    @property
    def registers(self) -> list[int]:
        return self._server.registers  # type: ignore[attr-defined]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
