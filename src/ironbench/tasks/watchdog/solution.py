# Lines longer than 32 characters are pathological input: a state reset, a
# "wd: reset" report, and continued operation (crashing is not allowed).
print("wd ready")
val = 0

while True:
    line = input()
    if len(line) > 32:
        val = 0
        print("wd: reset")
    elif line == "ping":
        print("pong")
    elif line.startswith("set "):
        val = int(line[4:])
        print("ok", val)
    elif line == "get":
        print("val", val)
