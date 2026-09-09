# Frames "#<id>:<payload>:<xor2>\n"; xor2 is the XOR of the payload bytes, 2 hex digits.
# The line tears and glues frames: resynchronize on '#' - segments up to the next
# '#' or the end of the line; a line without '#' - one nak.
while True:
    rest = input()
    started = False
    while True:
        i = rest.find("#")
        if i < 0:
            if not started:
                print("nak")
            break
        started = True
        rest = rest[i + 1 :]
        seg = rest.split("#")[0]
        parts = seg.split(":")
        ok = len(parts) == 3
        if ok:
            fid, payload, x2 = parts
            x = 0
            for ch in payload:
                x ^= ord(ch)
            ok = f"{x:02x}" == x2
        print(("ack " + parts[0]) if ok else "nak")
        rest = rest[len(seg) :]
