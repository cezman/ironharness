# A PI with conditional integration: while the actuator is saturated and the error
# "pushes" further into saturation the integrator holds; otherwise it accumulates.
# Naive accumulation in saturation drives the overshoot far past the tolerance.
KP = 0.1
KI = 0.02
DT = 0.5

integ = 0.0


def control(t, y, setpoint):
    global integ
    err = setpoint - y
    u_unsat = KP * err + integ
    u = min(1.0, max(0.0, u_unsat))
    # integrate if not in saturation, or the error already pulls out of saturation
    if (u < 1.0 or err < 0) and (u > 0.0 or err > 0):
        integ += KI * err * DT
    return u
