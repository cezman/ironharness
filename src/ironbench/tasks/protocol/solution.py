from machine import Pin

# Командный протокол по serial: "led on"/"led off" -> "OK", "state?" -> "state: on|off",
# любая другая строка -> "ERR unknown". Светодиод на GPIO2 отражает состояние.
led = Pin(2, Pin.OUT)
state = False
print("proto v1")
while True:
    cmd = input().strip()
    if cmd == "led on":
        led.value(1)
        state = True
        print("OK")
    elif cmd == "led off":
        led.value(0)
        state = False
        print("OK")
    elif cmd == "state?":
        print("state: " + ("on" if state else "off"))
    else:
        print("ERR unknown")
