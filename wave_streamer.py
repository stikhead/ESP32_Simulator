#!/usr/bin/env python3
# wave_streamer.py
# Stream a PCM16LE mono 16kHz WAV to a WebSocket server as binary frames.

import wave
import time
import argparse
import struct
from websocket import create_connection, ABNF

def stream_wav_to_ws(wav_path, server_url, frames_per_packet=512, interval_sec=None):
    # frames_per_packet = number of samples per packet (512 => 1024 bytes)
    wf = wave.open(wav_path, 'rb')
    nch = wf.getnchannels()
    sampwidth = wf.getsampwidth()
    sr = wf.getframerate()
    assert sampwidth == 2, "WAV must be 16-bit"
    assert nch == 1, "WAV must be mono"
    print(f"WAV: {wav_path} sr={sr} ch={nch} sampwidth={sampwidth}")

    if interval_sec is None:
        # default interval ~= frames_per_packet / sr
        interval_sec = frames_per_packet / float(sr)

    ws = create_connection(server_url, timeout=5)
    print("Connected to", server_url)

    # send init JSON
    init = {"type":"init","sampleRate":sr,"channels":nch,"bits":sampwidth*8}
    ws.send(str(init).replace("'", '"'))
    print("Sent init", init)

    packet_count = 0
    try:
        while True:
            raw = wf.readframes(frames_per_packet)
            if not raw:
                print("End of file;")
                break
            # ensure we send exact bytes; send as binary op
            ws.send(raw, ABNF.OPCODE_BINARY)
            packet_count += 1
            if packet_count % 50 == 0:
                print(f"sent packet #{packet_count}")
            # try to receive ack non-blocking
            ws.settimeout(0.01)
            try:
                msg = ws.recv()
                if msg:
                    print("recv:", msg)
            except Exception:
                pass
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        print("stopped by user")
    finally:
        ws.close()
        wf.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('wav', help='path to 16-bit mono WAV (16000 Hz preferred)')
    parser.add_argument('server', help='ws://<phone-ip>:8889 (Flutter server URL)')
    parser.add_argument('--frames', type=int, default=512, help='samples per packet')
    parser.add_argument('--interval', type=float, default=None, help='seconds between packets (default = frames/sr)')
    args = parser.parse_args()
    stream_wav_to_ws(args.wav, args.server, frames_per_packet=args.frames, interval_sec=args.interval)
