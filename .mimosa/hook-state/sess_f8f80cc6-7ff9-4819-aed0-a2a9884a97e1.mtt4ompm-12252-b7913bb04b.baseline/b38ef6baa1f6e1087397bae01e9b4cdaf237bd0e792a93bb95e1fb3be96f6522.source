import time

# Конечный автомат светофора: red(3с) → green(3с) → yellow(1с) → red ... по кругу.
# Каждый вход в состояние печатается как "state: <имя>", каждый новый цикл — "cycle: N".
lights = (("red", 3), ("green", 3), ("yellow", 1))
i = 0
while True:
    name, seconds = lights[i % 3]
    print(f"state: {name}")
    if name == "red":
        print(f"cycle: {i // 3 + 1}")
    time.sleep(seconds)
    i += 1
