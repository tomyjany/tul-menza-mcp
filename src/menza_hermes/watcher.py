"""Persistent, bounded burza watches. Run the worker separately from the stdio server.

A committed 'purchasing' record precedes every POST. A worker crash during that
POST never causes an automatic second purchase; it becomes 'needs_review'.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .client import MenzaError
from .models import PRAGUE, Meal, canteen_slug, normalize

POLL_SECONDS = 20


def price_limit(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("max_price must be a finite positive CZK amount")
    if Decimal(str(value)).as_tuple().exponent < -2:
        raise ValueError("max_price must have at most two decimal places")
    return value


class Watches:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "watches.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS watches (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            canteen TEXT NOT NULL, day TEXT NOT NULL, section TEXT NOT NULL,
            number TEXT NOT NULL, name TEXT NOT NULL, max_price REAL NOT NULL,
            status TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}'
        )""")
        self.db.commit()

    def close(self):
        self.db.close()

    def list(self) -> list[dict]:
        result = []
        for row in self.db.execute("SELECT * FROM watches ORDER BY created_at DESC"):
            item = dict(row)
            item["result"] = json.loads(item["result"])
            result.append(item)
        return result

    def change(self, watch_id: str, status: str, now: datetime, result: dict | None = None):
        self.db.execute("UPDATE watches SET status=?,updated_at=?,result=? WHERE id=?",
                        (status, now.isoformat(), json.dumps(result or {}, ensure_ascii=False), watch_id))
        self.db.commit()

    def create(self, meal: Meal, max_price: float, expires_at: datetime, now: datetime) -> dict:
        price_limit(max_price)
        if expires_at.tzinfo is None:
            raise ValueError("expires_at requires a timezone offset")
        if not now < expires_at <= now + timedelta(days=14):
            raise ValueError("watch expiry must be in the future and within 14 days")
        if meal.direct_sale_only or meal.section == "minutka":
            raise MenzaError("Minutky are sold in person; they cannot be watched for online purchase")
        if meal.order_id or meal.orderable:
            raise MenzaError("Watch only a sold-out meal that is not already ordered")
        if not meal.sell_from or not meal.buy_until or meal.buy_until <= now:
            raise MenzaError("Cannot watch a meal with unknown or elapsed ordering deadlines")
        expiry = min(expires_at, meal.buy_until)
        if expiry <= max(now, meal.sell_from):
            raise MenzaError("Watch expires before the burza window opens")
        active = [w for w in self.list() if w["status"] in ("waiting", "purchasing")]
        for w in active:
            if (w["day"], w["canteen"], w["section"], w["number"], w["name"]) == (
                meal.date.isoformat(), canteen_slug(meal.canteen_id), meal.section, meal.number, meal.name
            ):
                return w
        if len(active) >= 10:
            raise MenzaError("At most 10 active watches are allowed")
        wid = uuid.uuid4().hex
        self.db.execute("INSERT INTO watches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            wid, now.isoformat(), expiry.isoformat(), canteen_slug(meal.canteen_id),
            meal.date.isoformat(), meal.section, meal.number, meal.name, max_price,
            "waiting", now.isoformat(), "{}",
        ))
        self.db.commit()
        return next(w for w in self.list() if w["id"] == wid)

    def cancel(self, wid: str, now: datetime) -> dict:
        w = next((w for w in self.list() if w["id"] == wid), None)
        if not w:
            raise ValueError("Unknown watch id")
        if w["status"] == "waiting":
            self.change(wid, "cancelled", now)
        return next(w for w in self.list() if w["id"] == wid)

    def tick(self, client, now: datetime) -> list[dict]:
        """One poll under runtime.account's lock. Read failures leave waiting jobs intact.

        Mutation exceptions stop that watch, rather than guessing whether a retry
        would buy twice. list_watches exposes the state to the operator/agent.
        """
        pages = {}
        for w in self.list():
            if w["status"] == "purchasing":
                self.change(w["id"], "needs_review", now, {"reason": "Worker stopped during purchase; inspect orders before retrying"})
                continue
            if w["status"] != "waiting":
                continue
            if now >= datetime.fromisoformat(w["expires_at"]):
                self.change(w["id"], "expired", now)
                continue
            key = (w["canteen"], w["day"])
            if key not in pages:
                pages[key] = client.day(w["canteen"], date.fromisoformat(w["day"]))
            matches = [m for m in pages[key] if m.section == w["section"] and m.number == w["number"]
                       and normalize(m.name) == normalize(w["name"])]
            if len(matches) != 1:
                continue  # never substitute another dish or guess an ambiguous match
            meal = matches[0]
            if meal.order_id:
                self.change(w["id"], "already_ordered", now, {"order_id": meal.order_id})
                continue
            if not meal.sell_from or not meal.buy_until or meal.phase(now) != "burza":
                continue
            if not meal.orderable or meal.price is None or Decimal(str(meal.price)) > Decimal(str(w["max_price"])):
                continue
            self.change(w["id"], "purchasing", now)
            try:
                booked = client.order(w["canteen"], date.fromisoformat(w["day"]), meal.menu_id, max_price=w["max_price"])
            except Exception:
                self.change(w["id"], "needs_review", now, {"reason": "Purchase not confirmed; inspect orders before retrying"})
            else:
                self.change(w["id"], "purchased", now, {"order_id": booked.order_id, "meal": booked.to_dict(now)})
        return self.list()
