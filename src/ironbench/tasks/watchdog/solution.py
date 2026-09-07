# Строки длиннее 32 символов — патологический ввод: сброс состояния, отчёт
# "wd: reset" и продолжение работы (падать нельзя).
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
