# Медиана окна 5 гасит одиночные выбросы DHT22; печать — только по заполненному
# окну (на прогреве медиана неполного окна дала бы ложный выброс).
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
        pass  # одиночный сбой чтения не фатален
    if len(window) > 5:
        window.pop(0)
    if len(window) == 5:
        ordered = sorted(window)
        print("med:", ordered[2])
    time.sleep(2.5)
