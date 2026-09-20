# Bus-diagnose golden (IH-91): discover the actual I2C bus state against the
# expected device set and report it in the task contract. Degradation is
# handled by report, not by crash: a missing device is only ever observed
# through scan() (an I2C write to an absent address raises OSError ENODEV).
from machine import Pin, SoftI2C

EXPECTED = (0x76, 0x3C)  # BME280, SSD1306 OLED

i2c = SoftI2C(scl=Pin(22), sda=Pin(21), freq=400000)
print('bus ready')
while True:
    if not input().strip():
        continue
    found = i2c.scan()
    missing = [f'0x{a:02X}' for a in EXPECTED if a not in found]
    print('SCAN=' + str(found) + ' MISSING=' + (','.join(missing) if missing else 'none'))
    print('STATUS=' + ('degraded' if missing else 'ok'))
