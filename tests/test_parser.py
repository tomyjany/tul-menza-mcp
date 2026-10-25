from datetime import date

import pytest

from menza_hermes.parser import parse_balance, parse_day, parse_history, parse_orders


DAY = date(2026, 10, 26)


def menu(*, price="58,84 Kč", menu_id="7", order_id="", direct=False):
    notice = "<small>Pouze přímý prodej</small>" if direct else ""
    return (f'<section data-buyuntil="Mon, 26 Oct 2026 19:00:00 GMT"><header>Obědy</header>'
            f'<article data-canteen-id="1" data-menu-id="{menu_id}" data-order-id="{order_id}">'
            f'<h1>Vegetable plate</h1>{notice}<td>1</td><td></td><td></td><td></td>'
            f'<td>{price}</td></article></section>')


def orders(oid="13", xid=""):
    return (f'<section><header>26.10. pondělí</header><article data-order-id="{oid}" '
            f'data-exchange-id="{xid}" data-hintx="place\nHarcov" class="obe">'
            '<h1>Vegetable plate</h1><td>1</td></article></section>')


def test_menu_price_and_direct_sale():
    meal, = parse_day(menu(direct=True), DAY)
    assert meal.price == 58.84
    assert meal.menu_id == 7
    assert not meal.orderable


@pytest.mark.parametrize("page", [menu(price="unknown"), menu(menu_id="7", price=""),
                                   '<html>site changed</html>', menu(menu_id="oops")])
def test_menu_critical_data_must_not_be_silently_accepted(page):
    with pytest.raises(ValueError):
        parse_day(page, DAY)


def test_order_row_requires_date_and_valid_id():
    assert parse_orders(orders(), DAY)[0].order_id == 13
    for page in (orders(oid="garbage"), orders().replace("26.10.", "99.10."),
                 orders().replace("26.10. pondělí", "unknown")):
        with pytest.raises(ValueError):
            parse_orders(page, DAY)


def test_balance_requires_actual_balance():
    with pytest.raises(ValueError):
        parse_balance("<dd><span>Dostupné bony </span>10 ks</dd>")


def test_history_rejects_partial_or_invalid_rows():
    good = ('<table><tr><th>Date</th></tr><tr><td>2026-10-26 12:00:00</td>'
            '<td>Obědy</td><td>-58,84 Kč</td><td>Harcov</td></tr></table>')
    assert parse_history(good)[0].amount == -58.84
    for page in ('<html></html>', good.replace('2026-10-26 12:00:00', 'invalid'),
                 good.replace('-58,84 Kč', 'unknown'),
                 good.replace('<td>Harcov</td>', '')):
        with pytest.raises(ValueError):
            parse_history(page)
