# ESP32 Fake Simulator Toolkit

This repository contains a suite of Python and Node.js scripts designed
to simulate an ESP32 or ESP32-CAM device. It is incredibly useful for
developing and testing mobile apps (like Flutter apps) that expect live
audio (PCM16LE) and video (JPEG) streams over WebSockets, without
needing actual physical hardware. It also includes a proxy for testing
live Speech-to-Text (STT) via Deepgram.

## 🛠 Prerequisites

You will need **Python 3** and **Node.js** installed on your system.

Install the required Python packages:

``` bash
pip install websocket-client numpy sounddevice opencv-python
```

*(Note: `opencv-python` is only required for webcam video streaming, and
`sounddevice` is required for live microphone streaming).*

Install the required Node.js packages (for the proxy):

``` bash
npm install ws
```

## 📂 Files Overview

-   **`webcam.py`**: A full-featured simulator that streams both JPEG
    frames from your laptop's webcam and audio (via live mic, WAV file,
    or synthetic sine wave) to a WebSocket server.
-   **`mic.py`**: A bi-directional audio streamer. It streams live
    microphone audio to the server and listens for returned JSON
    transcripts, rendering them in the terminal to simulate an ESP32
    OLED screen.
-   **`proxy.js`**: A Node.js WebSocket proxy. It acts as a bridge
    between your mobile app and Deepgram's API, forwarding binary audio
    to Deepgram and returning the text transcripts back to the app.
-   **`wave_streamer.py`**: A simple utility to stream a 16-bit mono
    16kHz WAV file over WebSockets as binary frames.
-   **`esp.py`**: A barebones, blocking simulator that continuously
    sends synthetic sine-wave audio packets. Good for quick connection
    testing.

------------------------------------------------------------------------

## 🚀 Usage Guide

### 1. The Deepgram STT Proxy (`proxy.js`)

If your mobile app uses Deepgram for Speech-to-Text, you can run this
local proxy to bridge the connection. It will print out your local IP
address upon startup so you can point your mobile device to it.

**Setup & Run:**

``` bash
node proxy.js
```

-   **Default Port:** `3000`
-   **Endpoint:** `ws://<your-local-ip>:3000/stream`

### 2. Full Camera & Audio Simulator (`webcam.py`)

This is the most feature-rich script. It simulates an ESP32-CAM by
capturing frames from your webcam and combining them with an audio
source.

**Basic Usage (Webcam + Synthetic Sine Wave Audio):**

``` bash
python webcam.py --server ws://<phone-ip>:8889
```

**Webcam + Live Laptop Microphone:**

``` bash
python webcam.py --server ws://<phone-ip>:8889 --use-mic
```

**Webcam + WAV File Audio:**

``` bash
python webcam.py --server ws://<phone-ip>:8889 --wav sample_16k_mono.wav
```

### 3. Bi-Directional Mic & OLED Simulator (`mic.py`)

Streams your laptop microphone and visually renders any incoming text
transcripts (like those sent back from the `proxy.js` via your app) into
a simulated ASCII OLED display in your terminal.

**Usage:**

``` bash
python mic.py ws://<phone-ip>:8889
```

### 4. WAV File Streamer (`wave_streamer.py`)

Useful if you want perfectly repeatable audio data for testing speech
recognition accuracy without background noise.

**Usage:**

``` bash
python wave_streamer.py path/to/audio.wav ws://<phone-ip>:8889
```

*Note: Ensure your WAV file is 16-bit, mono, and ideally 16000Hz to
mimic standard ESP32 audio configurations.*

### 5. Simple Blocking Simulator (`esp.py`)

A lightweight, no-dependency (other than `websocket-client`) script to
test if your WebSocket server is receiving data. It sends a continuous
synthetic tone.

**Usage:** 1. Open `esp.py` and edit the `SERVER_URL` variable to match
your target server. 2. Run the script:

``` bash
python esp.py
```

------------------------------------------------------------------------

## 🏗 Architecture Example

If you are developing a Flutter app that shows video and transcribes
audio, your testing workflow might look like this:

1.  Start **`proxy.js`** on your laptop.
2.  Point your **Flutter App**'s STT engine to
    `ws://<laptop-ip>:3000/stream`.
3.  Start your **Flutter App**'s WebSocket server (e.g., on port 8889)
    to listen for the ESP32.
4.  Run **`webcam.py`** or **`mic.py`** on your laptop, pointing it to
    your Flutter app: `ws://<phone-ip>:8889`.

Now, your laptop feeds video/audio into your phone, your phone forwards
the audio to the local proxy, the proxy forwards it to Deepgram, and the
transcript flows all the way back to your phone!
