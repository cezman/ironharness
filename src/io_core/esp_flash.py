"""ESP32 flashing tools backed by esptool.

image_info() parses a .bin image without hardware (lets an agent inspect an
image before flashing); flash()/erase() talk to a live board — tests replace
the esptool functions with fakes. The flash/erase sequence mirrors the
canonical esptool CLI flow (see connect_esp docs): connect at 115200 ->
run_stub -> change_baud -> attach_flash -> operation. ESPLoader's context
manager closes the port even when the operation fails.

Note: with no board attached connect_esp makes 7 sync attempts — flash/erase
blocks for up to ~30 seconds, then raises ConnectionError.

Classic ESP32: a single image (MicroPython) is flashed at offset 0x1000.
.bin paths are NOT restricted to the file sandbox (images live outside its
root), but every operation — including failures — is journaled with the full
path.

Safety: flash()/erase() modify real hardware and are gated behind the
IRONHARNESS_ALLOW_REAL_FLASH=1 environment variable (opt-in). The gate is
checked before anything else touches the dependency chain, and its denial is
an event in the journal (esp_denied) before the PermissionError - an
unauthorized flashing attempt must leave a trace ("no log = didn't happen"
applies to refusals too). image_info() is offline and needs no guard. esptool
itself is an optional dependency: install the `flash` extra
(pip install 'ironharness[flash]').
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:  # esptool is an optional dependency — see the [flash] extra
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

    _ESPTOOL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via importorskip in tests
    _ESPTOOL_AVAILABLE = False

EventHook = Callable[[str, dict[str, Any]], None]

DEFAULT_BOOTLOADER_OFFSET = 0x1000  # classic ESP32
ALLOW_REAL_FLASH_ENV = "IRONHARNESS_ALLOW_REAL_FLASH"


def _require_esptool() -> None:
    if not _ESPTOOL_AVAILABLE:
        raise ImportError(
            "esptool is not installed — flashing tools need the [flash] extra: "
            "pip install 'ironharness[flash]'"
        )


def _reverse_lookup(table: dict, value: int, fallback: str) -> str:
    return next((name for name, v in table.items() if v == value), fallback)


class EspFlasher:
    def __init__(self, *, chip: str = "esp32", on_event: EventHook | None = None) -> None:
        self._chip = chip
        self._on_event = on_event

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)

    def _require_real_flash_allowed(self, op: str, port: str) -> None:
        """The live-hardware gate. Checked before the esptool availability so
        the refusal does not depend on an optional dependency, and journaled
        before raising: an unauthorized flashing attempt must leave a trace."""
        if os.environ.get(ALLOW_REAL_FLASH_ENV) != "1":
            error = (
                f"{ALLOW_REAL_FLASH_ENV}=1 is required for live flash/erase — "
                "these operations modify real hardware"
            )
            self._emit("esp_denied", {"op": op, "port": port, "error": error})
            raise PermissionError(error)

    # --- offline: image inspection, no board needed ---

    def image_info(self, firmware_path: str | Path) -> dict[str, Any]:
        path = Path(firmware_path)
        if not path.is_file():
            error = f"image not found: {path}"
            self._emit("esp_image_info_failed", {"path": str(path), "error": error})
            raise FileNotFoundError(error)
        _require_esptool()
        img = LoadFirmwareImage(self._chip, str(path))
        segments = [{"addr": hex(s.addr), "size": len(s.data)} for s in img.segments]
        # flash_size_freq: high nibble is size, low nibble is frequency; tables live on the target class
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

    # --- live board ---

    def flash(
        self,
        port: str,
        firmware_path: str | Path,
        *,
        addr: int = DEFAULT_BOOTLOADER_OFFSET,
        baud: int = 921600,
    ) -> str:
        # the gate is the very first thing: even a probe with a bogus path is
        # an unauthorized flash attempt and must leave a trace
        self._require_real_flash_allowed("flash", port)
        path = Path(firmware_path)
        if not path.is_file():
            raise FileNotFoundError(f"image not found: {path}")
        _require_esptool()
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
            raise ConnectionError(f"esp {port}: failed to flash ({e})") from None
        self._emit("esp_flash", {"port": port, "addr": addr, "baud": baud, "path": str(path)})
        return f"ok: {path.name} flashed at 0x{addr:x} ({port})"

    def erase(self, port: str, *, baud: int = 921600) -> str:
        self._require_real_flash_allowed("erase", port)
        _require_esptool()
        try:
            with connect_esp(port=port, chip=self._chip) as esp:
                esp = run_stub(esp)
                esp.change_baud(baud)
                attach_flash(esp)
                erase_flash(esp)
        except (OSError, FatalError) as e:
            self._emit("esp_erase_failed", {"port": port, "baud": baud, "error": str(e)})
            raise ConnectionError(f"esp {port}: failed to erase ({e})") from None
        self._emit("esp_erase", {"port": port, "baud": baud})
        return f"ok: flash erased ({port})"
