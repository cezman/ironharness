"""Инструменты прошивки ESP32 через esptool (план 3.3; офлайн-часть этапа 3).

image_info() разбирает .bin-образ без железа (проверка агентом перед прошивкой);
flash()/erase() требуют живую плату — в тестах функции esptool подменяются моками.
Последовательность flash/erase — каноническая для esptool CLI (см. докстринг
connect_esp): connect на 115200 → run_stub → change_baud → attach_flash → операция.
Порт закрывается контекст-менеджером ESPLoader даже при ошибке операции.

Внимание: без платы connect_esp делает 7 попыток синхронизации — вызов
flash/erase блокируется до ~30 секунд, потом ConnectionError.

Классический ESP32: один образ (MicroPython) шьётся по адресу 0x1000.
Пути к .bin не ограничены файловой песочницей (образы лежат вне её корня),
но каждая операция журналируется с полным путём, включая неудачные попытки.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from esptool.cmds import (
    CHIP_DEFS,
    FLASH_MODES,
    FatalError,
    LoadFirmwareImage,
    attach_flash,
    connect_esp,
    erase_flash,
    run_stub,
    write_flash,
)

EventHook = Callable[[str, dict[str, Any]], None]

DEFAULT_BOOTLOADER_OFFSET = 0x1000  # классический ESP32


def _reverse_lookup(table: dict, value: int, fallback: str) -> str:
    return next((name for name, v in table.items() if v == value), fallback)


class EspFlasher:
    def __init__(self, *, chip: str = "esp32", on_event: EventHook | None = None) -> None:
        self._chip = chip
        self._on_event = on_event

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)

    # --- офлайн: разбор образа без платы ---

    def image_info(self, firmware_path: str | Path) -> dict[str, Any]:
        path = Path(firmware_path)
        if not path.is_file():
            raise FileNotFoundError(f"образ не найден: {path}")
        img = LoadFirmwareImage(self._chip, str(path))
        segments = [{"addr": hex(s.addr), "size": len(s.data)} for s in img.segments]
        # flash_size_freq: старший ниббл — размер, младший — частота; таблицы — в классе таргета
        size_nibble = (img.flash_size_freq >> 4) & 0xF
        freq_nibble = img.flash_size_freq & 0xF
        target = CHIP_DEFS[self._chip]
        info = {
            "chip": self._chip,
            "path": str(path),
            "size": path.stat().st_size,
            "entrypoint": hex(img.entrypoint),
            "segments": segments,
            "flash_mode": _reverse_lookup(FLASH_MODES, img.flash_mode, str(img.flash_mode)),
            "flash_size": _reverse_lookup(
                {name: (v >> 4) & 0xF for name, v in target.FLASH_SIZES.items()},
                size_nibble,
                hex(size_nibble),
            ),
            "flash_freq": _reverse_lookup(
                dict(target.FLASH_FREQUENCY), freq_nibble, hex(freq_nibble)
            ),
        }
        self._emit("esp_image_info", info)
        return info

    # --- живая плата ---

    def flash(
        self,
        port: str,
        firmware_path: str | Path,
        *,
        addr: int = DEFAULT_BOOTLOADER_OFFSET,
        baud: int = 921600,
    ) -> str:
        path = Path(firmware_path)
        if not path.is_file():
            raise FileNotFoundError(f"образ не найден: {path}")
        try:
            with connect_esp(port=port, chip=self._chip) as esp:
                esp = run_stub(esp)
                esp.change_baud(baud)
                attach_flash(esp)
                write_flash(esp, [(addr, str(path))])
        except (OSError, FatalError) as e:
            self._emit(
                "esp_flash_failed",
                {"port": port, "addr": addr, "baud": baud, "path": str(path), "error": str(e)},
            )
            raise ConnectionError(f"esp {port}: не удалось прошить ({e})") from None
        self._emit("esp_flash", {"port": port, "addr": addr, "baud": baud, "path": str(path)})
        return f"ok: {path.name} прошит по 0x{addr:x} ({port})"

    def erase(self, port: str, *, baud: int = 921600) -> str:
        try:
            with connect_esp(port=port, chip=self._chip) as esp:
                esp = run_stub(esp)
                esp.change_baud(baud)
                attach_flash(esp)
                erase_flash(esp)
        except (OSError, FatalError) as e:
            self._emit("esp_erase_failed", {"port": port, "baud": baud, "error": str(e)})
            raise ConnectionError(f"esp {port}: не удалось стереть ({e})") from None
        self._emit("esp_erase", {"port": port, "baud": baud})
        return f"ok: флеш стёрт ({port})"
