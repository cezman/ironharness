# The "payload*checksum" frame protocol: checksum is a single character, the XOR byte
# of all payload characters. A broken frame (checksum mismatch, no '*', an empty line)
# -> "NAK", a correct one -> "ACK <payload>". The banners are glued from parts: the
# echo of the pasted source must not match expect (see the ironbench-task skill).
print("proto " + "checksum")
while True:
    line = input().strip()
    payload, sep, expected = line.rpartition("*")
    if not sep or not payload:
        print("N" + "AK")
        continue
    x = 0
    for ch in payload:
        x ^= ord(ch)
    if chr(x) == expected:
        print("ACK " + payload)
    else:
        print("N" + "AK")
