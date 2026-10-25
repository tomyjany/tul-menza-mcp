"""Hard rolling-24h purchase limit, enforced by the account that owns the credentials.

The MCP host (an LLM agent) can pass confirmed=true by itself, so confirmation is not
authentication. This bound limits damage regardless of what the host is tricked into doing.
Limits come from the operator environment (root-owned launcher/systemd unit), never from
tool arguments.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .client import MenzaError
from .models import PRAGUE

DEFAULT_MAX_PURCHASES = 2
DEFAULT_MAX_CZK = 250.0


class SpendGuard:
    def __init__(self, state_dir: Path, max_purchases: int | None = None, max_czk: float | None = None):
        self.path = state_dir / "spending.sqlite3"
        self.max_purchases = max_purchases if max_purchases is not None else int(
            os.environ.get("MENZA_MAX_PURCHASES_24H", DEFAULT_MAX_PURCHASES))
        self.max_czk = max_czk if max_czk is not None else float(
            os.environ.get("MENZA_MAX_CZK_24H", DEFAULT_MAX_CZK))
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS purchases (at TEXT NOT NULL, price REAL NOT NULL)")
            db.commit()
        finally:
            db.close()
        self.path.chmod(0o600)

    def usage(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(PRAGUE)
        since = (now - timedelta(hours=24)).isoformat()
        db = sqlite3.connect(self.path)
        try:
            count, total = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(price), 0) FROM purchases WHERE at > ?", (since,)).fetchone()
        finally:
            db.close()
        return {"purchases_24h": count, "spent_24h": round(total, 2),
                "max_purchases_24h": self.max_purchases, "max_czk_24h": self.max_czk}

    def __call__(self, price: float, now: datetime | None = None) -> None:
        now = now or datetime.now(PRAGUE)
        use = self.usage(now)
        if use["purchases_24h"] + 1 > self.max_purchases or use["spent_24h"] + price > self.max_czk + 1e-9:
            raise MenzaError(
                f"24h purchase limit reached ({use['purchases_24h']}/{self.max_purchases} purchases, "
                f"{use['spent_24h']:.2f}/{self.max_czk:.2f} Kč); change it only on the server")
        db = sqlite3.connect(self.path)
        try:
            db.execute("INSERT INTO purchases VALUES (?, ?)", (now.isoformat(), price))
            db.commit()
        finally:
            db.close()
