from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from menza_hermes import server
from menza_hermes.models import Order, PRAGUE


def test_refund_does_not_mistake_new_exchange_id_for_success(monkeypatch):
    now = datetime.now(PRAGUE)
    order = Order(now.date(), 'Harcov', 'Test meal', '1', 12, None, 'obe',
                  now + timedelta(days=1), now + timedelta(days=2))

    class Client:
        cancelled = False
        def orders(self):
            return [replace(order, order_id=None, exchange_id=999)] if self.cancelled else [order]
        def balance(self):
            return {'balance': 100.0}
        def cancel(self, oid):
            self.cancelled = True
    client = Client()
    @contextmanager
    def account():
        yield client
    monkeypatch.setattr(server, 'account', account)
    with pytest.raises(ToolError, match='moved this order to burza'):
        server.cancel_meal(12, confirmed=True)


def test_price_and_confirmation_rejected_before_account_access(monkeypatch):
    def forbidden():
        raise AssertionError('must not open account')
    monkeypatch.setattr(server, 'account', forbidden)
    today = datetime.now(PRAGUE).date().isoformat()
    with pytest.raises(ToolError, match='confirmation'):
        server.buy_meal('harcov', today, 1, 80)
    with pytest.raises(ToolError, match='finite positive'):
        server.buy_meal('harcov', today, 1, float('nan'), confirmed=True)
