# АЦП на GPIO34: полный диапазон (ATTN_11DB), линейная шкала raw→милливольты
# по опорным точкам 0→0 и 4095→3300.
import time

from machine import ADC, Pin

adc = ADC(Pin(34))
adc.atten(ADC.ATTN_11DB)

while True:
    raw = adc.read()
    print("mv:", raw * 3300 // 4095)
    time.sleep(0.3)
