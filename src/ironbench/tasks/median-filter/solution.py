# The median of a window of 5 suppresses single DHT22 spikes; printing happens
# only for a full window (during warm-up a partial-window median would give a false spike).
import time

import dht
from machine import Pin

sensor = dht.DHT22(Pin(4))
window = []
print("dht ready")

while True:
    try:
        window.append(sensor.temperature())
    except OSError:
        pass  # a single read failure is not fatal
    if len(window) > 5:
        window.pop(0)
    if len(window) == 5:
        ordered = sorted(window)
        print("med:", ordered[2])
    time.sleep(2.5)
