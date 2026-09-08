# Кадры "<id>:<payload>:<xor2>\n": xor2 — XOR байтов payload, hex из 2 знаков.
# Битая сумма или битый формат → "nak", иначе "ack <id>". Переприём — забота
# отправителя, прошивка только честно отвечает.
while True:
    line = input()
    parts = line.split(":")
    if len(parts) != 3:
        print("nak")
        continue
    fid = parts[0]
    x = 0
    for ch in parts[1]:
        x ^= ord(ch)
    print("ack " + fid if f"{x:02x}" == parts[2] else "nak")
