# UART echo: reads lines from serial and prints "echo: <line>" for each.
# The banner is glued from two strings so the echo of the pasted source does not match expect.
print("echo " + "ready")
while True:
    line = input()
    print("echo: " + line.strip())
