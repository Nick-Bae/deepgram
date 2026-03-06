import socket
import os
from pathlib import Path


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def upsert_env_vars(path: Path, updates: dict[str, str]) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    seen: set[str] = set()
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or "=" not in line:
            if line:
                output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    next_content = "\n".join([line for line in output if line]) + "\n"
    path.write_text(next_content, encoding="utf-8")
    return next_content


explicit_host = (os.getenv("BACKEND_HOST") or "").strip()
auto_detect = (os.getenv("AUTO_DETECT_BACKEND_IP") or "").strip().lower() in {"1", "true", "yes"}
ip = explicit_host or (get_local_ip() if auto_detect else "localhost")

frontend_env_path = Path("frontend") / ".env.local"
frontend_env_path.parent.mkdir(parents=True, exist_ok=True)
env_text = upsert_env_vars(
    frontend_env_path,
    {
        "NEXT_PUBLIC_API_BASE_URL": f"http://{ip}:8000",
        "NEXT_PUBLIC_WS_URL": f"ws://{ip}:8000",
    },
)

print("✅ .env.local updated with:")
print(env_text)
