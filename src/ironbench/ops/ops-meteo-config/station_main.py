# config probe station: prints the configured station id at boot
import time

try:
    from config import STATION
except ImportError:
    STATION = 'UNCONFIGURED'

print('STATION', STATION)

while True:
    time.sleep(10)
