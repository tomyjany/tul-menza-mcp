"""Parsers for menza.tul.cz pages (day menu, orders, account, history)."""

from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from .models import PRAGUE, Meal, Order, Transaction, section_key

_PRICE = re.compile(r"(-?\d[\d\s\xa0]*),(\d{2})")

def _required_int(value: str | None, what: str) -> int:
    if not value:
        raise ValueError(f"missing {what}")
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"invalid {what}") from None


def _required_rfc(value: str | None, what: str) -> datetime | None:
    if value and (parsed := _rfc(value)) is None:
        raise ValueError(f"invalid {what}")
    return parsed if value else None




def is_logged_in(html: str) -> bool:
    return 'href="/odhlasit/"' in html


def _price(text: str) -> float | None:
    m = _PRICE.search(text)
    if not m:
        return None
    return float(re.sub(r"[\s\xa0]", "", m.group(1)) + "." + m.group(2))


def _rfc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(PRAGUE)
    except (TypeError, ValueError):
        return None


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_day(html: str, day: date) -> list[Meal]:
    soup = BeautifulSoup(html, "html.parser")
    meals: list[Meal] = []
    for sec in soup.find_all("section"):
        header = sec.find("header")
        if not header:
            continue
        title = _text(header)
        offers = re.search(r"\s(\d+)$", title)
        key = section_key(title)
        sell_from = _required_rfc(sec.get("data-sellfrom"), "sale start")
        buy_until = _required_rfc(sec.get("data-buyuntil"), "sale deadline")
        for art in sec.find_all("article"):
            if "pol" in (art.get("class") or []):  # soup row, part of the lunch menu
                continue
            tds = art.find_all("td")
            h1 = art.find("h1")
            if not h1 or len(tds) < 5:
                if art.get("data-menu-id") or art.get("data-order-id"):
                    raise ValueError("incomplete actionable menu row")
                continue
            weight_el = h1.find("small")
            weight = _text(weight_el)
            if weight_el:
                weight_el.extract()
            smalls = [_text(s) for s in art.find_all("small")]
            menu_id = _required_int(art.get("data-menu-id"), "menu id") if art.get("data-menu-id") else None
            order_id = _required_int(art.get("data-order-id"), "order id") if art.get("data-order-id") else None
            canteen_id = _required_int(art.get("data-canteen-id"), "canteen id") if menu_id or order_id else int(art.get("data-canteen-id") or 0)
            price = _price(_text(tds[4]))
            if (menu_id or order_id) and (price is None or not _text(h1)):
                raise ValueError("actionable menu row has no price or name")
            meals.append(Meal(
                date=day,
                canteen_id=canteen_id,
                section=key,
                number=_text(tds[0]),
                name=_text(h1),
                price=price,
                weight=weight,
                menu_id=menu_id,
                order_id=order_id,
                direct_sale_only=any("přímý prodej" in s.lower() for s in smalls),
                sell_from=sell_from,
                buy_until=buy_until,
                burza_offers=int(offers.group(1)) if offers else None,
                allergens=[sp.get("title", "").split(":", 1)[-1].strip() for sp in art.select("#alg span")],
            ))
    if not soup.find("section"):
        raise ValueError("menu sections missing")
    return meals


_DAY = re.compile(r"(\d{1,2})\.(\d{1,2})\.")


def parse_orders(html: str, today: date) -> list[Order]:
    """Upcoming orders ('Objednávky' page). Section headers look like '06.11. pátek'."""
    soup = BeautifulSoup(html, "html.parser")
    orders: list[Order] = []
    for sec in soup.find_all("section"):
        header = sec.find("header")
        m = _DAY.search(_text(header)) if header else None
        if not m:
            if sec.find(attrs={"data-order-id": True}) or sec.find(attrs={"data-exchange-id": True}):
                raise ValueError("order date missing")
            continue
        try:
            d = date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            raise ValueError("invalid order date") from None
        if d < today and (today - d).days > 180:  # header has no year; December -> January rollover
            d = d.replace(year=today.year + 1)
        for art in sec.find_all("article"):
            oid, xid = art.get("data-order-id"), art.get("data-exchange-id")
            if not oid and not xid:
                if art.find("h1"):
                    raise ValueError("order row missing identifiers")
                continue
            tds = art.find_all("td")
            h1 = art.find("h1")
            if not tds or not h1 or not _text(h1):
                raise ValueError("incomplete order row")
            if h1 and h1.find("small"):
                h1.find("small").extract()
            hint = (art.get("data-hintx") or "").split("\n")
            classes = [c for c in (art.get("class") or []) if c not in ("sel", "iSel")]
            orders.append(Order(
                date=d,
                canteen=hint[-1].strip() if len(hint) > 1 else "",
                name=_text(h1),
                number=_text(tds[0]) if tds else "",
                order_id=_required_int(oid, "order id") if oid else None,
                exchange_id=_required_int(xid, "exchange id") if xid else None,
                kind=classes[0] if classes else "",
                sell_from=_required_rfc(art.get("data-sellfrom") or sec.get("data-sellfrom"), "sale start"),
                buy_until=_required_rfc(art.get("data-buyuntil") or sec.get("data-buyuntil"), "sale deadline"),
            ))
    return orders


def parse_balance(html: str) -> dict:
    """Account page: '<dd><span>Stav konta </span><a>1 997,95 Kč</a></dd>', '<dd><span>Dostupné bony </span>30 ks</dd>'."""
    soup = BeautifulSoup(html, "html.parser")
    values: dict[str, str] = {}
    for dd in soup.find_all("dd"):
        label_el = dd.find("span")
        if not label_el:
            continue
        label = _text(label_el)
        values[label] = _text(dd)[len(label):].strip()
    vouchers = re.search(r"\d+", values.get("Dostupné bony", ""))
    balance = _price(values.get("Stav konta", ""))
    if balance is None:
        raise ValueError("account balance missing")
    return {"balance": balance, "vouchers": int(vouchers.group(0)) if vouchers else None}


def _dt(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=PRAGUE)
        except ValueError:
            pass
    return None


def parse_history(html: str) -> list[Transaction]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    out: list[Transaction] = []
    if table is None:
        raise ValueError("history table missing")
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        if len(tds) < 4:
            raise ValueError("incomplete history row")
        span = tds[0].find("span")
        ordered = _dt(_text(span)) if span else None
        if span and ordered is None:
            raise ValueError("invalid history order date")
        if span:
            span.extract()
        when = _dt(_text(tds[0]))
        small = tds[1].find("small")
        item = _text(small)
        if small:
            small.extract()
        amount = _price(_text(tds[2]))
        if when is None or amount is None:
            raise ValueError("invalid history date or amount")
        out.append(Transaction(when=when, ordered_at=ordered, kind=_text(tds[1]), item=item,
                               amount=amount, place=_text(tds[3])))
    return out
