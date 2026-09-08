import time

from machine import Pin

# The blink task: the LED on GPIO2 blinks (0.5 s on / 0.5 s off);
# every turn is printed to serial - the runner and the scenario score the firmware by it.
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
