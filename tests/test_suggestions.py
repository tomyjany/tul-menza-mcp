"""Synthetic, fixed-date tests; no account details or network access."""

from datetime import date, datetime

import pytest

from menza_hermes.models import Meal, Transaction
from menza_hermes.suggestions import suggest_plan

MONDAY = date(2026, 10, 26)


def purchase(day: date, kind: str, item: str, place: str, amount: float = -75.0) -> Transaction:
    return Transaction(datetime.combine(day, datetime.min.time()), None, kind, item, amount, place)


def meal(
    day: date, name: str, canteen: int = 1, section: str = "lunch",
    menu_id: int | None = 1, order_id: int | None = None,
    direct_sale_only: bool = False,
) -> Meal:
    return Meal(day, canteen, section, "1", name, 88.0, "300 g", menu_id,
                order_id, direct_sale_only, None, None)


def test_weekday_lunch_location_and_fixed_harcov_dinner():
    history = [
        purchase(date(2026, 10, 19), "Obědy", "Kuřecí řízek", "Menza Husova"),
        purchase(date(2026, 10, 12), "Obědy", "Rýže", "Husova"),
        purchase(date(2026, 10, 20), "Obědy", "Těstoviny", "Harcov"),
        purchase(date(2026, 10, 21), "Obědy", "Salát", "Harcov"),
        purchase(date(2026, 10, 22), "Obědy", "Guláš", "Harcov"),
    ]
    menus = [meal(MONDAY, "Kuřecí řízek", 2), meal(MONDAY, "Salát"),
             meal(MONDAY, "Večerní guláš", 1, "dinner"),
             meal(MONDAY, "Večeře Husova", 2, "dinner")]
    day = suggest_plan(history, menus, MONDAY, 1)["plan"][0]
    assert day["lunch"]["canteen"] == "Husova"
    assert day["lunch"]["status"] == "available"
    assert day["lunch"]["meal"]["name"] == "Kuřecí řízek"
    assert day["lunch"]["evidence"]["canteen_basis"] == "same_weekday"
    assert day["lunch"]["evidence"]["canteen_counts"]["husova"] == {
        "same_weekday_orders": 2, "overall_orders": 2,
    }
    assert day["dinner"]["canteen"] == "Harcov"
    assert day["dinner"]["meal"]["name"] == "Večerní guláš"


def test_lunch_uses_overall_history_if_weekday_unknown_and_bookable_fallback():
    history = [purchase(date(2026, 10, 20), "Obědy", "Řízek", "Husova")]
    menus = [meal(MONDAY, "Brambory", 2, menu_id=None), meal(MONDAY, "Rýže", 1)]
    day = suggest_plan(history, menus, MONDAY, 1)["plan"][0]
    assert day["lunch"]["canteen"] == "Harcov"
    assert day["lunch"]["status"] == "available"
    assert day["lunch"]["evidence"]["canteen_basis"] == "overall"
    assert day["lunch"]["evidence"]["canteen_counts"]["husova"]["overall_orders"] == 1
    assert "Husova has no bookable" in day["lunch"]["reason"]
    assert day["dinner"]["status"] == "unavailable"


def test_exact_dish_count_then_name_word_overlap_and_permutation_stability():
    history = [
        purchase(date(2026, 10, 19), "Obědy", "Hovězí guláš s rýží", "Harcov"),
        purchase(date(2026, 10, 12), "Obědy", "Hovězí guláš s rýží", "Harcov"),
        purchase(date(2026, 10, 5), "Obědy", "Kuřecí salát", "Harcov"),
        purchase(date(2026, 10, 19), "Večeře", "Zapečené těstoviny", "Harcov"),
    ]
    menus = [meal(MONDAY, "Kuřecí salát"), meal(MONDAY, "Hovězí guláš s rýží"),
             meal(MONDAY, "Těstoviny zapečené", section="dinner"),
             meal(MONDAY, "Jiné jídlo", section="dinner")]
    first = suggest_plan(history, menus, MONDAY, 1)
    assert first == suggest_plan(list(reversed(history)), list(reversed(menus)), MONDAY, 1)
    lunch = first["plan"][0]["lunch"]
    dinner = first["plan"][0]["dinner"]
    assert lunch["meal"]["name"] == "Hovězí guláš s rýží"
    assert lunch["evidence"]["exact_orders"] == 2
    assert "2 past lunch order(s)" in lunch["reason"]
    assert dinner["meal"]["name"] == "Těstoviny zapečené"
    assert dinner["evidence"]["exact_orders"] == 0
    assert dinner["evidence"]["shared_token_orders"] == 1
    assert dinner["score"] == [0, 1, 2]
    assert "Zapečené těstoviny" in dinner["reason"]


def test_no_menu_each_requested_day_has_lunch_dinner_without_timeslots():
    plan = suggest_plan([], [], MONDAY, 3)
    assert plan["start"] == "2026-10-26"
    assert plan["days"] == 3
    assert plan["lunch_section"] == "lunch"
    assert [row["date"] for row in plan["plan"]] == [
        "2026-10-26", "2026-10-27", "2026-10-28",
    ]
    for day in plan["plan"]:
        assert set(day) == {"date", "lunch", "dinner"}
        for key in ("lunch", "dinner"):
            assert day[key]["status"] == "unavailable"
            assert day[key]["meal"] is None
            assert "No" in day[key]["reason"]
            assert day[key]["score"] is None
            assert set(day[key]) == {"status", "canteen", "meal", "reason", "evidence", "score"}
    assert plan["plan"][0]["dinner"]["canteen"] == "Harcov"


