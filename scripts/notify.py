"""Discord webhook notifier. Silent no-op if DISCORD_WEBHOOK_URL is unset."""

from __future__ import annotations

import os
from typing import Literal

import httpx

Severity = Literal["info", "warn", "error", "success"]

_COLORS: dict[Severity, int] = {
    "info": 0x3B82F6,
    "success": 0x10B981,
    "warn": 0xF59E0B,
    "error": 0xEF4444,
}


def _webhook_url() -> str | None:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    return url or None


def _send(title: str, body: str, severity: Severity) -> bool:
    url = _webhook_url()
    if not url:
        # No webhook configured — just print so humans running locally see it.
        # This is a silent-ok: absence of webhook is not a failure.
        print(f"[notify:{severity}] {title}\n{body}")
        return True
    payload = {
        "embeds": [{
            "title": title[:256],
            "description": body[:4000],
            "color": _COLORS[severity],
        }],
        "username": "TradingAgent",
    }
    try:
        r = httpx.post(url, json=payload, timeout=10.0)
        return 200 <= r.status_code < 300
    except httpx.HTTPError as e:
        print(f"[notify] webhook failed: {e}")
        return False


def info(title: str, body: str) -> bool:
    return _send(title, body, "info")


def success(title: str, body: str) -> bool:
    return _send(title, body, "success")


def warn(title: str, body: str) -> bool:
    return _send(title, body, "warn")


def error(title: str, body: str) -> bool:
    return _send(title, body, "error")
