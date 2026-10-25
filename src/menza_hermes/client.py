"""HTTP client for menza.tul.cz with persistent, owner-only session cookies."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import date, datetime, timedelta
from functools import wraps
from http.cookiejar import Cookie
from pathlib import Path

import requests

from .auth import BASE_URL, login
from .models import PRAGUE, Meal, Order, Transaction, canteen_slug
from .parser import is_logged_in, parse_balance, parse_day, parse_history, parse_orders

USER_AGENT = "menza-hermes/0.1 (+https://github.com/tomyjany/tul-menza-mcp)"
_COOKIE_FIELDS = ("version", "name", "value", "port", "port_specified", "domain", "domain_specified",
                  "domain_initial_dot", "path", "path_specified", "secure", "expires", "discard",
                  "comment", "comment_url", "rfc2109")


class MenzaError(RuntimeError):
    pass


class _UncertainAction(MenzaError):
    """A POST may have reached the server; reconciliation must not replay it."""


def _serialized(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapped


def _today() -> date:
    return datetime.now(PRAGUE).date()


def _same_meal(a: Meal, b: Meal) -> bool:
    return (a.date, a.canteen_id, a.section, a.number, a.name) == (
        b.date, b.canteen_id, b.section, b.number, b.name)


def _same_order(a: Order, b: Order) -> bool:
    return (a.date, a.canteen, a.number, a.name, a.kind) == (
        b.date, b.canteen, b.number, b.name, b.kind)


class MenzaClient:
    def __init__(self, username: str, password: str, *, state_dir: Path, timeout: float = 30.0):
        self.username = username
        self._password = password
        self.timeout = timeout
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self._cookie_file = self.state_dir / "cookies.json"
        self._lock = threading.RLock()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._load_cookies()

    # ---- session -----------------------------------------------------------
    def _load_cookies(self) -> None:
        try:
            data = json.loads(self._cookie_file.read_text())
            for entry in data:
                if all(key in entry for key in _COOKIE_FIELDS):
                    cookie = Cookie(**{key: entry[key] for key in _COOKIE_FIELDS}, rest=entry.get("rest", {}))
                else:  # one-time migration of the original minimal cookie file
                    cookie = requests.cookies.create_cookie(**entry)
                if cookie.domain.lstrip(".") in ("menza.tul.cz", "shibbo.tul.cz", "login.microsoftonline.com"):
                    self.session.cookies.set_cookie(cookie)
            self.session.cookies.clear_expired_cookies()
        except (OSError, ValueError, KeyError, TypeError):
            pass  # stale/corrupt state is harmless; login will create a new session

    def _save_cookies(self) -> None:
        data = [{**{key: getattr(c, key) for key in _COOKIE_FIELDS}, "rest": c._rest}
                for c in self.session.cookies]
        fd, temporary = tempfile.mkstemp(prefix=".cookies-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self._cookie_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @_serialized
    def login(self) -> None:
        login(self.session, self.username, self._password, timeout=self.timeout)
        self._save_cookies()

    def _get(self, path: str) -> str:
        for attempt in range(2):
            try:
                r = self.session.get(BASE_URL + path, timeout=self.timeout, allow_redirects=False)
            except requests.RequestException:
                raise MenzaError("menza read failed due to a network error") from None
            if r.is_redirect or not is_logged_in(r.text):
                if attempt == 0:
                    self.login()
                    continue
                raise MenzaError("still logged out after signing in")
            if r.status_code >= 400:
                raise MenzaError("menza rejected a read request")
            return r.text
        raise MenzaError("menza session could not be established")

    def _action(self, path: str, data: dict) -> None:
        """Send a mutation once. A failure may have occurred after server-side commit."""
        try:
            r = self.session.post(BASE_URL + path, data=data, timeout=self.timeout,
                                  allow_redirects=False,
                                  headers={"X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL,
                                           "Referer": BASE_URL + path})
        except requests.RequestException:
            raise _UncertainAction("menza mutation outcome is uncertain after network failure") from None
        if r.is_redirect:
            raise _UncertainAction("menza mutation redirected; outcome is uncertain")
        if r.status_code == 412:
            raise MenzaError("menza refused the request")
        if r.status_code >= 400:
            raise _UncertainAction("menza mutation failed; outcome is uncertain")
        if not is_logged_in(r.text):
            raise _UncertainAction("menza mutation did not return an authenticated page")

    # ---- reads -------------------------------------------------------------
    @_serialized
    def day(self, canteen: str | int, day: date) -> list[Meal]:
        return parse_day(self._get(f"/{canteen_slug(canteen)}/{day.isoformat()}/"), day)

    @_serialized
    def orders(self) -> list[Order]:
        today = _today()
        return parse_orders(self._get(f"/objednavky/{today.isoformat()}/"), today)

    @_serialized
    def balance(self) -> dict:
        return parse_balance(self._get(f"/konto/{_today().isoformat()}/"))

    @_serialized
    def history(self, since: date, until: date | None = None) -> list[Transaction]:
        until = until or _today()
        self._get("/historie/")
        try:
            r = self.session.post(BASE_URL + "/historie/", timeout=self.timeout, allow_redirects=False,
                                  data={"HIST": "true", "hfrom": since.isoformat(), "hto": until.isoformat()})
        except requests.RequestException:
            raise MenzaError("menza history read failed due to a network error") from None
        if r.is_redirect:
            raise MenzaError("history request redirected; inspect authentication")
        # HIST returns only the table fragment, without the logged-in navbar.
        # parse_history rejects login pages or missing/malformed result tables.
        if r.status_code >= 400:
            raise MenzaError("menza rejected history request")
        return parse_history(r.text)

    # ---- actions -----------------------------------------------------------
    @_serialized
    def order(self, canteen: str | int, day: date, menu_id: int, max_price: float | None = None) -> Meal:
        slug = canteen_slug(canteen)
        meal = next((m for m in self.day(slug, day) if m.menu_id == menu_id), None)
        if meal is None:
            raise MenzaError("menu item is not available")
        if meal.order_id is not None:
            return meal  # already ours, no second charge
        if not meal.orderable or meal.buy_until is None or meal.phase(datetime.now(PRAGUE)) == "closed":
            raise MenzaError("meal is not open for ordering")
        if meal.price is None:
            raise MenzaError("meal price is unavailable; cannot place an order")
        if max_price is not None and (not math.isfinite(max_price) or max_price < 0 or meal.price > max_price):
            raise MenzaError("current meal price exceeds the authorized maximum")
        # Recheck the live menu immediately before submitting the POST, including price.
        fresh = next((m for m in self.day(slug, day) if m.menu_id == menu_id), None)
        if fresh is None or not _same_meal(meal, fresh) or fresh.price != meal.price:
            raise MenzaError("meal or price changed before ordering")
        if fresh.order_id is not None:
            return fresh
        if not fresh.orderable or fresh.buy_until is None or fresh.phase(datetime.now(PRAGUE)) == "closed":
            raise MenzaError("meal is no longer open for ordering")
        uncertain = False
        try:
            self._action(f"/{slug}/{day.isoformat()}/",
                         {"ORDER": "", "JID": str(menu_id), "CANTEEN": str(fresh.canteen_id)})
        except _UncertainAction:
            uncertain = True
        try:
            confirmed = [m for m in self.day(slug, day) if _same_meal(fresh, m) and m.order_id is not None]
        except (MenzaError, ValueError):
            raise MenzaError("order outcome is uncertain; check orders before trying again") from None
        if len(confirmed) == 1:
            return confirmed[0]
        if uncertain:
            raise MenzaError("order outcome is uncertain; check orders before trying again")
        raise MenzaError("order was not confirmed by the site; do not repeat automatically")

    @_serialized
    def cancel(self, order_id: int) -> None:
        original = next((o for o in self.orders() if o.order_id == order_id), None)
        if original is None:
            raise MenzaError("active order not found in this account")
        uncertain = False
        try:
            self._action(f"/objednavky/{_today().isoformat()}/", {"CANCEL": "", "OID": str(order_id)})
        except _UncertainAction:
            uncertain = True
        try:
            after = self.orders()
        except (MenzaError, ValueError):
            raise MenzaError("cancel outcome is uncertain; check orders before trying again") from None
        if not any(o.order_id == order_id for o in after):
            return  # may now be offered on the exchange; this does NOT promise a refund
        if uncertain:
            raise MenzaError("cancel outcome is uncertain; check orders before trying again")
        raise MenzaError("order is still active after cancelling")

    @_serialized
    def restore(self, exchange_id: int) -> None:
        original = next((o for o in self.orders() if o.exchange_id == exchange_id), None)
        if original is None:
            raise MenzaError("exchange offer not found in this account")
        uncertain = False
        try:
            self._action(f"/objednavky/{_today().isoformat()}/", {"RESTORE": "", "OID": str(exchange_id)})
        except _UncertainAction:
            uncertain = True
        try:
            after = self.orders()
        except (MenzaError, ValueError):
            raise MenzaError("restore outcome is uncertain; check orders before trying again") from None
        if (not any(o.exchange_id == exchange_id for o in after) and
                any(_same_order(o, original) and o.order_id is not None for o in after)):
            return
        if uncertain:
            raise MenzaError("restore outcome is uncertain; check orders before trying again")
        raise MenzaError("restored order was not confirmed by the site")

    @_serialized
    def days_ahead(self, canteen: str | int, days: int) -> list[Meal]:
        today = _today()
        out: list[Meal] = []
        for i in range(days + 1):
            out.extend(self.day(canteen, today + timedelta(days=i)))
        return out
