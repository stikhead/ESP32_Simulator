#!/usr/bin/env python3
# esp_sim_blocking.py
# Blocking simulator using websocket-client

import time, struct, math, random, json
from websocket import create_connection, ABNF

# <-- set this correctly, no extra spaces
SERVER_URL = "ws://ip:8889"

SAMPLE_RATE = 16000
SAMPLES_PER_PACKET = 512
BYTES_PER_PACKET = SAMPLES_PER_PACKET * 2
INTERVAL_SEC = 0.15

def make_packet(samples_per_packet):
    out = bytearray(samples_per_packet * 2)
    for i in range(samples_per_packet):
        val = int(32767 * 0.2 * math.sin(2*math.pi*(i % 50)/50) + (random.random()-0.5)*2000)
        val = max(-32768, min(32767, val))
        struct.pack_into('<h', out, i*2, val)
    return bytes(out)

def main():
    print("Connecting to", SERVER_URL)
    ws = create_connection(SERVER_URL, timeout=5)
    print("Connected")

    # send init JSON
    init = {"type":"init","sampleRate":SAMPLE_RATE,"channels":1,"bits":16}
    ws.send(json.dumps(init))
    print("Sent init")

    pkt = 0
    try:
        while True:
            pkt += 1
            data = make_packet(SAMPLES_PER_PACKET)
            # send as binary using ABNF opcode
            ws.send(data, ABNF.OPCODE_BINARY)
            if pkt % 20 == 0:
                print(f"sent packet #{pkt} ({len(data)} bytes)")
            # try read ack without blocking (short timeout)
            ws.settimeout(0.05)
            try:
                msg = ws.recv()
                if msg:
                    print("recv:", msg)
            except Exception:
                pass
            time.sleep(INTERVAL_SEC)
    except KeyboardInterrupt:
        print("Stopping")
    finally:
        ws.close()

if __name__ == "__main__":
    main()
