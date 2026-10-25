from datetime import date

import pytest
import requests

from menza_hermes.client import MenzaClient, MenzaError


DAY = date(2099, 1, 26)
LOGGED = '<a href="/odhlasit/">Log out</a>'


def menu(price="58,84 Kč", *, ordered=False, direct=False):
    return (LOGGED + '<section data-buyuntil="Mon, 26 Jan 2099 19:00:00 GMT">'
            '<header>Obědy</header><article data-canteen-id="1" data-menu-id="7" '
            f'data-order-id="{13 if ordered else ""}"><h1>Vegetable plate</h1>'
            + ('<small>Pouze přímý prodej</small>' if direct else '')
            + f'<td>1</td><td></td><td></td><td></td><td>{price}</td></article></section>')


def orders(*, active=False, exchange=False):
    return (LOGGED + '<section><header>26.01. pondělí</header>'
            + (f'<article data-order-id="{13 if active else ""}" '
               f'data-exchange-id="{91 if exchange else ""}" data-hintx="place\nHarcov" class="obe">'
               '<h1>Vegetable plate</h1><td>1</td></article>' if active or exchange else '')
            + '</section>')


def response(body, status=200, location=None):
    r = requests.Response()
    r.url = 'https://menza.tul.cz/harcov/2099-01-26/'
    r.status_code = status
    r._content = body.encode()
    if location:
        r.headers['Location'] = location
    return r


