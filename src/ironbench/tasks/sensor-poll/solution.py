import time

import dht
from machine import Pin

# Опрос датчика DHT22 на GPIO4: раз в 2 секунды печать температуры "temp: <число>".
sensor = dht.DHT22(Pin(4))
print("dht ready")
while True:
    try:
        sensor.measure()
        print(f"temp: {sensor.temperature()}")
    except OSError:
        print("sensor error")
    time.sleep(2)
