#!/usr/bin/env python3
"""
esp32_fake_cam_audio_with_mic.py

Same as the earlier ESP32 fake but with optional --use-mic to stream live laptop microphone audio
as PCM16LE packets.

Usage:
  # webcam frames + synthetic audio (default)
  python esp32_fake_cam_audio_with_mic.py --server ws://IP:8889

  # webcam frames + live mic audio (while recording)
  python esp32_fake_cam_audio_with_mic.py --server ws://IP:8889 --use-mic

  # use WAV file as audio source
  python esp32_fake_cam_audio_with_mic.py --server ws://IP:8889 --wav sample_16k_mono.wav
"""

import argparse
import json
import logging
import time
import threading
import wave
import random
import queue
from datetime import datetime
from pathlib import Path

# websocket-client
from websocket import WebSocketApp, ABNF

# optional OpenCV for webcam frames
try:
    import cv2
except Exception:
    cv2 = None

import numpy as np

# optional microphone (sounddevice)
try:
    import sounddevice as sd
except Exception:
    sd = None

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')


class FakeESP32CamAudio:
    def __init__(self, server_url, frame_interval=0.5, jpeg_quality=70,
                 webcam_index=0, audio_interval=0.1, samples_per_packet=512,
                 sample_rate=16000, wav_path=None, use_mic=False, sine=False, save_frames=False):
        self.server_url = server_url
        self.frame_interval = float(frame_interval)
        self.jpeg_quality = int(jpeg_quality)
        self.webcam_index = int(webcam_index)
        self.audio_interval = float(audio_interval)
        self.samples_per_packet = int(samples_per_packet)
        self.sample_rate = int(sample_rate)
        self.wav_path = Path(wav_path) if wav_path else None
        self.use_mic = bool(use_mic)
        self.use_sine = bool(sine)
        self.save_frames = bool(save_frames)

        self.ws = None
        self.connected = threading.Event()
        self.stop_event = threading.Event()

        self._last_frame_ts = 0.0
        self._last_audio_ts = 0.0
        self._last_keepalive = 0.0

        self.recording = False
        self.record_dir = Path("recordings")
        if self.save_frames:
            self.record_dir.mkdir(exist_ok=True)

        # WAV file support
        self._wav = None
        if self.wav_path:
            if not self.wav_path.exists():
                logging.warning("WAV path does not exist: %s; ignoring wav", self.wav_path)
                self.wav_path = None
            else:
                try:
                    wf = wave.open(str(self.wav_path), 'rb')
                    if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                        logging.warning("WAV must be 16-bit mono for direct streaming. Ignoring wav.")
                        wf.close()
                    else:
                        self._wav = wf
                        if wf.getframerate() != self.sample_rate:
                            logging.warning("WAV SR %d != target %d; packets read raw (timing may differ)",
                                            wf.getframerate(), self.sample_rate)
                except Exception:
                    logging.exception("Failed to open WAV; ignoring")
                    self._wav = None
                    self.wav_path = None

        # Microphone queue: sounddevice callback writes raw bytes here
        self._mic_queue = queue.Queue(maxsize=64) if self.use_mic else None
        self._mic_stream = None

        self._cap = None
        self._backoff = 1.0

        # fallback generator state
        self._sine_phase = 0.0

    # --- WebSocket callbacks ---
    def on_open(self, ws):
        logging.info("[WS] connected")
        self.connected.set()
        self._backoff = 1.0
        # send init JSON
        init = {
            "type": "init",
            "device": "py_esp32_cam_audio_sim",
            "capabilities": {
                "camera": True,
                "cam_width": 320,
                "cam_height": 240,
                "sampleRate": self.sample_rate,
                "channels": 1,
                "bits": 16,
                "samplesPerPacket": self.samples_per_packet
            }
        }
        try:
            ws.send(json.dumps(init))
            logging.info("[WS] Sent init: %s", init)
        except Exception as e:
            logging.warning("[WS] failed to send init: %s", e)

    def on_close(self, ws, code, reason):
        logging.info("[WS] disconnected code=%s reason=%s", code, reason)
        self.connected.clear()

    def on_error(self, ws, err):
        logging.warning("[WS] error: %s", err)
        self.connected.clear()

    def on_message(self, ws, message):
        # message can be string or bytes
        if isinstance(message, (bytes, bytearray)):
            logging.debug("[WS] binary received (len=%d)", len(message))
            return

        logging.info("[WS] TXT recv: %s", message)
        try:
            m = json.loads(message)
            typ = m.get("type")
            if typ == "start_record":
                logging.info("[CMD] start_record -> start audio streaming")
                self.recording = True
            elif typ == "stop_record":
                logging.info("[CMD] stop_record -> stop audio streaming")
                self.recording = False
            elif typ == "KeepAlive":
                logging.debug("[CMD] KeepAlive")
            else:
                logging.info("[MSG] %s", message)
        except Exception:
            logging.info("[MSG raw] %s", message)

    # --- ws helper ---
    def _make_ws(self):
        return WebSocketApp(self.server_url,
                            on_open=self.on_open,
                            on_message=self.on_message,
                            on_error=self.on_error,
                            on_close=self.on_close)

    # --- camera helpers ---
    def _ensure_camera(self):
        if self._cap is None:
            if cv2 is None:
                logging.warning("OpenCV not available; camera frames disabled")
                return False
            self._cap = cv2.VideoCapture(self.webcam_index, cv2.CAP_ANY)
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            time.sleep(0.1)
        return True

    def _capture_jpeg(self):
        if not self._ensure_camera():
            return None
        ret, frame = self._cap.read()
        if not ret:
            logging.warning("webcam read failed")
            return None
        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        if not ok:
            logging.warning("jpeg encode failed")
            return None
        return buf.tobytes()

    # --- audio helpers ---
    def _read_wav_packet(self):
        if not self._wav:
            return None
        frames = self._wav.readframes(self.samples_per_packet)
        if not frames:
            # loop file
            try:
                self._wav.rewind()
                frames = self._wav.readframes(self.samples_per_packet)
                if not frames:
                    return None
            except Exception:
                return None
        return frames

    def _generate_sine_packet(self):
        # synthetic packet: multi-tone + small noise
        t = np.arange(self.samples_per_packet)
        freq = 440.0 + (random.random()-0.5)*20.0
        phase = random.random()*2*np.pi
        sine = 0.2 * np.sin(2*np.pi*freq*t/self.sample_rate + phase)
        noise = (np.random.rand(self.samples_per_packet) - 0.5) * 0.05
        sig = sine + noise
        sig = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
        return sig.tobytes()

    # --- microphone (sounddevice) callback ---
    def _mic_callback(self, indata, frames, time_info, status):
        # indata is numpy array shape (frames, channels); dtype depends on dtype set
        try:
            if status:
                logging.debug("[MIC] status: %s", status)
            # If stereo, keep first channel
            if indata.ndim == 2 and indata.shape[1] > 1:
                arr = indata[:, 0]
            else:
                arr = indata.ravel()
            # ensure int16; if not, convert
            if arr.dtype != np.int16:
                # float32 -> int16
                if arr.dtype == np.float32 or arr.dtype == np.float64:
                    arr = (arr * 32767.0).astype(np.int16)
                else:
                    arr = arr.astype(np.int16)
            b = arr.tobytes()
            # push to queue (non-blocking)
            try:
                self._mic_queue.put_nowait(b)
            except queue.Full:
                # drop old frames if queue is full
                try:
                    _ = self._mic_queue.get_nowait()
                    self._mic_queue.put_nowait(b)
                except Exception:
                    pass
        except Exception as e:
            logging.exception("[MIC] callback error: %s", e)

    def _start_mic_stream(self):
        if sd is None:
            logging.error("sounddevice not available; cannot use mic")
            return False
        try:
            # InputStream with the same sample rate and blocksize == samples_per_packet
            self._mic_stream = sd.InputStream(samplerate=self.sample_rate,
                                              blocksize=self.samples_per_packet,
                                              channels=1,
                                              dtype='int16',
                                              callback=self._mic_callback)
            self._mic_stream.start()
            logging.info("[MIC] InputStream started (sr=%d block=%d)", self.sample_rate, self.samples_per_packet)
            return True
        except Exception:
            logging.exception("[MIC] failed to start microphone stream")
            return False

    def _stop_mic_stream(self):
        try:
            if self._mic_stream:
                self._mic_stream.stop()
                self._mic_stream.close()
        except Exception:
            pass
        self._mic_stream = None

    # --- main loops ---
    def start(self):
        logging.info("Starting Fake ESP32 Cam+Audio -> %s", self.server_url)
        # start mic if requested
        if self.use_mic:
            started = self._start_mic_stream()
            if not started:
                logging.warning("Failed to start mic; falling back to synthetic audio")
                self.use_mic = False

        while not self.stop_event.is_set():
            try:
                self.ws = self._make_ws()
                wst = threading.Thread(target=self.ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True)
                wst.start()

                wait_start = time.time()
                while not self.connected.is_set() and time.time() - wait_start < 10 and not self.stop_event.is_set():
                    time.sleep(0.05)

                if not self.connected.is_set():
                    logging.warning("[WS] connect timed out; backing off %.1fs", self._backoff)
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    time.sleep(self._backoff)
                    self._backoff = min(30.0, self._backoff * 2)
                    continue

                # connected
                self._last_frame_ts = 0.0
                self._last_audio_ts = 0.0
                self._last_keepalive = time.time()

                while self.connected.is_set() and not self.stop_event.is_set():
                    now = time.time()

                    # KeepAlive text every 15s
                    if now - self._last_keepalive > 15.0:
                        try:
                            self.ws.send(json.dumps({"type": "KeepAlive"}))
                        except Exception:
                            pass
                        self._last_keepalive = now

                    # camera frame
                    if now - self._last_frame_ts >= self.frame_interval:
                        jpeg = self._capture_jpeg()
                        if jpeg:
                            try:
                                self.ws.send(jpeg, opcode=ABNF.OPCODE_BINARY)
                                if self.save_frames:
                                    fn = self.record_dir / f"frame_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
                                    fn.write_bytes(jpeg)
                                logging.debug("[SEND] frame len=%d", len(jpeg))
                            except Exception:
                                logging.exception("[SEND] failed to send frame - reconnecting")
                                break
                        self._last_frame_ts = now

                    # audio packet if recording
                    if self.recording and now - self._last_audio_ts >= self.audio_interval:
                        self._last_audio_ts = now
                        audio_bytes = None

                        # priority: mic -> wav -> synthetic
                        if self.use_mic and self._mic_queue is not None:
                            # try to get exactly one packet's worth
                            try:
                                # join multiple queued small chunks if needed
                                audio_bytes = self._mic_queue.get(timeout=0.05)
                                # If the frame length differs from expected, try to pad/truncate:
                                if len(audio_bytes) != self.samples_per_packet * 2:
                                    # if it is longer, truncate; if shorter, pad with zeros
                                    if len(audio_bytes) > self.samples_per_packet * 2:
                                        audio_bytes = audio_bytes[:self.samples_per_packet * 2]
                                    else:
                                        audio_bytes = audio_bytes.ljust(self.samples_per_packet * 2, b'\x00')
                            except queue.Empty:
                                audio_bytes = None

                        if audio_bytes is None and self._wav:
                            audio_bytes = self._read_wav_packet()

                        if audio_bytes is None and (self.use_sine or not self.use_mic):
                            audio_bytes = self._generate_sine_packet()

                        if audio_bytes:
                            try:
                                self.ws.send(audio_bytes, opcode=ABNF.OPCODE_BINARY)
                                logging.info("[SEND] audio packet len=%d", len(audio_bytes))
                            except Exception:
                                logging.exception("[SEND] failed to send audio - reconnecting")
                                break

                    time.sleep(0.005)

            except KeyboardInterrupt:
                logging.info("Interrupted by user")
                break
            except Exception:
                logging.exception("Outer loop exception, reconnecting after backoff")
                time.sleep(self._backoff)
                self._backoff = min(30.0, self._backoff * 2)
                continue

        logging.info("Simulator stopped")
        self._stop_mic_stream()

    def stop(self):
        logging.info("Stopping simulator")
        self.stop_event.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self._cap:
                self._cap.release()
        except Exception:
            pass
        if self._wav:
            try:
                self._wav.close()
            except Exception:
                pass
        self._stop_mic_stream()


