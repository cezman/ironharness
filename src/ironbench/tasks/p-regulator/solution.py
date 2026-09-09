# P-controller: e_ss = (r-ambient)/(1+K*Kp) <= 2 requires Kp >= 44/120 ~= 0.37;
# take a margin - the u=1 saturation keeps the ramp-up at the limit, then the linear zone.
GAIN = 0.6


def control(t, y, setpoint):
    return GAIN * (setpoint - y)
