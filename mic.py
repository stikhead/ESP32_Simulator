#!/usr/bin/env python3
"""
esp32_fake_bidi.py
- Streams live mic (PCM16LE 16kHz mono) to a websocket server (Flutter app).
- Also listens for JSON messages from the server and displays transcripts,
  simulating the ESP32 OLED output in the terminal.

Usage:
  pip install websocket-client sounddevice numpy
  python esp32_fake_bidi.py ws://<phone-ip>:8889

If you prefer to stream from a WAV file instead of mic, see the earlier wave_streamer script.
"""
import argparse
import threading
import queue
import time
import json
import sys

import numpy as np
import sounddevice as sd
from websocket import create_connection, ABNF, WebSocketException

# CONFIG
SAMPLE_RATE = 16000
SAMPLES_PER_PACKET = 512   # 512 samples -> 1024 bytes
CHANNELS = 1
KEEPALIVE_INTERVAL = 10.0
RECONNECT_BASE = 1.0
RECONNECT_MAX = 30.0
QUEUE_MAX = 1000

# terminal "OLED" layout (approx)
OLED_CHARS_PER_LINE = 21
OLED_LINES = 4

# global state
send_q = queue.Queue(maxsize=QUEUE_MAX)
running = True

# ---------------- mic callback ----------------
def mic_callback(indata, frames, time_info, status):
    # indata is float32 in [-1,1]
    # if status:
        # print status occasionally
        # print("[mic] status:", status, file=sys.stderr)
    if indata.ndim > 1:
        mono = np.mean(indata, axis=1)
    else:
        mono = indata
    int16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
    raw = int16.tobytes()
    try:
        send_q.put_nowait(raw)
    except queue.Full:
        # occasional warning
        if (time.time() % 10) < 0.05:
            print("[mic] send queue full, dropping packet", file=sys.stderr)

# ---------------- ASCII OLED renderer ----------------
def render_ascii_oled(lines):
    """
    Very simple simulated 128x64 OLED rendering to console.
    We show a bordered box with 4 lines and fixed chars per line.
    """
    # normalize & wrap lines to fit chars
    wrapped = []
    for ln in lines:
        s = ln.strip()
        # naive wrap
        pos = 0
        while pos < len(s):
            chunk = s[pos:pos+OLED_CHARS_PER_LINE]
            # try not to break words if possible
            if pos + OLED_CHARS_PER_LINE < len(s):
                last_space = chunk.rfind(' ')
                if last_space > 0:
                    wrapped.append(chunk[:last_space])
                    pos += last_space + 1
                    continue
            wrapped.append(chunk)
            pos += len(chunk)
    # keep last OLED_LINES lines
    wrapped = wrapped[-OLED_LINES:]
    # pad to OLED_LINES
    while len(wrapped) < OLED_LINES:
        wrapped.insert(0, "")

    # draw box
    border = "+" + "-"*(OLED_CHARS_PER_LINE+2) + "+"
    print(border)
    for ln in wrapped:
        # pad ln to OLED_CHARS_PER_LINE
        display_ln = ln.ljust(OLED_CHARS_PER_LINE)[:OLED_CHARS_PER_LINE]
        print("| " + display_ln + " |")
    print(border)

# ---------------- websocket sender/receiver ----------------
def websocket_worker(server_url):
    backoff = RECONNECT_BASE
    last_keepalive = time.time()
    global running

    while running:
        ws = None
        try:
            print(f"[ws] connecting to {server_url} ...")
            ws = create_connection(server_url, timeout=5)
            print("[ws] connected")
            # send init JSON
            init = {
                "type": "init",
                "device": "esp32_fake_bidi",
                "sampleRate": SAMPLE_RATE,
                "channels": CHANNELS,
                "bits": 16,
                "samplesPerPacket": SAMPLES_PER_PACKET
            }
            ws.send(json.dumps(init))
            print("[ws] sent init")

            # start receiver thread (reads ws.recv)
            recv_stop = threading.Event()
            receiver = threading.Thread(target=ws_receiver, args=(ws, recv_stop), daemon=True)
            receiver.start()

            backoff = RECONNECT_BASE
            last_keepalive = time.time()

            # sending loop: consume send_q
            while running:
                try:
                    raw = send_q.get(timeout=1.0)
                    if not raw:
                        continue
                    ws.send(raw, ABNF.OPCODE_BINARY)
                    send_q.task_done()
                except queue.Empty:
                    # send keepalive text if needed
                    now = time.time()
                    if now - last_keepalive > KEEPALIVE_INTERVAL:
                        try:
                            ws.send(json.dumps({"type": "KeepAlive"}))
                            last_keepalive = now
                        except Exception as e:
                            print("[ws] keepalive send failed:", e, file=sys.stderr)
                            break
                except WebSocketException as e:
                    print("[ws] websocket send error:", e, file=sys.stderr)
                    break
                except Exception as e:
                    print("[ws] unexpected send error:", e, file=sys.stderr)
                    break

            # stop receiver and close ws
            recv_stop.set()
            try:
                ws.close()
            except Exception:
                pass

        except Exception as e:
            print("[ws] connection failed:", e, file=sys.stderr)

        if not running:
            break
        print(f"[ws] reconnecting in {backoff:.1f}s ...")
        time.sleep(backoff)
        backoff = min(backoff * 2.0, RECONNECT_MAX)

    print("[ws] sender exiting")

