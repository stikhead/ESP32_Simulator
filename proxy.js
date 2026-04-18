
// proxy.js
// proxy.js
// Usage: DEEPGRAM_API_KEY=your_key node proxy.js
// Prints local IPv4 addresses on startup to help you pick the correct proxy IP.

const WebSocket = require('ws');
const http = require('http');
const os = require('os');
import 'dotenv/config';
const PORT = process.env.PORT ? parseInt(process.env.PORT) : 3000;
const DEEPGRAM_KEY =  'process.env.DEEPGRAM_KEY'
if (!DEEPGRAM_KEY) {
  console.error('Set DEEPGRAM_API_KEY env var');
  process.exit(1);
}

// Adjust params to match your audio format (ESP32): PCM16LE 16000 mono
const DG_URL = 'wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1';

function printLocalIPs() {
  const ifaces = os.networkInterfaces();
  const ips = [];
  Object.keys(ifaces).forEach((name) => {
    for (const iface of ifaces[name]) {
      if (iface.family === 'IPv4' && !iface.internal) {
        ips.push({ ifname: name, address: iface.address });
      }
    }
  });
  if (ips.length === 0) {
    console.log('No non-loopback IPv4 addresses detected. Is the machine connected to the same network/hotspot?');
  } else {
    console.log('Local IPv4 addresses (use one of these on your phone as Proxy Host):');
    ips.forEach((i) => console.log(` - ${i.ifname}: ${i.address}`));
  }
}

printLocalIPs();

const server = http.createServer();
const wss = new WebSocket.Server({ server, path: '/stream' });

wss.on('connection', function connection(wsClient, req) {
  console.log('Flutter app connected to proxy from', req.socket.remoteAddress);

  // Connect to Deepgram
  const dg = new WebSocket(DG_URL, {
    headers: {
      Authorization: `Token ${DEEPGRAM_KEY}`,
    },
  });

dg.on('open', () => {
  console.log('Connected to Deepgram');
});

dg.on('message', (msg, isBinary) => {
  try {
    // convert Buffer -> utf8 string so Flutter receives text (not binary)
    let out;
    if (typeof msg === 'string') {
      out = msg;
    } else if (Buffer.isBuffer(msg)) {
      out = msg.toString('utf8');
    } else {
      out = String(msg);
    }

    if (wsClient.readyState === WebSocket.OPEN) {
      wsClient.send(out);
    }
  } catch (e) {
    console.warn('Forward failed:', e);
  }
});

dg.on('close', (code, reason) => {
  console.log('Deepgram socket closed', code, reason && reason.toString());
  try {
    if (wsClient.readyState === WebSocket.OPEN)
      wsClient.send(JSON.stringify({ type: 'dg_closed', code, reason: String(reason) }));
  } catch (_) {}
});

dg.on('error', (err) => {
  console.error('Deepgram socket error', err && err.toString());
});

  wsClient.on('message', (data, isBinary) => {
    // Expect binary audio frames from Flutter; forward directly to Deepgram
    if (isBinary) {
      if (dg.readyState === WebSocket.OPEN) {
        dg.send(data);
      }
    } else {
      // Text control messages forwarded as-is
      if (dg.readyState === WebSocket.OPEN) {
        dg.send(data.toString());
      }
    }
  });

  wsClient.on('close', (code, reason) => {
    console.log('Flutter ws closed', code, reason && reason.toString());
    try {
      dg.close();
    } catch (_) {}
  });

  wsClient.on('error', (err) => {
    console.warn('Flutter ws error', err);
  });
});

server.listen(PORT, () => {
  console.log(`Proxy listening on ws://0.0.0.0:${PORT}/stream`);
});
