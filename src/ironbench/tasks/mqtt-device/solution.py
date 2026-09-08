# A mini-MQTT client (a 3.1.1 subset, QoS0) on a bare socket. The broker does not
# care that the body length is encoded as a varint in 7-bit groups.
import os
import socket
import struct
import sys
import time

HOST = os.getenv("IRONBENCH_MQTT_HOST", "127.0.0.1")
PORT = int(os.getenv("IRONBENCH_MQTT_PORT", "1883"))

_buf = b""
_sock = socket.socket()


def mq_packet(type_flags, body):
    n = len(body)
    varint = b""
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        varint += bytes([b])
        if not n:
            return bytes([type_flags]) + varint + body


def mq_str(s):
    raw = s.encode()
    return struct.pack(">H", len(raw)) + raw


def read_packet(timeout_ms):
    """One whole MQTT packet from the stream; None on timeout."""
    global _buf
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while True:
        i = 1
        mult = 1
        n = 0
        complete = False
        while i < len(_buf):
            b = _buf[i]
            n += (b & 0x7F) * mult
            mult *= 128
            i += 1
            if not b & 0x80:
                complete = True
                break
        if complete and len(_buf) >= i + n:
            body = _buf[i : i + n]
            ptype = _buf[0]
            _buf = _buf[i + n :]
            return ptype, body
        if time.ticks_diff(time.ticks_ms(), deadline) >= 0:
            return None
        try:
            _sock.settimeout(0.1)
            data = _sock.recv(256)
            if data:
                _buf += data
        except OSError:
            pass


_sock.connect(socket.getaddrinfo(HOST, PORT)[0][-1])
# CONNECT: MQTT 3.1.1, clean session, keepalive 60
_sock.send(mq_packet(0x10, mq_str("MQTT") + bytes([4, 2]) + struct.pack(">H", 60) + mq_str("dev1")))
pkt = read_packet(5000)
if not pkt or pkt[0] != 0x20:
    print("mqtt: no connack")
    sys.exit(1)
# SUBSCRIBE dev1/cmd (qos 0)
_sock.send(mq_packet(0x82, struct.pack(">H", 1) + mq_str("dev1/cmd") + bytes([0])))
pkt = read_packet(5000)
if not pkt or pkt[0] != 0x90:
    print("mqtt: no suback")
    sys.exit(1)
print("mqtt ready")

offset = 0
n = 0
for i in range(12):
    n = i + 1
    _sock.send(mq_packet(0x30, mq_str("dev1/value") + str(n + offset).encode()))
    pkt = read_packet(400)
    if pkt and pkt[0] == 0x30:
        tlen = struct.unpack(">H", pkt[1][:2])[0]
        payload = pkt[1][2 + tlen :].decode()
        parts = payload.split()
        if parts and parts[0] == "add":
            offset += int(parts[1])
            print("applied")
    time.sleep(0.4)
print("mqtt done " + str(offset))