class FakeHTTP:
    def __init__(self, reads, writes=()):
        self.reads = iter(reads)
        self.writes = iter(writes)
        self.posts = []

    def get(self, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        reply = next(self.reads)
        if isinstance(reply, Exception):
            raise reply
        return response(reply) if isinstance(reply, str) else reply

    def post(self, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        self.posts.append((url, kwargs['data']))
        reply = next(self.writes)
        if isinstance(reply, Exception):
            raise reply
        return response(reply) if isinstance(reply, str) else reply


def client(tmp_path, reads, writes=()):
    c = MenzaClient('synthetic@example.invalid', 'synthetic', state_dir=tmp_path)
    c.session = FakeHTTP(reads, writes)
    return c


def test_history_post_accepts_table_only_fragment_without_logged_in_nav(tmp_path):
    fragment = ('<table><tr><th>When</th><th>Kind</th><th>Amount</th><th>Place</th></tr>'
                '<tr><td>2026-10-26 12:00:00</td><td>Obědy</td>'
                '<td>-58,84 Kč</td><td>Harcov</td></tr></table>')
    c = client(tmp_path, [LOGGED + '<main>History</main>'], [fragment])
    records = c.history(date(2026, 10, 1), date(2026, 10, 31))
    assert len(records) == 1
    assert records[0].amount == -58.84
    assert c.session.posts == [
        ('https://menza.tul.cz/historie/',
         {'HIST': 'true', 'hfrom': '2026-10-01', 'hto': '2026-10-31'})
    ]


def test_order_max_price_prevents_charge(tmp_path):
    c = client(tmp_path, [menu()])
    with pytest.raises(MenzaError, match='maximum'):
        c.order('harcov', DAY, 7, max_price=50)
    assert c.session.posts == []


def test_order_rechecks_changed_price_before_post(tmp_path):
    c = client(tmp_path, [menu(), menu('78,84 Kč')])
    with pytest.raises(MenzaError, match='price changed'):
        c.order('harcov', DAY, 7, max_price=100)
    assert not c.session.posts


def test_direct_sale_rejected_before_post(tmp_path):
    c = client(tmp_path, [menu(direct=True)])
    with pytest.raises(MenzaError, match='not open'):
        c.order('harcov', DAY, 7)
    assert not c.session.posts


def test_closed_meal_is_never_submitted(tmp_path):
    c = client(tmp_path, [menu().replace('2099 19:00:00', '2020 19:00:00')])
    with pytest.raises(MenzaError, match='not open'):
        c.order('harcov', DAY, 7)
    assert not c.session.posts


def test_order_is_idempotent_for_an_existing_order(tmp_path):
    c = client(tmp_path, [menu(ordered=True)])
    assert c.order('harcov', DAY, 7).order_id == 13
    assert not c.session.posts


def test_network_failure_reconciles_success_without_reposting(tmp_path):
    c = client(tmp_path, [menu(), menu(), menu(ordered=True)],
               [requests.ConnectionError('sensitive details')])
    assert c.order('harcov', DAY, 7, max_price=60).order_id == 13
    assert len(c.session.posts) == 1


def test_unknown_post_outcome_is_not_retried(tmp_path):
    c = client(tmp_path, [menu(), menu(), menu()], [requests.Timeout('secret')])
    with pytest.raises(MenzaError, match='uncertain') as exc:
        c.order('harcov', DAY, 7)
    assert 'secret' not in str(exc.value)
    assert len(c.session.posts) == 1


def test_expired_session_prevents_mutation(tmp_path, monkeypatch):
    c = client(tmp_path, [response('', status=302, location='https://shibbo.tul.cz/login')])
    def fail_login():
        raise MenzaError('expired session')
    monkeypatch.setattr(c, 'login', fail_login)
    with pytest.raises(MenzaError, match='expired'):
        c.order('harcov', DAY, 7)
    assert not c.session.posts


def test_cancel_only_owned_order_and_confirm_removal(tmp_path, monkeypatch):
    monkeypatch.setattr('menza_hermes.client._today', lambda: DAY)
    c = client(tmp_path, [orders()])
    with pytest.raises(MenzaError, match='not found'):
        c.cancel(13)
    assert not c.session.posts
    c = client(tmp_path, [orders(active=True), orders()], [orders()])
    c.cancel(13)
    assert len(c.session.posts) == 1


def test_mutation_redirect_never_forwards_body(tmp_path):
    c = client(tmp_path, [menu(), menu(), menu()], [
        response('', status=307, location='https://other.invalid/collect')])
    with pytest.raises(MenzaError, match='uncertain'):
        c.order('harcov', DAY, 7)
    assert len(c.session.posts) == 1


def test_restore_network_failure_reconciles_once(tmp_path, monkeypatch):
    monkeypatch.setattr('menza_hermes.client._today', lambda: DAY)
    c = client(tmp_path, [orders(exchange=True), orders(active=True)],
               [requests.Timeout('private')])
    c.restore(91)
    assert len(c.session.posts) == 1

def test_restore_requires_postcondition(tmp_path, monkeypatch):
    monkeypatch.setattr('menza_hermes.client._today', lambda: DAY)
    c = client(tmp_path, [orders(exchange=True), orders(exchange=True)], [orders(exchange=True)])
    with pytest.raises(MenzaError, match='not confirmed'):
        c.restore(91)
    assert len(c.session.posts) == 1
    c = client(tmp_path, [orders(exchange=True), orders(active=True)], [orders(active=True)])
    c.restore(91)


def test_persistent_cookies_preserve_expiry_and_security(tmp_path):
    c = MenzaClient('synthetic@example.invalid', 'synthetic', state_dir=tmp_path)
    cookie = requests.cookies.create_cookie(name='session', value='opaque-synthetic',
        domain='menza.tul.cz', path='/', secure=True, expires=4102444800,
        rest={'HttpOnly': None, 'SameSite': 'Lax'})
    c.session.cookies.set_cookie(cookie)
    c._save_cookies()
    assert (tmp_path / 'cookies.json').stat().st_mode & 0o777 == 0o600
    restored = MenzaClient('synthetic@example.invalid', 'synthetic', state_dir=tmp_path)
    saved, = list(restored.session.cookies)
    assert saved.secure and saved.expires == 4102444800
    assert saved._rest == cookie._rest
