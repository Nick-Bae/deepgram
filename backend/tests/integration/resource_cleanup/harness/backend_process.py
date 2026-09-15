"""Uvicorn subprocess launcher for the resource-cleanup integration
harness. Each `BackendProcess` runs the real `app.main:app` in a fresh
process with distinct `INSTANCE_ID` and port, so cross-instance
behavior (Redis fanout, cleanup, startup safety) is exercised end-to-
end.

The harness intentionally reuses the production entrypoint. No test-
only routes or diagnostic endpoints are added to `app.main` — every
observation the test makes is through the real HTTP / WebSocket
surface or through direct Firestore emulator reads.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class BackendConfig:
    """Configuration for one uvicorn backend process."""

    instance_id: str
    port: int = 0  # 0 → assign a free port
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    firestore_emulator_host: str = "127.0.0.1:8085"
    gcp_project: str = "cleanup-track1-harness"
    deepgram_endpoint: str = ""  # set to the stub's ws://... URL
    host_api_token: str = "harness-host-token"
    extra_env: dict = field(default_factory=dict)

    def resolved_port(self) -> int:
        return self.port or _free_port()


class BackendProcess:
    """One backend uvicorn subprocess.

    Attributes:
        config: The `BackendConfig` used to launch this process.
        proc:   The `subprocess.Popen` handle (None before start).
        base_url: `http://127.0.0.1:<port>` for HTTP requests.
        ws_url:   `ws://127.0.0.1:<port>` for WebSocket connections.
        log_path: file path capturing stdout+stderr from this process.
    """

    def __init__(self, config: BackendConfig):
        self.config = config
        if not self.config.port:
            self.config.port = _free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.base_url = f"http://127.0.0.1:{self.config.port}"
        self.ws_url = f"ws://127.0.0.1:{self.config.port}"
        self.log_file = None
        self.log_path: str = ""

    def env(self) -> dict:
        env = os.environ.copy()
        # Store: real Firestore against the emulator.
        env["MULTICHURCH_STORE_BACKEND"] = "firestore"
        env["FIRESTORE_EMULATOR_HOST"] = self.config.firestore_emulator_host
        env["GOOGLE_CLOUD_PROJECT"] = self.config.gcp_project
        # Redis: real Redis, real fanout — the whole point of the harness.
        env["REDIS_ENABLED"] = "1"
        env["REDIS_HOST"] = self.config.redis_host
        env["REDIS_PORT"] = str(self.config.redis_port)
        env["INSTANCE_ID"] = self.config.instance_id
        # Deepgram: point at the stub instead of the paid endpoint.
        if self.config.deepgram_endpoint:
            env["DEEPGRAM_ENDPOINT"] = self.config.deepgram_endpoint
        # Bypass rate limits and Firebase-auth checks that would need
        # real credentials to exercise. HOST_API_TOKEN is the global
        # test-only shared secret the store accepts as host auth.
        env["HOST_API_TOKEN"] = self.config.host_api_token
        env["DISABLE_WS_TRANSLATION_LIMITS"] = "1"
        # A minimal set of the env vars app.main reads at import time —
        # keep in sync with backend/.env.example when new ones appear.
        env.setdefault("DEEPGRAM_API_KEY", "test-key-not-used")
        env.setdefault("DEEPGRAM_MODEL", "nova-3")
        env.setdefault("DEEPGRAM_LANGUAGE", "ko")
        env.setdefault("OPENAI_API_KEY", "test-key-not-used")
        env.setdefault("OPENAI_TRANSLATION_MODEL", "gpt-4o")
        env.setdefault("CORS_ALLOW_ORIGINS", "http://localhost")
        env.setdefault("ROOM_SWEEPER_INTERVAL_SEC", "60")
        env.setdefault("ROOM_IDLE_TIMEOUT_SEC", "900")
        env.update(self.config.extra_env)
        return env

    def start(self) -> None:
        if self.proc is not None:
            raise RuntimeError("BackendProcess already started")
        # Log file for post-mortem when a test fails.
        log_fd, self.log_path = tempfile.mkstemp(
            prefix=f"backend-{self.config.instance_id}-",
            suffix=".log",
        )
        self.log_file = os.fdopen(log_fd, "w")
        # Run uvicorn against the real app entrypoint.
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.config.port),
            "--log-level",
            "info",
        ]
        # Working directory: the backend package root, so `app.main`
        # resolves. Callers ensure they run from backend/.
        self.proc = subprocess.Popen(
            cmd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            env=self.env(),
        )

    def wait_ready(self, *, timeout: float = 30.0) -> None:
        """Poll the health path until the server accepts requests."""
        deadline = time.monotonic() + timeout
        last_err = None
        # No `/health` endpoint in the app — use any HTTP path and
        # accept any response (404 counts as "server up"). The app root
        # returns 404 on GET which is enough to prove uvicorn accepted
        # the socket.
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{self.base_url}/", timeout=2.0)
                if r.status_code < 500:
                    return
            except Exception as exc:
                last_err = exc
            # Detect early crash
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"backend {self.config.instance_id} exited during startup "
                    f"(code={self.proc.returncode}); see {self.log_path}"
                )
            time.sleep(0.2)
        raise TimeoutError(
            f"backend {self.config.instance_id} did not become ready within "
            f"{timeout:.1f}s (last error: {last_err}); see {self.log_path}"
        )

    def stop(self, *, timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5.0)
        finally:
            if self.log_file is not None:
                try:
                    self.log_file.close()
                except Exception:
                    pass
                self.log_file = None
        self.proc = None

    def logs(self) -> str:
        try:
            with open(self.log_path, "r") as f:
                return f.read()
        except Exception:
            return ""

    def __enter__(self):
        self.start()
        self.wait_ready()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()


def redis_reachable(*, host: str = "127.0.0.1", port: int = 6379, timeout: float = 2.0) -> bool:
    """Cheap TCP probe — no redis client needed."""
    try:
        with contextlib.closing(socket.create_connection((host, port), timeout=timeout)):
            return True
    except OSError:
        return False


def firestore_emulator_reachable(host: str = "127.0.0.1:8085", timeout: float = 2.0) -> bool:
    """HTTP probe against the emulator's root path."""
    try:
        r = httpx.get(f"http://{host}/", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def which_or_raise(name: str) -> str:
    """Fail loudly and specifically if a required binary isn't on PATH."""
    p = shutil.which(name)
    if not p:
        raise RuntimeError(
            f"required binary {name!r} not found on PATH — "
            f"the resource-cleanup integration harness needs it. "
            f"See backend/tests/integration/resource_cleanup/README.md"
        )
    return p
