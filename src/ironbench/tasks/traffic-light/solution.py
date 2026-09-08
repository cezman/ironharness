import time

# A traffic-light FSM: red(3s) -> green(3s) -> yellow(1s) -> red ... in a circle.
# Every state entry prints as "state: <name>", every new cycle - "cycle: N".
lights = (("red", 3), ("green", 3), ("yellow", 1))
i = 0
while True:
    name, seconds = lights[i % 3]
    print(f"state: {name}")
    if name == "red":
        print(f"cycle: {i // 3 + 1}")
    time.sleep(seconds)
    i += 1
