# ops-демо «wipe + запись метеостанции: MCP vs руками» (2026-09-20)

Стенд: ESP32 (классический), MicroPython v1.27.0, COM6 (CH340, loc 1-4).
Метеостанция: BME280 по I2C (адрес 118 = 0x76, SCL=22/SDA=21), DS18B20 на GPIO4
(OneWire; на момент демо не отвечает — ROMS []), SSD1306 OLED 0x3C физически
отвалился от шины (известное состояние стенда: main.py падает на line 89 с
ENODEV — это и есть подпись «станция установлена, дисплея нет»).

Одна и та же работа двумя дорогами. Сценарий (5 шагов):

0. Префлайт: опознать порт по баннеру (не по памяти!), забэкапить main.py,
   boot.py, smoke_test.py с платы, скан шины, sha256 прошивки.
1. WIPE — полное стирание flash (esptool erase-flash / MCP esp_erase).
2. FLASH — MicroPython v1.27.0 с пина репозитория
   (.ironbench/blink/ESP32_GENERIC-20251209-v1.27.0.bin, 1 759 456 байт,
   sha256 aa4be80e…62136b9f) на 0x1000 (MCP esp_flash).
3. STATION — записать main.py (метеостанция, 3787 байт) в файловую систему
   платы: raw-REPL/паста руками / MCP serial_put.
4. VERIFY — сброс + проверка подписи загрузки (traceback line 89 ENODEV =
   станция на месте; METEO BOOT не печатается, пока OLED не на шине);
   на MCP-дороге дополнительно round-trip serial_get и побайтовое сравнение.

## Дорожка A — голые руки (tmp/ops-demo/road-a/road_a.py)

Сырые команды: esptool CLI (erase-flash, write-flash) + собственный
минимальный raw-REPL клиент на pyserial (~60 строк: танец входа, hex-чанки,
парсинг ответа). Ни журнала, ни гейтов, ни капов — каждая проверка написана
руками в скрипте. Транскрипты — logs/NN-*.log.

## Дорожка B — MCP (tmp/ops-demo/road-b/mcp_ops.py)

Выделенный MCP-сервер (subprocess, IRONHARNESS_HOME=tmp/ops-demo/mcp-home),
клиент говорит line-delimited JSON-RPC (тот же паттерн, что
tests/test_mcp_stdio.py). Два прохода в одном транскрипте:

- NEG: без IRONHARNESS_ALLOW_REAL_FLASH вызов esp_erase ОБЯЗАН быть
  отклонён и записан в журнал (esp_denied);
- POS: с флагом — serial_open → reset → чтение подписи → serial_close →
  esp_erase → esp_flash → file_write → serial_put → reset → чтение
  подписи → serial_get round-trip → побайтовое сравнение.

Журнал mcp-home/journal.jsonl — доказательство «кто/когда/что/ответ» для
каждого шага; в транскрипт дампится хвост событий.

## Критерии успеха

- Обе дороги: erase ok → flash ok → main.py побайтово на плате → подпись
  загрузки line 89 ENODEV.
- Дорожка B, дополнительно: NEG-отказ до флага, журнал со всеми шагами.
- Конечное состояние платы: meteo main.py на месте, плата перезапущена
  (serial_reset). Никогда не оставлять плату без main.py.
