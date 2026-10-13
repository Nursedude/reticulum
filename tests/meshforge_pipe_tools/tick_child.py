import os, time
while True:
    time.sleep(0.25)
    if not os.read(0, 1 << 20): break
