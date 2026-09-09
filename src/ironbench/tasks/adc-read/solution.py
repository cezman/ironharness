# ADC on GPIO34: full range (ATTN_11DB), a linear raw->millivolts scale
# over the reference points 0->0 and 4095->3300.
import time

from machine import ADC, Pin

adc = ADC(Pin(34))
adc.atten(ADC.ATTN_11DB)

while True:
    raw = adc.read()
    print("mv:", raw * 3300 // 4095)
    time.sleep(0.3)
