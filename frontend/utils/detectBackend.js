const os = require('os');
const fs = require('fs');
const path = require('path');

const EXCLUDED_INTERFACE_NAME_RE = /vEthernet|WSL|Hyper-V|Loopback|docker|vboxnet|vmnet|virbr/i;
const PREFERRED_LAN_IP_RE = /^(10\.|192\.168\.)/;

function getPreferredNetworkIP() {
  try {
    const interfaces = os.networkInterfaces();
    if (!interfaces || typeof interfaces !== 'object') return null;

    for (const name of Object.keys(interfaces)) {
      if (EXCLUDED_INTERFACE_NAME_RE.test(name)) continue;
      const entries = interfaces[name] || [];
      for (const iface of entries) {
        if (
          iface.family === 'IPv4' &&
          !iface.internal &&
          PREFERRED_LAN_IP_RE.test(iface.address)
        ) {
          return iface.address;
        }
      }
    }

    // Fall back to localhost unless the user passes BACKEND_HOST explicitly.
    // In WSL/Docker setups, 172.x addresses are often ephemeral and not usable
    // from the browser even though they look like private LAN addresses.
  } catch (err) {
    console.warn(`⚠️ IP detection failed (${err.message}). Falling back to localhost.`);
  }

  return null;
}

const explicitHost = (process.env.BACKEND_HOST || '').trim();
const autoDetect = /^(1|true|yes)$/i.test((process.env.AUTO_DETECT_BACKEND_IP || '').trim());
const detectedIp = autoDetect ? getPreferredNetworkIP() : null;
const ip = explicitHost || detectedIp || 'localhost';
const envPath = path.join(__dirname, '../.env.local');

function upsertEnvVars(envFilePath, updates) {
  const existing = fs.existsSync(envFilePath)
    ? fs.readFileSync(envFilePath, 'utf8')
    : '';
  const lines = existing.split(/\r?\n/);
  const seen = new Set();
  const output = [];

  for (const line of lines) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (!match) {
      if (line.length) output.push(line);
      continue;
    }
    const key = match[1];
    if (Object.prototype.hasOwnProperty.call(updates, key)) {
      output.push(`${key}=${updates[key]}`);
      seen.add(key);
    } else {
      output.push(line);
    }
  }

  for (const [key, value] of Object.entries(updates)) {
    if (!seen.has(key)) output.push(`${key}=${value}`);
  }

  const nextContent = `${output.filter(Boolean).join('\n')}\n`;
  fs.writeFileSync(envFilePath, nextContent, 'utf8');
  return nextContent;
}

const envContent = upsertEnvVars(envPath, {
  NEXT_PUBLIC_API_BASE_URL: `http://${ip}:8000`,
  NEXT_PUBLIC_WS_URL: `ws://${ip}:8000/ws/translate`,
});

if (ip === 'localhost') {
  const hint = autoDetect
    ? '\nTip: pass BACKEND_HOST=<LAN_IP> if you are opening frontend from another device.'
    : '';
  console.log(`⚠️ .env.local updated with localhost default:\n${envContent}${hint}`);
} else {
  console.log(`✅ .env.local updated with detected IP (${ip}):\n${envContent}`);
}
