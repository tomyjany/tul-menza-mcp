"""Hard purchase bounds, enforced by the account that owns the credentials.

The MCP host (an LLM agent) can pass confirmed=true by itself, so confirmation is not
authentication. These bounds limit what a confused or prompt-injected host can buy, while
still allowing a confirmed 14-day plan of one lunch + one dinner per day:

* at most ``per_slot`` purchases per served date and meal slot (lunch / dinner);
* at most ``max_meal_czk`` per meal;
* at most ``max_czk_14d`` spent in any rolling 14 days.

A reservation is recorded *before* the purchase POST; an uncertain outcome keeps it.
It is released only when the site definitively refused the order, or when a cancellation
was confirmed as a refund in the account balance. Limits come from the operator
environment (root-owned launcher/systemd unit), never from tool arguments.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from .client import MenzaError
from .models import PRAGUE, Meal

DEFAULT_PER_SLOT = 1
DEFAULT_MAX_MEAL_CZK = 150.0
DEFAULT_MAX_CZK_14D = 2000.0
WINDOW = timedelta(days=14)


def meal_slot(section: str) -> str:
    return "dinner" if section == "dinner" else "lunch"


class SpendGuard:
    def __init__(self, state_dir: Path, *, per_slot: int | None = None,
                 max_meal_czk: float | None = None, max_czk_14d: float | None = None):
        env = os.environ.get
        self.per_slot = per_slot if per_slot is not None else int(env("MENZA_MAX_PER_SLOT", DEFAULT_PER_SLOT))
        self.max_meal_czk = max_meal_czk if max_meal_czk is not None else float(
            env("MENZA_MAX_MEAL_CZK", DEFAULT_MAX_MEAL_CZK))
        self.max_czk_14d = max_czk_14d if max_czk_14d is not None else float(
            env("MENZA_MAX_CZK_14D", DEFAULT_MAX_CZK_14D))
        self.path = state_dir / "spending.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, served TEXT NOT NULL, slot TEXT NOT NULL,
                price REAL NOT NULL, order_id INTEGER, released INTEGER NOT NULL DEFAULT 0)""")
            db.commit()
        self.path.chmod(0o600)

    def _db(self):
        return closing(sqlite3.connect(self.path))

    def usage(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(PRAGUE)
        with self._db() as db:
            spent = db.execute("SELECT COALESCE(SUM(price), 0) FROM reservations WHERE released=0 AND at > ?",
                               ((now - WINDOW).isoformat(),)).fetchone()[0]
        return {"spent_14d": round(spent, 2), "max_czk_14d": self.max_czk_14d,
                "max_meal_czk": self.max_meal_czk, "max_per_meal_slot_per_day": self.per_slot}

    def reserve(self, meal: Meal, now: datetime | None = None) -> int:
        """Record an intended purchase or raise MenzaError if it would exceed a bound."""
        now = now or datetime.now(PRAGUE)
        if meal.price is None:
            raise MenzaError("meal price unknown; refusing to buy")
        if meal.price > self.max_meal_czk + 1e-9:
            raise MenzaError(f"meal costs {meal.price:.2f} Kč, above the per-meal limit {self.max_meal_czk:.2f} Kč")
        slot = meal_slot(meal.section)
        with self._db() as db:
            same = db.execute("SELECT COUNT(*) FROM reservations WHERE released=0 AND served=? AND slot=?",
                              (meal.date.isoformat(), slot)).fetchone()[0]
            if same >= self.per_slot:
                raise MenzaError(f"limit: already {same} {slot} purchase(s) for {meal.date.isoformat()}; "
                                 "cancel that one first (a confirmed refund frees the slot)")
            spent = db.execute("SELECT COALESCE(SUM(price), 0) FROM reservations WHERE released=0 AND at > ?",
                               ((now - WINDOW).isoformat(),)).fetchone()[0]
            if spent + meal.price > self.max_czk_14d + 1e-9:
                raise MenzaError(f"limit: {spent:.2f} Kč already spent in 14 days; this {meal.price:.2f} Kč "
                                 f"meal would exceed {self.max_czk_14d:.2f} Kč (change it only on the server)")
            cur = db.execute("INSERT INTO reservations (at, served, slot, price) VALUES (?, ?, ?, ?)",
                             (now.isoformat(), meal.date.isoformat(), slot, meal.price))
            db.commit()
            return cur.lastrowid

    def confirm(self, reservation: int, order_id: int) -> None:
        with self._db() as db:
            db.execute("UPDATE reservations SET order_id=? WHERE id=?", (order_id, reservation))
            db.commit()

    def release(self, reservation: int) -> None:
        """The site definitively refused the order: nothing was bought."""
        with self._db() as db:
            db.execute("UPDATE reservations SET released=1 WHERE id=?", (reservation,))
            db.commit()

    def release_refunded(self, order_id: int) -> None:
        """A cancellation was confirmed as a refund in the account balance."""
        with self._db() as db:
            db.execute("UPDATE reservations SET released=1 WHERE order_id=?", (order_id,))
            db.commit()
