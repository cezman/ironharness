# Fixed heartbeat reporter: the beat counter lives across requests, not
# inside the loop body.
print("hb ready")

count = 0
while True:
    line = input()
    if not line.strip():
        continue
    count += 1
    print(f"beat {count}")
