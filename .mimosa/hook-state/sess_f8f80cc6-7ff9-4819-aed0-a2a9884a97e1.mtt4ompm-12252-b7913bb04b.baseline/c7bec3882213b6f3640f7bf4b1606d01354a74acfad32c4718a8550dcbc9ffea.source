# ПИ с conditional integration: пока актюатор в насыщении и ошибка «давит» туда
# же — интегратор стоит; иначе копим. Наивное накопление в насыщении уводит
# перерегулирование далеко за допуск.
KP = 0.1
KI = 0.02
DT = 0.5

integ = 0.0


def control(t, y, setpoint):
    global integ
    err = setpoint - y
    u_unsat = KP * err + integ
    u = min(1.0, max(0.0, u_unsat))
    # интегрируем, если не в насыщении, или ошибка уже вытащит из насыщения
    if (u < 1.0 or err < 0) and (u > 0.0 or err > 0):
        integ += KI * err * DT
    return u
