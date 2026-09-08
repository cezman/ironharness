from machine import Pin

# A command protocol over serial: "led on"/"led off" -> "OK", "state?" -> "state: on|off",
# any other line -> "ERR unknown". The LED on GPIO2 mirrors the state.
# The banners are glued from parts: the echo of the pasted source must not match expect.
led = Pin(2, Pin.OUT)
state = False
print("proto " + "v1")
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
        print("ERR " + "unknown")
