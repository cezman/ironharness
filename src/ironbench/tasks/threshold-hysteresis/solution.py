# An alarm with 30/28 hysteresis: on at t >= 30, off at t <= 28;
# inside the band the state holds; print on every reading.
import time

import dht
from machine import Pin

sensor = dht.DHT22(Pin(4))
alarm = False

while True:
    try:
        t = sensor.temperature()
        if not alarm and t >= 30:
            alarm = True
        elif alarm and t <= 28:
            alarm = False
    except OSError:
        pass  # a single read failure is not fatal
    print("alarm:", "on" if alarm else "off")
    time.sleep(2.5)
