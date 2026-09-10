# Keeps the last 4 samples in a sliding window; every sample is answered with
# the integer mean of exactly the samples received so far (the window grows up
# to 4 and is never zero-padded).
print("agg ready")
samples = []

while True:
    line = input()
    if not line.startswith("s "):
        continue
    samples.append(int(line[2:]))
    if len(samples) > 4:
        samples.pop(0)
    print("avg", sum(samples) // len(samples))
