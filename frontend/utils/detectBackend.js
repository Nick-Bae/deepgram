const os = require('os');
const fs = require('fs');
const path = require('path');

function getPreferredNetworkIP() {
  try {
    const interfaces = os.networkInterfaces();
    if (!interfaces || typeof interfaces !== 'object') return null;

    for (const name of Object.keys(interfaces)) {
      if (/vEthernet|WSL|Hyper-V|Loopback/i.test(name)) continue;
      const entries = interfaces[name] || [];
      for (const iface of entries) {
        if (
          iface.family === 'IPv4' &&
          !iface.internal &&
          /^(10\.|192\.168\.|172\.)/.test(iface.address)
        ) {
          return iface.address;
        }
      }
    }

    // Fallback: any non-internal IPv4 if private ranges were not found.
    for (const name of Object.keys(interfaces)) {
      const entries = interfaces[name] || [];
      for (const iface of entries) {
        if (iface.family === 'IPv4' && !iface.internal) {
          return iface.address;
        }
      }
    }
  } catch (err) {
    console.warn(`⚠️ IP detection failed (${err.message}). Falling back to localhost.`);
  }

  return null;
}

const ip = process.env.BACKEND_HOST || getPreferredNetworkIP() || 'localhost';
const envPath = path.join(__dirname, '../.env.local');
const envContent =
  `NEXT_PUBLIC_API_BASE_URL=http://${ip}:8000\n` +
  `NEXT_PUBLIC_WS_URL=ws://${ip}:8000/ws/translate\n`;

fs.writeFileSync(envPath, envContent);

if (ip === 'localhost') {
  console.log(`⚠️ .env.local updated with localhost fallback:\n${envContent}`);
} else {
  console.log(`✅ .env.local updated with detected IP (${ip}):\n${envContent}`);
}