def parse_args():
    p = argparse.ArgumentParser(description="Fake ESP32-CAM + audio simulator with optional mic")
    p.add_argument("--server", "-s", required=True, help="ws://<phone-ip>:<port>")
    p.add_argument("--interval", "-i", type=float, default=0.2, help="seconds between camera frames (default 0.2)")
    p.add_argument("--audio-interval", type=float, default=1.0, help="seconds between audio packets while recording (default 1.0)")
    p.add_argument("--quality", "-q", type=int, default=70, help="JPEG quality 1-100")
    p.add_argument("--webcam-index", type=int, default=0, help="OpenCV webcam index (default 0)")
    p.add_argument("--wav", type=str, default=None, help="path to 16-bit mono WAV to stream as audio packets (optional)")
    p.add_argument("--use-mic", action="store_true", help="use laptop microphone for audio packets (requires sounddevice)")
    p.add_argument("--sine", action="store_true", help="use generated synthetic audio even if WAV provided")
    p.add_argument("--samples", type=int, default=512, help="samples per packet (default 512 -> 1024 bytes)")
    p.add_argument("--sr", type=int, default=16000, help="audio sample rate (default 16000)")
    p.add_argument("--save-frames", action="store_true", help="save sent JPEG frames locally in ./recordings")
    return p.parse_args()


def main():
    args = parse_args()
    sim = FakeESP32CamAudio(
        server_url=args.server,
        frame_interval=args.interval,
        jpeg_quality=args.quality,
        webcam_index=args.webcam_index,
        audio_interval=args.audio_interval,
        samples_per_packet=args.samples,
        sample_rate=args.sr,
        wav_path=args.wav,
        use_mic=args.use_mic,
        sine=args.sine,
        save_frames=args.save_frames
    )
    try:
        sim.start()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt, stopping")
    finally:
        sim.stop()


if __name__ == '__main__':
    main()
