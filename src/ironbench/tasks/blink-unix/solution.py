import time

from machine import Pin

pin = Pin(2, Pin.OUT)
for _ in range(10):
    pin.toggle()
    time.sleep(0.5)
