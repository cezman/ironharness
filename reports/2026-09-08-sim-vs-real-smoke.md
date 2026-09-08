# Sim-vs-real smoke: uart-echo (2026-09-08)

Первый прогон одной и той же золотой задачи на симуляторе и на живой плате
(этап 3, IH-2/IH-3). Это smoke харнесса: эталонное решение, без LLM-агента —
кампания pass@k sim-vs-real ждёт локальный LLM-эндпоинт.

| target | команда | результат | время |
|--------|---------|-----------|-------|
| unix (MicroPython unix-port, WSL2) | `ironbench run --task uart-echo --target unix` | **PASS** | 7.9 s |
| real (ESP32 Wroom-32 + CH340, COM4, MicroPython v1.27) | `IRONBENCH_REAL_PORT=COM4 ironbench run --task uart-echo --target real` | **PASS** | 20.9 s |

## Что подтверждено

- Один и тот же entry (`uart-echo/solution.py`: баннер + бесконечный input()-эхо)
  проходит критерии `task.yaml` на обеих мишенях без изменений кода.
- Накладные расходы real: ~13 s сверх симуляции — staging построчным paste,
  boot-паузы после сброса порта, soft reset (гигиена main.py).

## Грабли CH340/REPL, закрытые в realhw.py (важно для будущих real-мишеней)

1. Конкурентные read+write из разных потоков кладут драйвер CH340 (segfault) —
   линк однопоточный: pump-чтение только между шагами, записи дозированы
   (24 байта / 40 мс).
2. Заливка кода одной пачкой бьётся (на плату приходят NUL) — построчно,
   chunk 24 B + 40 мс.
3. `input()` терминируется только `\r` (`\n` молчит) — стимулы нормализуются
   к `\r` (зеркально unix-мишени, где нужен `\n`).
4. Paste mode подтверждается баннером; перед прогоном сносится чужой `main.py`
   + soft reset; буфер вывода чистится после staging — в оценку идёт только
   сам прогон (boot-мусор и чужие traceback'ы не фейлят задачу).

## Следующий шаг

`ironbench solve --task uart-echo --target {unix,real} --attempts N` на
локальном LLM (LLM_BASE_URL/LLM_MODEL) → pass@k сим против железа — это и есть
кампания sim-vs-real для анонса.
