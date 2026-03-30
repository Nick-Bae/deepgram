const os = require('os');
const fs = require('fs');
const path = require('path');

function isWslEnvironment() {
  if (process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP) return true;
  try {
    return fs.readFileSync('/proc/version', 'utf8').toLowerCase().includes('microsoft');
  } catch {
    return false;
  }
}

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

const explicitHost = (process.env.BACKEND_HOST || '').trim();
const autoDetect = /^(1|true|yes)$/i.test((process.env.AUTO_DETECT_BACKEND_IP || '').trim());
const runningInWsl = isWslEnvironment();
const detectedIp = autoDetect ? getPreferredNetworkIP() : null;
const ip = explicitHost || (runningInWsl ? 'localhost' : (detectedIp || 'localhost'));
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
  const wslHint = runningInWsl && !explicitHost
    ? '\nWSL detected: using localhost avoids unstable WSL private IPs like 172.x.x.x.'
    : '';
  console.log(`⚠️ .env.local updated with localhost default:\n${envContent}${wslHint}${hint}`);
} else {
  console.log(`✅ .env.local updated with detected IP (${ip}):\n${envContent}`);
}
