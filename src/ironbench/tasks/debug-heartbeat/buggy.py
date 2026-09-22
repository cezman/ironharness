# Deployed heartbeat reporter. Field report: dies answering the first request.
print("hb ready")

while True:
    line = input()
    if not line.strip():
        continue
    count += 1
    print(f"beat {count}")