def test_sold_out_reported_only_if_nothing_bookable_and_minutky_never_preordered():
    menus = [
        meal(MONDAY, "Favorite", menu_id=None),
        meal(MONDAY, "Available", menu_id=10),
        meal(MONDAY, "Minutka", section="minutka", menu_id=11),
        meal(MONDAY, "Direct sale", section="dinner", menu_id=12, direct_sale_only=True),
        meal(MONDAY, "Dinner unavailable", section="dinner", menu_id=None),
    ]
    history = [purchase(date(2026, 10, 19), "Obědy", "Favorite", "Harcov")]
    day = suggest_plan(history, menus, MONDAY, 1)["plan"][0]
    assert day["lunch"]["meal"]["name"] == "Available"
    assert day["lunch"]["status"] == "available"
    assert day["dinner"]["meal"]["name"] == "Dinner unavailable"
    assert day["dinner"]["status"] == "unavailable"
    assert "not bookable" in day["dinner"]["reason"]
    only_direct = suggest_plan([], [meal(MONDAY, "Minutka", section="minutka"),
                                    meal(MONDAY, "Direct sale", section="dinner", direct_sale_only=True)],
                               MONDAY, 1)["plan"][0]
    assert only_direct["lunch"]["meal"] is None
    assert only_direct["dinner"]["meal"] is None
    assert "direct-sale" in only_direct["dinner"]["reason"]


def test_unavailable_lunch_dish_at_other_canteen_beats_direct_sale_only():
    history = [purchase(date(2026, 10, 19), "Obědy", "Rizoto", "Harcov")]
    menus = [meal(MONDAY, "Counter only", direct_sale_only=True),
             meal(MONDAY, "Rizoto", canteen=2, menu_id=None)]
    lunch = suggest_plan(history, menus, MONDAY, 1)["plan"][0]["lunch"]
    assert lunch["status"] == "unavailable"
    assert lunch["canteen"] == "Husova"
    assert lunch["meal"]["name"] == "Rizoto"
    assert lunch["score"][0] == 1
    assert "unavailable preorder dish" in lunch["reason"]


def test_refunds_and_minutky_do_not_infer_lunch_location_or_dish():
    history = [
        purchase(date(2026, 10, 19), "Obědy", "Refunded", "Husova", 75.0),
        purchase(date(2026, 10, 19), "Minutky", "Counter sale", "Husova"),
    ]
    menus = [meal(MONDAY, "Lunch A", 1), meal(MONDAY, "Counter sale", 2)]
    lunch = suggest_plan(history, menus, MONDAY, 1)["plan"][0]["lunch"]
    assert lunch["canteen"] == "Harcov"
    assert lunch["evidence"]["history_orders"] == 0
    assert lunch["score"] == [0, 0, 0]


def test_existing_order_across_lunch_variants_or_dinner_canteens_prevents_duplicate():
    menus = [meal(MONDAY, "Ordered no-soup", 2, "lunch_no_soup", None, 101),
             meal(MONDAY, "Available with soup", 1, "lunch", 102),
             meal(MONDAY, "Ordered elsewhere", 2, "dinner", None, 103),
             meal(MONDAY, "Available Harcov", 1, "dinner", 104)]
    for section in ("lunch", "lunch_no_soup"):
        day = suggest_plan([], menus, MONDAY, 1, lunch_section=section)["plan"][0]
        assert day["lunch"]["status"] == "ordered"
        assert day["lunch"]["meal"]["order_id"] == 101
        assert day["lunch"]["evidence"]["existing_orders"] == 1
        assert day["dinner"]["status"] == "ordered"
        assert day["dinner"]["meal"]["order_id"] == 103
        assert "do not order" in day["dinner"]["reason"]


def test_soup_section_is_explicit_never_deduced_from_historical_price():
    history = [purchase(date(2026, 10, 19), "Obědy", "Bean dish", "Harcov", -50.0)]
    menus = [meal(MONDAY, "Soup lunch", section="lunch"),
             meal(MONDAY, "No soup lunch", section="lunch_no_soup")]
    with_soup = suggest_plan(history, menus, MONDAY, 1)["plan"][0]["lunch"]
    no_soup = suggest_plan(history, menus, MONDAY, 1, lunch_section="lunch_no_soup")["plan"][0]["lunch"]
    assert with_soup["meal"]["name"] == "Soup lunch"
    assert no_soup["meal"]["name"] == "No soup lunch"


@pytest.mark.parametrize("days", [0, -1, 15, True, 1.5])
def test_invalid_horizon(days):
    with pytest.raises(ValueError, match="days"):
        suggest_plan([], [], MONDAY, days)


def test_full_horizon_and_unknown_section_rejected():
    result = suggest_plan([], [], MONDAY, 14)
    assert len(result["plan"]) == 14
    assert result["plan"][-1]["date"] == "2026-11-08"
    with pytest.raises(ValueError, match="lunch_section"):
        suggest_plan([], [], MONDAY, lunch_section="minutka")
