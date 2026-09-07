# Пилот мишени renode: только печать — у закреплённого litex-ELF нет GPIO и ввода.
# Строки склеены из двух частей, чтобы эхо вставленного исходника не совпало с expect.
print("renode " + "alive")
print("no " + "gpio here")
print("bye " + "now")
