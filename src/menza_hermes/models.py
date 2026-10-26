"""Data model for menza.tul.cz."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")

CANTEENS = {"harcov": 1, "husova": 2, "voronezska": 3, "komenskeho": 4}
CANTEEN_NAMES = {1: "Harcov", 2: "Husova", 3: "Voroněžská", 4: "Komenského"}

# Section headers on a day page, normalised to a stable key.
SECTION_KEYS = {
    "obědy": "lunch",
    "obědy bez polévky": "lunch_no_soup",
    "večeře": "dinner",
    "minutky": "minutka",
    "s sebou": "takeaway",
    "studené": "cold",
    "jídlo na víkend": "weekend",
}


def canteen_slug(value: str | int) -> str:
    """Accept 'Harcov', 'husova', 1, '2' ... and return the URL slug."""
    if isinstance(value, int) or str(value).isdigit():
        by_id = {v: k for k, v in CANTEENS.items()}
        slug = by_id.get(int(value))
    else:
        slug = normalize(str(value)).replace(" ", "")
        slug = slug if slug in CANTEENS else None
    if slug is None:
        raise ValueError(f"unknown canteen {value!r}; use one of {', '.join(CANTEENS)}")
    return slug


def normalize(text: str) -> str:
    """Lower-case, strip diacritics and collapse whitespace (for matching dish names)."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()


def section_key(header: str) -> str:
    header = re.sub(r"\s+\d+$", "", header.strip()).lower()  # "Večeře 40" -> "večeře" (40 = burza offers)
    return SECTION_KEYS.get(header, normalize(header).replace(" ", "_"))


def meal_slot(section: str) -> str:
    """Menu section -> purchase slot; both lunch variants are one lunch."""
    return "dinner" if section == "dinner" else "lunch"


def order_slot(kind: str) -> str:
    """Orders-page CSS class -> slot. "vec" is dinner; anything else counts as lunch (stricter)."""
    return "dinner" if kind == "vec" else "lunch"


@dataclass(frozen=True)
class Meal:
    """One dish row on a day menu."""

    date: date
    canteen_id: int
    section: str  # lunch | lunch_no_soup | dinner | minutka | takeaway | ...
    number: str
    name: str
    price: float | None
    weight: str
    menu_id: int | None  # present only while the dish can be ordered (normal sale or burza)
    order_id: int | None  # set when I have ordered this dish
    direct_sale_only: bool  # "Pouze přímý prodej" (minutky)
    sell_from: datetime | None  # normal ordering ends / burza starts
    buy_until: datetime | None  # last moment to buy (incl. burza)
    burza_offers: int | None = None  # count shown in the section header during the burza phase
    allergens: list[str] = field(default_factory=list)

    @property
    def canteen(self) -> str:
        return CANTEEN_NAMES.get(self.canteen_id, str(self.canteen_id))

    @property
    def orderable(self) -> bool:
        return self.menu_id is not None and not self.direct_sale_only

    def phase(self, now: datetime) -> str:
        """'order' before sell_from, 'burza' until buy_until, then 'closed'."""
        if self.buy_until and now >= self.buy_until:
            return "closed"
        if self.sell_from and now >= self.sell_from:
            return "burza"
        return "order"

    def to_dict(self, now: datetime | None = None) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["canteen"] = self.canteen
        d["sell_from"] = self.sell_from.isoformat() if self.sell_from else None
        d["buy_until"] = self.buy_until.isoformat() if self.buy_until else None
        d["orderable"] = self.orderable
        d["status"] = (
            "ordered" if self.order_id else
            "direct_sale_only" if self.direct_sale_only else
            "available" if self.menu_id else "sold_out"
        )
        if now:
            d["phase"] = self.phase(now)
        return d


@dataclass(frozen=True)
class Order:
    """An upcoming order from the 'Objednávky' page."""

    date: date
    canteen: str
    name: str
    number: str
    order_id: int | None  # set while it's my active order
    exchange_id: int | None  # set while it's offered in the burza (can be restored)
    kind: str  # obe | vec | ... (css class used by the site)
    sell_from: datetime | None
    buy_until: datetime | None

    def to_dict(self, now: datetime | None = None) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["sell_from"] = self.sell_from.isoformat() if self.sell_from else None
        d["buy_until"] = self.buy_until.isoformat() if self.buy_until else None
        d["in_burza"] = self.exchange_id is not None
        if now and self.order_id:
            if self.buy_until and now >= self.buy_until:
                d["cancel_effect"] = "too late"
            elif self.sell_from and now >= self.sell_from:
                d["cancel_effect"] = "moves to burza (refunded only if someone buys it)"
            else:
                d["cancel_effect"] = "storno with full refund"
        return d


@dataclass(frozen=True)
class Transaction:
    """One row of the account history ('Historie pohybů na účtu')."""

    when: datetime
    ordered_at: datetime | None
    kind: str  # Obědy | Večeře | Minutky | Platba kartou | Vklad | ...
    item: str
    amount: float
    place: str

    @property
    def is_meal(self) -> bool:
        return self.kind in ("Obědy", "Večeře", "Minutky")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["when"] = self.when.isoformat()
        d["ordered_at"] = self.ordered_at.isoformat() if self.ordered_at else None
        return d
