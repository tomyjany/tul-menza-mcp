"""Fun statistics over the account history."""

from __future__ import annotations

from collections import Counter, defaultdict

from .models import Transaction

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _top(counter: Counter, n: int) -> list[dict]:
    return [{"value": k, "count": v} for k, v in counter.most_common(n)]


def compute_stats(history: list[Transaction]) -> dict:
    meals = [t for t in history if t.is_meal]
    spending = [t for t in history if t.amount < 0]
    if not history:
        return {"meals": 0}
    spent = -sum(t.amount for t in spending)
    by_month: dict[str, float] = defaultdict(float)
    for t in spending:
        by_month[t.when.strftime("%Y-%m")] += -t.amount
    priciest = min(meals, key=lambda t: t.amount) if meals else None
    return {
        "first": min(t.when for t in history).isoformat(),
        "last": max(t.when for t in history).isoformat(),
        "meals": len(meals),
        "total_spent": round(spent, 2),
        "total_topped_up": round(sum(t.amount for t in history if t.amount > 0), 2),
        "average_meal_price": round(-sum(t.amount for t in meals) / len(meals), 2) if meals else None,
        "most_expensive_meal": {"item": priciest.item, "price": -priciest.amount, "date": priciest.when.date().isoformat()}
        if priciest else None,
        "by_kind": _top(Counter(t.kind for t in history), 10),
        "by_place": _top(Counter(t.place for t in meals), 10),
        "favourite_dishes": _top(Counter(t.item for t in meals), 10),
        "by_weekday": _top(Counter(WEEKDAYS[t.when.weekday()] for t in meals), 7),
        "spent_by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
        "kiosk_purchases": _top(Counter(t.item for t in history if t.kind == "Platba kartou"), 5),
    }
