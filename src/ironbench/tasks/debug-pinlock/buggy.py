# The deployed pad: the alarm can trip even right after a successful unlock.
PIN = "4829"

print("pad ready")
state = "locked"
strikes = 0

while True:
    line = input()
    parts = line.split()
    if not parts:
        continue
    if parts[0] == "code" and len(parts) == 2:
        if parts[1] == PIN:
            print("unlock", parts[1])
            state = "unlocked"
        else:
            print("deny", parts[1])
            if state == "locked":
                strikes += 1
                if strikes == 3:
                    print("alarm")
    elif parts[0] == "lock":
        print("locked")
        state = "locked"
