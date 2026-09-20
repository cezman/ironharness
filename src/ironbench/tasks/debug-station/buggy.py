# Deployed temperature logger. Field report: dies at boot before any output.
import struct
import time

from machine import Pin, SoftI2C

ADDR = 0x76

i2c = SoftI2C(scl=Pin(22), sda=Pin(21), freq=400000)
cal = i2c.readfrom_mem(ADDR, 0xE1, 7)
T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = struct.unpack('<HhhHhhhhhhhh', cal)
print('logger ready')

while True:
    if not input().strip():
        continue
    i2c.writeto_mem(ADDR, 0xF4, bytes([39]))
    time.sleep(0.15)
    d = i2c.readfrom_mem(ADDR, 0xF7, 8)
    t_raw = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
    v1 = ((t_raw >> 3) - (T1 << 1)) * T2 >> 11
    v2 = (((t_raw >> 4) - T1) * ((t_raw >> 4) - T1) >> 12) * T3 >> 14
    t = (((v1 + v2) * 5 + 128) >> 8) / 100.0
    print(f'TEMP={t:.2f} C')
