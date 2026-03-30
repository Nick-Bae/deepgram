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

function getPreferredIP() {
  const interfaces = os.networkInterfaces();
  let tenCandidate = null;
  let homeLanCandidate = null;
  let private172Candidate = null;
  let fallback = null;

  for (const name of Object.keys(interfaces)) {
    if (/vEthernet|WSL|Hyper-V|Loopback|Docker/i.test(name)) continue;
    for (const iface of interfaces[name] || []) {
      if (iface.family === 'IPv4' && !iface.internal) {
        if (iface.address.startsWith('10.')) tenCandidate ||= iface.address;
        else if (iface.address.startsWith('192.168.')) homeLanCandidate ||= iface.address;
        else if (/^172\.(1[6-9]|2\d|3[0-1])\./.test(iface.address)) private172Candidate ||= iface.address;
        else fallback ||= iface.address;
      }
    }
  }
  return tenCandidate || homeLanCandidate || private172Candidate || fallback || '127.0.0.1';
}

const explicitHost = (process.env.BACKEND_HOST || '').trim();
const autoDetect = /^(1|true|yes)$/i.test((process.env.AUTO_DETECT_BACKEND_IP || '').trim());
const runningInWsl = isWslEnvironment();
const ip = explicitHost || (runningInWsl ? 'localhost' : (autoDetect ? getPreferredIP() : 'localhost'));
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

const content = upsertEnvVars(envPath, {
  NEXT_PUBLIC_API_BASE_URL: `http://${ip}:8000`,
  NEXT_PUBLIC_WS_URL: `ws://${ip}:8000`,
});

if (ip === 'localhost') {
  const wslHint = runningInWsl && !explicitHost
    ? '\nWSL detected: using localhost avoids unstable WSL private IPs like 172.x.x.x.'
    : '';
  console.log(`⚠️ .env.local updated with localhost default:\n${content}${wslHint}`);
} else {
  console.log(`✅ .env.local updated with backend host (${ip}):\n${content}`);
}
