# Frames "<id>:<payload>:<xor2>\n": xor2 is the XOR of the payload bytes, 2 hex digits.
# A bad checksum or a bad format -> "nak", otherwise "ack <id>". Retransmission is
# the sender's concern, the firmware just answers honestly.
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