def ws_receiver(ws, stop_event):
    """
    Blocking recv loop that parses text messages and displays transcripts.
    """
    print("[ws.recv] receiver started")
    while not stop_event.is_set():
        try:
            msg = ws.recv()
            if not msg:
                continue
            # msg might be bytes or str; websocket-client returns str for text frames
            if isinstance(msg, bytes):
                # try decode
                try:
                    msg = msg.decode('utf-8')
                except Exception:
                    print("[ws.recv] got binary message (ignored)")
                    continue
            # parse possible JSON
            try:
                parsed = json.loads(msg)
            except Exception:
                # raw string fallback
                # print("[ws.recv] text:", msg)
                continue

            # handle control vs transcript messages
            mtype = parsed.get("type")
            if mtype == "KeepAlive":
                # ignore
                # print("[ws.recv] keepalive (ignored)")
                continue
            if mtype == "dg_closed":
                print("[ws.recv] deepgram closed:", parsed)
                continue

            # if it's a Deepgram-style message forwarded by proxy, try extracting transcript
            # prefer 'transcript' type messages
            if mtype == "transcript" and parsed.get("text"):
                txt = parsed.get("text", "")
                lang = parsed.get("lang")
                print("\n[TRANSCRIPT RECEIVED]")
                print("Lang:", lang)
                print(txt)
                print()
                render_ascii_oled([txt])
                continue

            # some proxies/Deepgram send channel.alternatives structures
            # try to find channel.alternatives[0].transcript
            if isinstance(parsed.get("channel"), dict):
                ch = parsed["channel"]
                alts = ch.get("alternatives")
                if isinstance(alts, list) and len(alts) > 0 and isinstance(alts[0], dict):
                    transcript = alts[0].get("transcript")
                    if transcript:
                        print("\n[DG TRANSCRIPT]")
                        print(transcript)
                        print()
                        render_ascii_oled([transcript])
                        continue

            # fallback: pretty-print the JSON
            # print("[ws.recv] JSON:", json.dumps(parsed, indent=2))

        except WebSocketException as e:
            print("[ws.recv] websocket exception:", e, file=sys.stderr)
            break
        except Exception as e:
            print("[ws.recv] unexpected error:", e, file=sys.stderr)
            break
    print("[ws.recv] receiver stopped")

# ---------------- main ----------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('server', help='ws://<phone-ip>:8889 (Flutter server URL)')
    parser.add_argument('--samplerate', type=int, default=SAMPLE_RATE)
    parser.add_argument('--frames', type=int, default=SAMPLES_PER_PACKET)
    parser.add_argument('--device', type=int, default=None)
    args = parser.parse_args()

    server_url = args.server
    sr = args.samplerate
    frames = args.frames

    # mic thread uses sounddevice callback
    sender_thread = threading.Thread(target=websocket_worker, args=(server_url,), daemon=True)
    sender_thread.start()

    try:
        print(f"[mic] opening input stream sr={sr}, frames={frames}")
        with sd.InputStream(samplerate=sr, channels=CHANNELS, blocksize=frames, dtype='float32',
                            callback=mic_callback, device=args.device):
            print("[mic] streaming... press Ctrl+C to stop")
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print("[mic] input stream failed:", e, file=sys.stderr)
    finally:
        global running
        running = False
        # give some time for threads to exit
        time.sleep(0.5)
        print("Shutting down")

if __name__ == '__main__':
    main()
