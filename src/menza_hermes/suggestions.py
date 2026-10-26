"""Deterministic menu suggestions from actual meal orders, never purchases.

Lunch defaults to the site's ``lunch`` section (with soup). Set
``lunch_section='lunch_no_soup'`` explicitly to consider the no-soup menu;
transaction prices do not establish whether somebody wanted soup.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import date, timedelta

from .models import CANTEEN_NAMES, CANTEENS, Meal, Transaction, normalize

DEFAULT_LUNCH_CANTEENS = ("harcov", "husova")
DEFAULT_DINNER_CANTEEN = "harcov"
_STOP_WORDS = frozenset({"a", "bez", "do", "g", "na", "s", "se", "v", "z", "ze"})


def _words(name: str) -> frozenset[str]:
    return frozenset(normalize(name).split()) - _STOP_WORDS


def _place(place: str, candidates=tuple(CANTEENS)) -> str | None:
    # History often prefixes the canteen name with "Menza"; do not count
    # unidentified locations as evidence for any canteen.
    words = set(normalize(place).split())
    for canteen in candidates:
        if canteen in words:
            return canteen
    return None


def _name(slug: str) -> str:
    return CANTEEN_NAMES[CANTEENS[slug]]


def canteen_preferences(env=os.environ) -> tuple[tuple[str, ...], str]:
    """Operator-set lunch candidates and fixed dinner canteen (never MCP arguments)."""
    lunch = tuple(c.strip().lower() for c in env.get("MENZA_LUNCH_CANTEENS", "").split(",") if c.strip())
    dinner = env.get("MENZA_DINNER_CANTEEN", "").strip().lower()
    return lunch or DEFAULT_LUNCH_CANTEENS, dinner or DEFAULT_DINNER_CANTEEN


def _meal_key(meal: Meal) -> tuple:
    return (normalize(meal.name), meal.number, meal.canteen_id, meal.menu_id or 0,
            meal.order_id or 0, meal.price if meal.price is not None else -1, meal.weight, meal.name)


def _dish_evidence(meal: Meal, purchases: list[Transaction]) -> dict:
    name = normalize(meal.name)
    words = _words(meal.name)
    exact = 0
    shared = 0
    overlapping_words = 0
    matches: Counter[str] = Counter()
    for purchase in purchases:
        previous_name = normalize(purchase.item)
        if previous_name == name:
            exact += 1
        overlap = words & _words(purchase.item)
        if overlap:
            shared += 1
            overlapping_words += len(overlap)
            matches[purchase.item] += 1
    match = sorted(matches, key=lambda item: (-matches[item], normalize(item), item))[0] if matches else None
    return {
        "history_orders": len(purchases),
        "exact_orders": exact,
        "shared_token_orders": shared,
        "shared_tokens": overlapping_words,
        "matched_history_dish": match,
        "matched_history_dish_orders": matches[match] if match else 0,
    }


def _choose_dish(meals: list[Meal], purchases: list[Transaction]) -> tuple[Meal, dict]:
    candidates = [(meal, _dish_evidence(meal, purchases)) for meal in meals]
    # Exact dish frequency first, then lexical similarity; stable name/number
    # breaks ties, regardless of incoming menu/history ordering.
    return min(candidates, key=lambda pair: (
        -pair[1]["exact_orders"], -pair[1]["shared_token_orders"],
        -pair[1]["shared_tokens"], _meal_key(pair[0]),
    ))


def _dish_reason(evidence: dict, section: str) -> str:
    if evidence["exact_orders"]:
        count = evidence["exact_orders"]
        return f"This dish appears in {count} past {section} order(s)."
    if evidence["shared_token_orders"]:
        return (
            f"Shares dish-name words with {evidence['shared_token_orders']} past {section} order(s); "
            f"for example {evidence['matched_history_dish']!r} "
            f"({evidence['matched_history_dish_orders']} order(s))."
        )
    return f"No matching dish names in {section} history; deterministic menu-name tie-break."


def _score(evidence: dict | None) -> list[int] | None:
    """Lexicographically compared observed counts, not a predicted preference."""
    if evidence is None:
        return None
    return [evidence["exact_orders"], evidence["shared_token_orders"], evidence["shared_tokens"]]


def suggest_plan(
    history: list[Transaction], menus: list[Meal], start: date, days: int = 14,
    lunch_section: str = "lunch", lunch_canteens: tuple[str, ...] = DEFAULT_LUNCH_CANTEENS,
    dinner_canteen: str = DEFAULT_DINNER_CANTEEN,
) -> dict:
    """Return lunch/dinner plans for every date from ``start`` (1–14 days).

    ``lunch_section`` is explicitly ``'lunch'`` (with soup, the default) or
    ``'lunch_no_soup'``. This only suggests menu entries, never places orders.
    An existing menu order wins over any suggested purchase. No menu entry is
    treated as bookable unless it has a menu ID and is not direct-sale-only.
    ``score`` is the lexicographically ranked [exact-order count,
    shared-word order count, shared-word count], or None without a dish;
    ``evidence`` gives the raw history counts behind it.
    """
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 14:
        raise ValueError("days must be an integer between 1 and 14")
    if lunch_section not in ("lunch", "lunch_no_soup"):
        raise ValueError("lunch_section must be 'lunch' or 'lunch_no_soup'")
    lunch_canteens = tuple(lunch_canteens)
    if not lunch_canteens or any(c not in CANTEENS for c in (*lunch_canteens, dinner_canteen)):
        raise ValueError(f"canteens must be among {', '.join(CANTEENS)}")
    lunch_ids = {CANTEENS[c] for c in lunch_canteens}

    lunches = [t for t in history if t.kind == "Obědy" and t.amount < 0]
    dinners = [t for t in history if t.kind == "Večeře" and t.amount < 0]
    overall = Counter(_place(t.place, lunch_canteens) for t in lunches)
    by_weekday = Counter((_place(t.place, lunch_canteens), t.when.weekday()) for t in lunches)
    has_lunch_location_history = any(overall[c] for c in lunch_canteens)
    dinner_evidence = {
        "canteen_basis": "configured",
        "canteen_counts": {dinner_canteen: sum(_place(t.place) == dinner_canteen for t in dinners)},
    }
    grouped: dict[tuple[date, str], list[Meal]] = {}
    last = start + timedelta(days=days)
    for meal in menus:
        if start <= meal.date < last:
            grouped.setdefault((meal.date, meal.section), []).append(meal)

    plan = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        weekday = day.weekday()
        has_weekday_history = any(by_weekday[c, weekday] for c in lunch_canteens)
        rank = sorted(lunch_canteens, key=lambda c: (
            -by_weekday[c, weekday] if has_weekday_history else -overall[c],
            -overall[c], lunch_canteens.index(c),
        ))
        preferred = rank[0]
        lunch_rows = [m for m in grouped.get((day, lunch_section), []) if m.canteen_id in lunch_ids]
        dinner_rows = [m for m in grouped.get((day, "dinner"), []) if m.canteen_id == CANTEENS[dinner_canteen]]

        canteen_counts = {
            c: {"same_weekday_orders": by_weekday[c, weekday], "overall_orders": overall[c]}
            for c in lunch_canteens
        }
        basis = "same_weekday" if has_weekday_history else "overall"
        lunch_evidence = {"canteen_basis": basis, "canteen_counts": canteen_counts}
        lunch_orders = [
            m for section in ("lunch", "lunch_no_soup")
            for m in grouped.get((day, section), []) if m.order_id is not None
        ]
        if lunch_orders:
            chosen, dish = _choose_dish(lunch_orders, lunches)
            lunch = {
                "status": "ordered", "canteen": chosen.canteen,
                "meal": chosen.to_dict(), "reason": "Already ordered; do not order lunch again.",
                "evidence": {**lunch_evidence, **dish, "existing_orders": len({m.order_id for m in lunch_orders})},
                "score": _score(dish),
            }
        else:
            available = [m for m in lunch_rows if m.orderable]
            canteen = next((c for c in rank if any(m.canteen_id == CANTEENS[c] for m in available)), None)
            if canteen is None:
                canteen = next(
                    (c for c in rank if any(
                        m.canteen_id == CANTEENS[c] and not m.direct_sale_only for m in lunch_rows
                    )),
                    preferred,
                )
            candidates = [m for m in available if m.canteen_id == CANTEENS[canteen]]
            if not candidates:
                candidates = [m for m in lunch_rows if m.canteen_id == CANTEENS[canteen] and not m.direct_sale_only]
            chosen, dish = _choose_dish(candidates, lunches) if candidates else (None, None)
            status = "available" if chosen and chosen.orderable else "unavailable"
            if not has_lunch_location_history and canteen == preferred:
                canteen_reason = f"No recorded lunch canteen orders; {_name(preferred)} is the default."
            elif canteen == preferred:
                canteen_reason = (
                    f"{_name(canteen)} chosen from {canteen_counts[canteen][basis + '_orders']} "
                    f"{basis.replace('_', ' ')} lunch order(s)."
                )
            else:
                canteen_reason = (
                    f"{_name(preferred)} has no bookable {lunch_section} dishes; "
                    f"{_name(canteen)} has bookable dishes."
                    if available else
                    f"{_name(preferred)} has no preorder {lunch_section} dishes; "
                    f"{_name(canteen)} lists an unavailable preorder dish."
                )
            if chosen:
                reason = f"{canteen_reason} {_dish_reason(dish, 'lunch')}"
                if status == "unavailable":
                    reason += " This dish is not bookable (sold out or ordering unavailable)."
            elif any(m.canteen_id == CANTEENS[canteen] for m in lunch_rows):
                reason = f"{canteen_reason} Only direct-sale dishes listed; no lunch preorder available."
            else:
                reason = f"{canteen_reason} No {lunch_section} menu listed; canteen may be closed."
            lunch = {
                "status": status, "canteen": _name(canteen),
                "meal": chosen.to_dict() if chosen else None,
                "reason": reason, "evidence": {**lunch_evidence, **(dish or {})},
                "score": _score(dish),
            }

        dinner_orders = [m for m in grouped.get((day, "dinner"), []) if m.order_id is not None]
        if dinner_orders:
            chosen, dish = _choose_dish(dinner_orders, dinners)
            dinner = {
                "status": "ordered", "canteen": chosen.canteen, "meal": chosen.to_dict(),
                "reason": "Already ordered dinner; do not order dinner again.",
                "evidence": {**dinner_evidence, **dish, "existing_orders": len({m.order_id for m in dinner_orders})},
                "score": _score(dish),
            }
        else:
            available = [m for m in dinner_rows if m.orderable]
            candidates = available or [m for m in dinner_rows if not m.direct_sale_only]
            chosen, dish = _choose_dish(candidates, dinners) if candidates else (None, None)
            status = "available" if chosen and chosen.orderable else "unavailable"
            if chosen:
                reason = f"Dinner is always at {_name(dinner_canteen)}. {_dish_reason(dish, 'dinner')}"
                if status == "unavailable":
                    reason += " This dish is not bookable (sold out or ordering unavailable)."
            elif dinner_rows:
                reason = (f"Dinner is always at {_name(dinner_canteen)}. Only direct-sale dishes listed; "
                          "no dinner preorder available.")
            else:
                reason = f"Dinner is always at {_name(dinner_canteen)}. No dinner menu listed; canteen may be closed."
            dinner = {
                "status": status, "canteen": _name(dinner_canteen), "meal": chosen.to_dict() if chosen else None,
                "reason": reason, "evidence": {**dinner_evidence, **(dish or {})},
                "score": _score(dish),
            }
        plan.append({"date": day.isoformat(), "lunch": lunch, "dinner": dinner})
    return {"start": start.isoformat(), "days": days, "lunch_section": lunch_section, "plan": plan}
