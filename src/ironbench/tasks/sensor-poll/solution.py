import time

import dht
from machine import Pin

# Polling a DHT22 sensor on GPIO4: every 2 seconds print the temperature "temp: <number>".
# The banner is glued from two strings so the echo of the pasted source does not match expect.
sensor = dht.DHT22(Pin(4))
print("dht " + "ready")
while True:
    try:
        sensor.measure()
        print(f"temp: {sensor.temperature()}")
    except OSError:
        print("sensor error")
    time.sleep(2)
