"""Small Toxiproxy HTTP client for deterministic Redis outages."""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class RedisToxiProxy:
    api_url: str
    name: str
    listen_port: int
    upstream: str

    @property
    def redis_host(self) -> str:
        return "127.0.0.1"

    @property
    def redis_port(self) -> int:
        return self.listen_port

    def create(self) -> None:
        with httpx.Client(timeout=5.0) as client:
            client.delete(f"{self.api_url}/proxies/{self.name}")
            response = client.post(
                f"{self.api_url}/proxies",
                json={
                    "name": self.name,
                    "listen": f"0.0.0.0:{self.listen_port}",
                    "upstream": self.upstream,
                    "enabled": True,
                },
            )
            response.raise_for_status()

    def set_enabled(self, enabled: bool) -> None:
        response = httpx.post(
            f"{self.api_url}/proxies/{self.name}",
            json={"enabled": bool(enabled)},
            timeout=5.0,
        )
        response.raise_for_status()

    def delete(self) -> None:
        response = httpx.delete(
            f"{self.api_url}/proxies/{self.name}",
            timeout=5.0,
        )
        if response.status_code not in {204, 404}:
            response.raise_for_status()


def toxiproxy_reachable(api_url: str, *, timeout: float = 2.0) -> bool:
    try:
        response = httpx.get(f"{api_url}/version", timeout=timeout)
        return response.status_code < 500
    except Exception:
        return False
