# П-регулятор: e_ss = (r-ambient)/(1+K·Kp) <= 2 требует Kp >= 44/120 ≈ 0.37;
# берём с запасом — насыщение u=1 держит разгон на пределе, потом линейная зона.
GAIN = 0.6


def control(t, y, setpoint):
    return GAIN * (setpoint - y)
