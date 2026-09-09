# Cooperative scheduler: the nearest deadline, a short sleep between duties;
# a deadline is counted from the previous firing (drift does not accumulate).
import time

A_PERIOD = 500  # ms
B_PERIOD = 1200
HORIZON = 6000

start = time.ticks_ms()
next_a = time.ticks_add(start, A_PERIOD)
next_b = time.ticks_add(start, B_PERIOD)
a = b = 0

while time.ticks_diff(time.ticks_ms(), start) < HORIZON:
    now = time.ticks_ms()
    if time.ticks_diff(next_a, now) <= 0:
        print("A")
        a += 1
        next_a = time.ticks_add(next_a, A_PERIOD)
    elif time.ticks_diff(next_b, now) <= 0:
        print("B")
        b += 1
        next_b = time.ticks_add(next_b, B_PERIOD)
    else:
        time.sleep(0.005)

print(f"A={a} B={b} ok")
