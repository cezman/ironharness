# Кадры "#<id>:<payload>:<xor2>\n"; xor2 — XOR байтов payload, 2 hex-знака.
# Линия рвёт и склеивает кадры: досинхронизация по '#' — сегменты до следующего
# '#' или конца строки; строка без '#' — один nak.
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
