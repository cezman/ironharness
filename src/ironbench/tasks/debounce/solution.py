import time

from machine import Pin

# Антидребезг: кнопка на GPIO4 (на GND, internal pullup). Нажатие засчитываем,
# только если уровень 0 стабильно держится 30 мс; на каждое чистое нажатие — печать.
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
