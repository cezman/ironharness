# Heater control with hysteresis: on at or below LOW, off at or above HIGH,
# inside the band the previous state holds. Every reading is answered with
# "k=<n> heater=<on|off>" where n is the reading number.
LOW = 18.0
HIGH = 22.0

print("ctl ready")
k = 0
heater = False

while True:
    line = input()
    if not line.startswith("t "):
        continue
    t = float(line[2:])
    k += 1
    if t <= LOW:
        heater = True
    elif t >= HIGH:
        heater = False
    # inside the band: keep the previous state (hysteresis)
    state = "on" if heater else "off"
    print(f"k={k} heater={state}")
