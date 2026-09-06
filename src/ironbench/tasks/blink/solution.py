import time

from machine import Pin

# Задача blink: светодиод на GPIO2 мигает (0.5 с вкл / 0.5 с выкл),
# каждый ход печатается в serial — по нему раннер и сценарий проверяют прошивку.
led = Pin(2, Pin.OUT)
n = 0
while True:
    led.value(1)
    print(f"blink {n}: on")
    time.sleep(0.5)
    led.value(0)
    print(f"blink {n}: off")
    time.sleep(0.5)
    n += 1
