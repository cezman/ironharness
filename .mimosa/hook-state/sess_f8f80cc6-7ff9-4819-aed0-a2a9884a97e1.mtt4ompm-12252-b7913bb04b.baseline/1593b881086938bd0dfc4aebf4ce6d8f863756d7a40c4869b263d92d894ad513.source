# Протокол кадров "payload*checksum": checksum — один символ, XOR-байт всех
# символов payload. Битый кадр (checksum не совпал, нет '*', пустая строка) -> "NAK",
# корректный -> "ACK <payload>". Баннеры склеены из частей: эхо вставленного
# исходника не должно совпадать с expect (см. skill ironbench-task).
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
