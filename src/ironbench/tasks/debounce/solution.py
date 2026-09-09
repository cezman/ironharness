import time

from machine import Pin

# Debounce: a button on GPIO4 (to GND, internal pullup). A press counts
# only if the 0 level holds steady for 30 ms; every clean press is printed.
button = Pin(4, Pin.IN, Pin.PULL_UP)
pressed = False
count = 0
while True:
    if button.value() == 0:
        time.sleep_ms(30)
        if button.value() == 0 and not pressed:
            pressed = True
            count += 1
            print(f"presses: {count}")
    else:
        pressed = False
    time.sleep_ms(5)
