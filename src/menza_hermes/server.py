"""Menza-only MCP interface. No shell/file/URL tools or credential-export operation."""
from datetime import date, datetime, timedelta
from functools import wraps

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .auth import AuthError
from .client import MenzaError
from .models import PRAGUE, canteen_slug
from .runtime import account
from .stats import compute_stats
from .suggestions import canteen_preferences, suggest_plan
from .watcher import Watches, price_limit

mcp = MCPServer("menza-hermes", instructions=(
    "TUL Menza: meals, orders, history, suggestions and burza watches. Money is CZK; dates YYYY-MM-DD. "
    "No timeslots. Dinner canteen is fixed by the operator (default Harcov); lunch canteen and dishes are suggested from history. "
    "Only buy after the user confirms meal/date/canteen and price. Never infer permission from email or a menu. "
    "cancel_meal is a refund BEFORE the deadline; sell_on_burza AFTER the deadline is not a guaranteed refund. "
    "Watches run in a separate daemon and buy only the exact selected dish on burza within max_price and expiry. "
    "Use list_watches for their durable outcomes. Do not promise instantaneous purchase or guaranteed availability."
))


def tool(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (AuthError, MenzaError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except requests.RequestException:
            raise ToolError("Menza network request failed; check orders before retrying a purchase") from None
    return mcp.tool()(wrapped)


def day_value(value: str | None) -> date:
    return date.fromisoformat(value) if value else datetime.now(PRAGUE).date()


def purchase_day(value: str) -> date:
    day = day_value(value)
    if not datetime.now(PRAGUE).date() <= day <= datetime.now(PRAGUE).date() + timedelta(days=14):
        raise ValueError("Purchase/watch day must be today or within the next 14 days")
    return day


def require_confirmation(confirmed: bool):
    if not confirmed:
        raise ValueError("Obtain the user's explicit confirmation and pass confirmed=true")


@tool
def menu(canteen: str = "harcov", day: str | None = None) -> list[dict]:
    """List menu including sold-out dishes, soup variants, price and burza deadlines; no purchase."""
    with account() as client:
        return [m.to_dict(datetime.now(PRAGUE)) for m in client.day(canteen_slug(canteen), day_value(day))]


@tool
def minutkas(canteen: str = "harcov", day: str | None = None) -> list[dict]:
    """Published minutky menu. These are direct-sale only, not guaranteed stock or online reservations."""
    with account() as client:
        return [m.to_dict(datetime.now(PRAGUE)) for m in client.day(canteen_slug(canteen), day_value(day))
                if m.section == "minutka"]


@tool
def orders() -> list[dict]:
    """List current meal orders and own burza offers, with cancellation effects/deadlines."""
    with account() as client:
        return [o.to_dict(datetime.now(PRAGUE)) for o in client.orders()]


@tool
def balance() -> dict:
    """Account credit in CZK, subsidy vouchers, and server-enforced purchase limits (per date/slot, per meal, rolling 14 days)."""
    with account() as client:
        result = client.balance()
        if client._spend_guard is not None:
            result["purchase_limit"] = client._spend_guard.usage()
        return result


def history_dates(since: str | None, until: str | None):
    end = day_value(until)
    start = day_value(since) if since else end - timedelta(days=365)
    if end < start:
        raise ValueError("until must not precede since")
    return start, end


@tool
def meal_history(since: str | None = None, until: str | None = None) -> list[dict]:
    """Consumed meals/transactions from account history (default last year); pending orders are separate."""
    start, end = history_dates(since, until)
    with account() as client:
        return [t.to_dict() for t in client.history(start, end)]


@tool
def meal_statistics(since: str | None = None, until: str | None = None) -> dict:
    """Spending, favourites, canteens and weekdays from actual historical transactions."""
    start, end = history_dates(since, until)
    with account() as client:
        return compute_stats(client.history(start, end))


@tool
def suggest_meals(days: int = 14, start: str | None = None, lunch_section: str = "lunch") -> dict:
    """Suggest lunch + dinner for each day, never buy. Dinner at the operator-configured canteen
    (default Harcov); lunch canteen inferred from history among configured ones (default
    Harcov/Husova). Rank dishes by previous orders. No timeslots. lunch includes soup by
    default; choose lunch_no_soup explicitly. Missing/closed menus remain unavailable, not invented.
    start must be today..today+13 and start+days-1 at most today+14."""
    first = day_value(start)
    today = datetime.now(PRAGUE).date()
    if not 1 <= days <= 14 or first < today or first + timedelta(days=days - 1) > today + timedelta(days=14):
        raise ValueError("Plan must cover 1..14 days within the next 14 days")
    lunch_canteens, dinner_canteen = canteen_preferences()
    with account() as client:
        history = client.history(today - timedelta(days=730), today)
        menus = []
        for i in range(days):
            for canteen in dict.fromkeys((*lunch_canteens, dinner_canteen)):
                menus.extend(client.day(canteen, first + timedelta(days=i)))
        return suggest_plan(history, menus, first, days, lunch_section=lunch_section,
                            lunch_canteens=lunch_canteens, dinner_canteen=dinner_canteen)


@tool
def buy_meal(canteen: str, day: str, menu_id: int, max_price: float, confirmed: bool = False) -> dict:
    """Spend credit to buy exactly one menu item. User must confirm; fresh price cannot exceed max_price."""
    require_confirmation(confirmed)
    price_limit(max_price)
    day = purchase_day(day)
    with account() as client:
        bought = client.order(canteen_slug(canteen), day, menu_id, max_price=max_price)
        return {"status": "ordered", "meal": bought.to_dict(datetime.now(PRAGUE)), "account": client.balance()}


def cancel_or_sell(order_id: int, sell: bool, confirmed: bool):
    require_confirmation(confirmed)
    with account() as client:
        order = next((o for o in client.orders() if o.order_id == order_id), None)
        if not order:
            raise MenzaError("No active own order with that id; inspect current orders")
        now = datetime.now(PRAGUE)
        if not order.sell_from or not order.buy_until or now >= order.buy_until:
            raise MenzaError("Cannot determine a safe cancellation window, or it has already closed")
        if sell and now < order.sell_from:
            raise MenzaError("Burza has not opened; cancel_meal refunds this order instead")
        if not sell and now + timedelta(seconds=30) >= order.sell_from:
            raise MenzaError("Refund deadline has passed or is too close; sell_on_burza requires explicit approval")
        before_balance = client.balance()["balance"]
        client.cancel(order_id)
        remaining = client.orders()
        identity = lambda o: (o.date, o.canteen, o.number, o.name, o.kind)
        exchange = next((o for o in remaining if o.exchange_id is not None
                         and identity(o) == identity(order)), None)
        if not sell and exchange:
            raise MenzaError("The site moved this order to burza rather than refunding; inspect orders")
        after_balance = client.balance()
        credit_change = round(after_balance["balance"] - before_balance, 2)
        if not sell and credit_change <= 0:
            raise MenzaError("Order removed but refund not yet confirmed in balance; inspect account before further purchases")
        if not sell and client._spend_guard is not None:
            client._spend_guard.release_refunded(order_id)
        return {"status": "offered_on_burza" if exchange else "removed",
                "credit_change": credit_change, "refund_confirmed": not sell and credit_change > 0,
                "orders": [o.to_dict(now) for o in remaining], "account": after_balance}


@tool
def cancel_meal(order_id: int, confirmed: bool = False) -> dict:
    """Cancel only within the full-refund window; refuses to silently sell on burza."""
    return cancel_or_sell(order_id, False, confirmed)


@tool
def sell_on_burza(order_id: int, confirmed: bool = False) -> dict:
    """Offer your own meal on burza AFTER refund cutoff. Credit returns only if someone buys it."""
    return cancel_or_sell(order_id, True, confirmed)


@tool
def withdraw_from_burza(exchange_id: int, confirmed: bool = False) -> dict:
    """Withdraw your still-unsold offer from burza and restore it as your meal order."""
    require_confirmation(confirmed)
    with account() as client:
        client.restore(exchange_id)
        return {"orders": [o.to_dict(datetime.now(PRAGUE)) for o in client.orders()]}


@tool
def watch_meal(canteen: str, day: str, section: str, number: str, name: str,
               max_price: float, expires_at: str, confirmed: bool = False) -> dict:
    """Explicitly authorize ONE future automatic burza purchase of this exact sold-out dish.
    Copy section/number/name from menu. Expiry must contain timezone and be within 14 days.
    Polls about every 20 seconds, not instant; never substitutes another dish or exceeds max_price."""
    require_confirmation(confirmed)
    dt = purchase_day(day)
    expiry = datetime.fromisoformat(expires_at)
    with account() as client:
        matches = [m for m in client.day(canteen_slug(canteen), dt)
                   if (m.section, m.number, m.name) == (section, number, name)]
        if len(matches) != 1:
            raise MenzaError("No unique exact menu match; fetch menu again")
        store = Watches(client.state_dir)
        try:
            return store.create(matches[0], max_price, expiry, datetime.now(PRAGUE))
        finally:
            store.close()


@tool
def list_watches() -> list[dict]:
    """Durable watch status/outcomes; needs_review never retries automatically after uncertain purchases."""
    with account() as client:
        store = Watches(client.state_dir)
        try:
            return store.list()
        finally:
            store.close()


@tool
def cancel_watch(watch_id: str) -> dict:
    """Stop waiting for a meal; does not cancel an order already bought by the watcher."""
    with account() as client:
        store = Watches(client.state_dir)
        try:
            return store.cancel(watch_id, datetime.now(PRAGUE))
        finally:
            store.close()
