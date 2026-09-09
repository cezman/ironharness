# A menu: two nested submodes (name input, number input until an empty line),
# handling of an invalid command, a finite exit on "3".
while True:
    print("MENU 1=greet 2=sum 3=quit")
    cmd = input()
    if cmd == "1":
        print("NAME?")
        name = input()
        print("HELLO " + name)
    elif cmd == "2":
        print("NUMS?")
        total = 0
        while True:
            line = input()
            if line == "":
                break
            total += int(line)
        print("SUM=" + str(total))
    elif cmd == "3":
        break
    else:
        print("ERR")
print("BYE")
