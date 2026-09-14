# Cross-sensor golden: BME280 (I2C) temperature + DS18B20 (OneWire GPIO4),
# cross-checked. The BME temperature compensation is the proven live-bench
# code (bme-read); only the temperature branch is kept. The DS18B20 uses the
# stock MicroPython driver: scan once, convert_temp + 750 ms, read_temp.
import struct
import time

import ds18x20
import onewire
from machine import Pin, SoftI2C

ADDR = 0x76

i2c = SoftI2C(scl=Pin(22), sda=Pin(21), freq=400000)
ow = onewire.OneWire(Pin(4, Pin.OPEN_DRAIN, pull=Pin.PULL_UP))
ds = ds18x20.DS18X20(ow)
roms = ds.scan()


def bme_temp(ic, a=ADDR):
    ic.writeto_mem(a, 0xF4, bytes([39]))  # temp x1, pressure x8, forced mode
    time.sleep(0.15)  # worst-case measurement time
    d = ic.readfrom_mem(a, 0xF7, 8)
    t_raw = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
    c = ic.readfrom_mem(a, 0x88, 26)
    T1, T2, T3 = struct.unpack('<Hhh', c[:6])
    v1 = ((t_raw >> 3) - (T1 << 1)) * T2 >> 11
    v2 = (((t_raw >> 4) - T1) * ((t_raw >> 4) - T1) >> 12) * T3 >> 14
    return ((v1 + v2) * 5 + 128) >> 8


def ds_read():
    ds.convert_temp()
    time.sleep_ms(750)
    return ds.read_temp(roms[0])


print('sensors ready')
while True:
    if not input().strip():
        continue
    try:
        raw_t = bme_temp(i2c)
        t_bme = raw_t / 100.0
        t_ds = ds_read()
        d = abs(t_bme - t_ds)
        print(f'BME={t_bme:.2f} DS={t_ds:.2f} D={d:.2f}')
    except OSError as e:
        print('BME ERR', e)
