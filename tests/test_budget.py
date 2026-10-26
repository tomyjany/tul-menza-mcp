from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from menza_hermes.budget import SpendGuard
from menza_hermes.client import MenzaError
from menza_hermes.models import PRAGUE, Meal

NOW = datetime(2027, 3, 15, 12, tzinfo=PRAGUE)


def meal(days_ahead=1, section="lunch", price=90.0):
    return Meal(NOW.date() + timedelta(days=days_ahead), 2, section, "1", "Synthetic dish", price, "150g",
                5, None, False, NOW + timedelta(days=days_ahead - 1), NOW + timedelta(days=days_ahead))


def test_confirmed_full_14_day_plan_fits_default_limits(tmp_path):
    guard = SpendGuard(tmp_path)
    weekdays = [d for d in range(1, 15) if (NOW.date() + timedelta(days=d)).weekday() < 5]
    for d in weekdays:
        guard.reserve(meal(d, "lunch", 95), NOW)
        guard.reserve(meal(d, "dinner", 70), NOW)
    assert guard.usage(NOW)["spent_14d"] == round(len(weekdays) * 165, 2)


def test_second_lunch_for_same_date_is_refused_but_dinner_is_not(tmp_path):
    guard = SpendGuard(tmp_path)
    guard.reserve(meal(3, "lunch"), NOW)
    guard.reserve(meal(3, "dinner"), NOW)
    with pytest.raises(MenzaError, match="lunch order"):
        guard.reserve(meal(3, "lunch_no_soup"), NOW)  # soup variant is still a lunch
    with pytest.raises(MenzaError, match="dinner order"):
        guard.reserve(meal(3, "dinner"), NOW)


def test_site_orders_count_once_alongside_own_purchases(tmp_path):
    guard = SpendGuard(tmp_path, per_slot=2)
    guard.confirm(guard.reserve(meal(5), NOW), order_id=77)
    guard.reserve(meal(5), NOW, existing_order_ids=[77])  # 77 is our own order, not a second one
    (tmp_path / "web").mkdir()
    with pytest.raises(MenzaError, match="already 2 lunch"):  # both made on the web, none reserved here
        SpendGuard(tmp_path / "web", per_slot=2).reserve(meal(5), NOW, existing_order_ids=[77, 88])


def test_per_meal_price_and_14_day_amount(tmp_path):
    guard = SpendGuard(tmp_path, max_czk_14d=200)
    with pytest.raises(MenzaError, match="per-meal"):
        guard.reserve(meal(1, price=151), NOW)
    guard.reserve(meal(1, price=120), NOW)
    with pytest.raises(MenzaError, match="14 days"):
        guard.reserve(meal(2, price=90), NOW)
    guard.reserve(meal(2, price=90), NOW + timedelta(days=14, seconds=1))  # first purchase left the window


def test_only_definitive_refusal_or_confirmed_refund_frees_budget(tmp_path):
    guard = SpendGuard(tmp_path)
    refused = guard.reserve(meal(4), NOW)
    guard.release(refused)  # site said no: nothing bought
    bought = guard.reserve(meal(4), NOW)
    guard.confirm(bought, order_id=77)
    with pytest.raises(MenzaError):
        guard.reserve(meal(4), NOW)
    guard.release_refunded(77)  # refund confirmed in balance
    guard.reserve(meal(4), NOW)
    assert SpendGuard(tmp_path).usage(NOW)["spent_14d"] == 90  # persists across processes
