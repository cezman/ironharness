# The deployed controller: the relay does not hold its state in the comfort band.
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
    else:
        heater = False
    state = "on" if heater else "off"
    print(f"k={k} heater={state}")
