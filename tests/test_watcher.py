from dataclasses import replace
from datetime import datetime, timedelta
import pytest

from menza_hermes.models import Meal, PRAGUE
from menza_hermes.watcher import Watches

NOW = datetime(2027, 3, 15, 10, tzinfo=PRAGUE)


def dish(**changes):
    base = Meal(NOW.date(), 1, 'lunch', '1', 'Testovací jídlo', 80.0, '150g', None, None,
                False, NOW - timedelta(hours=1), NOW + timedelta(hours=4))
    return replace(base, **changes)


class Client:
    def __init__(self, meal, fail=False):
        self.meal = meal
        self.calls = []
        self.fail = fail

    def day(self, canteen, day):
        return [self.meal]

    def order(self, canteen, day, menu_id, max_price=None):
        self.calls.append((canteen, day, menu_id, max_price))
        if self.fail:
            raise TimeoutError('uncertain response')
        self.meal = replace(self.meal, order_id=99)
        return self.meal


def test_watch_buys_once_after_burza_availability_and_survives_restart(tmp_path):
    store = Watches(tmp_path)
    job = store.create(dish(), 85, NOW + timedelta(hours=1), NOW)
    client = Client(dish())
    assert store.tick(client, NOW)[0]['status'] == 'waiting'
    assert not client.calls
    store.close()
    store = Watches(tmp_path)
    client.meal = dish(menu_id=22)
    results = store.tick(client, NOW + timedelta(seconds=20))
    assert results[0]['id'] == job['id'] and results[0]['status'] == 'purchased'
    assert results[0]['result']['order_id'] == 99
    store.tick(client, NOW + timedelta(seconds=40))
    assert len(client.calls) == 1 and client.calls[0][3] == 85
    store.close()


@pytest.mark.parametrize('changed', [
    {'price': 86}, {'price': None}, {'name': 'Jiné jídlo'}, {'section': 'dinner'},
    {'number': '2'}, {'sell_from': NOW + timedelta(hours=1)}, {'direct_sale_only': True},
])
def test_no_substitution_or_overspend(tmp_path, changed):
    store = Watches(tmp_path)
    store.create(dish(), 85, NOW + timedelta(hours=2), NOW)
    client = Client(dish(menu_id=22, **changed))
    store.tick(client, NOW)
    assert not client.calls
    store.close()


def test_expiry_cancellation_and_existing_order_never_purchase(tmp_path):
    store = Watches(tmp_path)
    one = store.create(dish(), 85, NOW + timedelta(seconds=5), NOW)
    client = Client(dish(menu_id=22))
    store.tick(client, NOW + timedelta(seconds=6))
    assert store.list()[0]['status'] == 'expired'
    two = store.create(dish(), 85, NOW + timedelta(hours=1), NOW)
    store.cancel(two['id'], NOW)
    store.tick(client, NOW)
    three = store.create(dish(), 85, NOW + timedelta(hours=1), NOW)
    client.meal = dish(menu_id=22, order_id=7)
    store.tick(client, NOW)
    assert next(w for w in store.list() if w['id'] == three['id'])['status'] == 'already_ordered'
    assert not client.calls
    store.close()


def test_uncertain_post_or_crash_never_replays(tmp_path):
    store = Watches(tmp_path)
    one = store.create(dish(), 85, NOW + timedelta(hours=1), NOW)
    client = Client(dish(menu_id=22), fail=True)
    assert store.tick(client, NOW)[0]['status'] == 'needs_review'
    store.tick(client, NOW)
    assert len(client.calls) == 1
    store.change(one['id'], 'purchasing', NOW)
    store.close()
    store = Watches(tmp_path)
    store.tick(client, NOW)
    assert store.list()[0]['status'] == 'needs_review'
    assert len(client.calls) == 1
    store.close()


def test_invalid_limits_and_expiry_are_rejected(tmp_path):
    store = Watches(tmp_path)
    for price in [0, -1, float('nan'), float('inf'), 1.001]:
        with pytest.raises(ValueError):
            store.create(dish(), price, NOW + timedelta(hours=1), NOW)
    for expiry in [NOW, NOW + timedelta(days=15), NOW.replace(tzinfo=None)]:
        with pytest.raises(ValueError):
            store.create(dish(), 80, expiry, NOW)
    store.close()
