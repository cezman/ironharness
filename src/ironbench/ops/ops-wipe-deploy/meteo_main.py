import framebuf, struct, time
from machine import Pin, SoftI2C
import onewire, ds18x20

i2c = SoftI2C(scl=Pin(22), sda=Pin(21), freq=400000)
ow = onewire.OneWire(Pin(4, Pin.OPEN_DRAIN, pull=Pin.PULL_UP))
ds = ds18x20.DS18X20(ow)
roms = ds.scan()
print('DS ROMS', roms)

class SSD1306_I2C(framebuf.FrameBuffer):
    def __init__(self, width, height, i2c, addr=0x3C):
        self.i2c = i2c
        self.addr = addr
        self.width = width
        self.height = height
        self.pages = height // 8
        self.buffer = bytearray(self.pages * width)
        super().__init__(self.buffer, width, height, framebuf.MONO_VLSB)
        self.init_display()

    def write_cmd(self, cmd):
        self.i2c.writeto(self.addr, bytes([0x80, cmd]))

    def write_data(self, buf):
        self.i2c.writeto(self.addr, bytes([0x40]) + buf)

    def init_display(self):
        self.fill(0)
        for cmd in (
            0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14,
            0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12, 0x81, 0xCF, 0xD9, 0xF1,
            0xDB, 0x40, 0xA4, 0xA6, 0xAF,
        ):
            self.write_cmd(cmd)
        self.show()

    def show(self):
        for page in range(self.pages):
            self.write_cmd(0xB0 | page)
            self.write_cmd(0x00)
            self.write_cmd(0x10)
            start = page * self.width
            self.write_data(self.buffer[start:start + self.width])

def bme_read(ic, a=118):
    ic.writeto_mem(a, 0xF2, bytes([1]))
    ic.writeto_mem(a, 0xF4, bytes([39]))
    time.sleep(0.15)
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
    if v < 0: v = 0
    if v > 419430400: v = 419430400
    H = (v >> 12) / 1024.0
    return T / 100.0, P / 100.0, H

oled = SSD1306_I2C(128, 64, i2c)
print('METEO BOOT')

while True:
    try:
        t, p, h = bme_read(i2c)
        oled.fill(0)
        oled.text('BME280 meteo:', 0, 0)
        oled.text('T %.2f C' % t, 0, 18)
        oled.text('H %.1f %%' % h, 0, 32)
        oled.text('P %d hPa' % int(p), 0, 46)
        if roms:
            ds.convert_temp()
            time.sleep_ms(750)
            tds = ds.read_temp(roms[0])
            oled.text('DS %.1f C' % tds, 0, 56)
            print('DS=%.2f C' % tds)
        oled.show()
        print('T=%.2f C  P=%.1f hPa  H=%.1f %%' % (t, p, h))
    except OSError as e:
        print('BME ERR', e)
        oled.fill(0)
        oled.text('sensor error', 0, 0)
        oled.show()
    time.sleep(10)
