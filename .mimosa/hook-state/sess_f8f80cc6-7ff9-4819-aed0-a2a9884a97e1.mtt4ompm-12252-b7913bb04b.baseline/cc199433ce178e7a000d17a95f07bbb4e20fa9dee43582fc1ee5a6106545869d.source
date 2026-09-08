# Тревога с гистерезисом 30/28: включение при t >= 30, выключение при t <= 28,
# внутри полосы состояние держится; печать на каждом измерении.
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
        pass  # одиночный сбой чтения не фатален
    print("alarm:", "on" if alarm else "off")
    time.sleep(2.5)
