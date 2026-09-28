"""Paquet mqtt_notif : pont MQTT vers notifications FCM (data-only)."""

from __future__ import annotations

from datetime import datetime, timezone

__version__ = "0.1.0"


def log_line(text: str) -> None:
    """Émet une ligne de journal plate sur stdout, préfixée d'un timestamp ISO 8601 UTC."""
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"[{timestamp}] {text}", flush=True)
