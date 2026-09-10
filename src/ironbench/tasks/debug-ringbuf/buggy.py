# The deployed aggregator: the first averages come out far lower than expected.
buf = [0] * 4

while True:
    line = input()
    if not line.startswith("s "):
        continue
    buf.append(int(line[2:]))
    buf.pop(0)
    print("avg", sum(buf) // len(buf))
