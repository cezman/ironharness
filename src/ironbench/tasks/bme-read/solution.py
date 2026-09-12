# BME280 meteo read (golden): one forced-mode measurement per received line.
# Compensation formulas are the proven live-bench ones (tmp/meteo_main.py):
# P4*65536 is added AFTER the /4, humidity result is (v>>12)/1024.
import struct
import time

from machine import Pin, SoftI2C

ADDR = 0x76

i2c = SoftI2C(scl=Pin(22), sda=Pin(21), freq=400000)


def bme_read(ic, a=ADDR):
    ic.writeto_mem(a, 0xF2, bytes([1]))  # humidity oversampling x1
    ic.writeto_mem(a, 0xF4, bytes([39]))  # temp x1, pressure x8, forced mode
    time.sleep(0.15)  # worst-case measurement time of this configuration
    d = ic.readfrom_mem(a, 0xF7, 8)
    p_raw = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
    t_raw = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
    h_raw = (d[6] << 8) | d[7]
    c = ic.readfrom_mem(a, 0x88, 26)
    T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = struct.unpack('<HhhHhhhhhhhh', c[:24])
    H1 = c[25]
    e = ic.readfrom_mem(a, 0xE1, 7)
    H2 = struct.unpack('<h', e[0:2])[0]
    H3 = e[2]
    H4 = (e[3] << 4) | (e[4] & 0x0F)
    if H4 > 2047: H4 -= 4096
    H5 = (e[5] << 4) | (e[4] >> 4)
    if H5 > 2047: H5 -= 4096
    H6 = struct.unpack('<b', e[6:7])[0]
    v1 = ((t_raw >> 3) - (T1 << 1)) * T2 >> 11
    v2 = (((t_raw >> 4) - T1) * ((t_raw >> 4) - T1) >> 12) * T3 >> 14
    tf = v1 + v2
    T = (tf * 5 + 128) >> 8
    f1 = tf / 2.0 - 64000.0
    f2 = (f1 * f1 * P6 / 32768.0 + f1 * P5 * 2.0) / 4.0 + P4 * 65536.0
    f3 = P3 * f1 * f1 / 524288.0
    f1 = (f3 + P2 * f1) / 524288.0
    f1 = (1.0 - f1 / 32768.0) * P1
    P = 0.0
    if f1 != 0:
        P = 1048576.0 - p_raw
        P = (P - f2 / 4096.0) * 6250.0 / f1
        v1 = P9 * P * P / 2147483648.0
        v2 = P * P8 / 32768.0
        P = P + (v1 + v2 + P7) / 16.0
    v = tf - 76800
    v = ((((h_raw << 14) - (H4 << 20) - (H5 * v)) + 16384) >> 15) * ((((((v * H6) >> 10) * (((v * H3) >> 11) + 32768)) >> 10) + 2097152) * H2 + 8192) >> 14
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * H1) >> 4)
    v = max(v, 0)
    v = min(v, 419430400)
    H = (v >> 12) / 1024.0
    return T / 100.0, P / 100.0, H


print('bme ready')
while True:
    if not input().strip():
        continue
    try:
        t, p, h = bme_read(i2c)
        print(f'T={t:.2f} C P={p:.1f} hPa H={h:.1f} %')
    except OSError as e:
        print('BME ERR', e)
