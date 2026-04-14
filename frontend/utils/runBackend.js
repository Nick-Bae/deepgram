const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const backendDir = path.resolve(__dirname, "../../backend");
const backendEnvPath = path.join(backendDir, ".env");

function parseEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return {};

  const out = {};
  const content = fs.readFileSync(filePath, "utf8");

  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;

    const match = rawLine.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (!match) continue;

    let value = match[2].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }

    out[match[1]] = value;
  }

  return out;
}

function resolveCandidatePath(filePath) {
  const candidate = String(filePath || "").trim();
  if (!candidate) return "";
  if (path.isAbsolute(candidate)) return candidate;
  return path.resolve(backendDir, candidate);
}

function getPythonPath() {
  if (process.platform === "win32") {
    const preferred = path.join(backendDir, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(preferred)) return preferred;
    return path.join(backendDir, "venv", "Scripts", "python.exe");
  }
  const preferred = path.join(backendDir, ".venv", "bin", "python");
  if (fs.existsSync(preferred)) return preferred;
  return path.join(backendDir, "venv", "bin", "python");
}

const envFile = parseEnvFile(backendEnvPath);
const explicitStoreBackend = String(process.env.MULTICHURCH_STORE_BACKEND || "").trim();
const configuredCredentialsPath = resolveCandidatePath(
  process.env.GOOGLE_APPLICATION_CREDENTIALS || envFile.GOOGLE_APPLICATION_CREDENTIALS
);
const shouldForceMemoryStore =
  !explicitStoreBackend &&
  !!configuredCredentialsPath &&
  !fs.existsSync(configuredCredentialsPath);

const childEnv = { ...process.env };
if (shouldForceMemoryStore) {
  childEnv.MULTICHURCH_STORE_BACKEND = "memory";
  console.warn(
    `[backend] ${configuredCredentialsPath} is missing; starting with MULTICHURCH_STORE_BACKEND=memory for local dev.`
  );
}

const pythonPath = getPythonPath();
const child = spawn(
  pythonPath,
  ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"],
  {
    cwd: backendDir,
    env: childEnv,
    stdio: "inherit",
  }
);

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});

child.on("error", (error) => {
  console.error(`[backend] Failed to start backend via ${pythonPath}: ${error.message}`);
  process.exit(1);
});
