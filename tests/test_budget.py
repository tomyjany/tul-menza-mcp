from datetime import datetime, timedelta

import pytest

from menza_hermes.budget import SpendGuard
from menza_hermes.client import MenzaError
from menza_hermes.models import PRAGUE

NOW = datetime(2027, 3, 15, 12, tzinfo=PRAGUE)


def test_limits_purchase_count_and_amount_in_rolling_day(tmp_path):
    guard = SpendGuard(tmp_path, max_purchases=2, max_czk=150)
    guard(80, NOW)
    with pytest.raises(MenzaError, match="limit"):
        guard(80, NOW + timedelta(minutes=1))  # 160 > 150
    guard(70, NOW + timedelta(minutes=2))
    with pytest.raises(MenzaError, match="limit"):
        guard(1, NOW + timedelta(minutes=3))  # third purchase
    guard(80, NOW + timedelta(hours=24, seconds=1))  # first purchase left the window


def test_limit_survives_new_process_and_refused_attempt_is_not_counted(tmp_path):
    SpendGuard(tmp_path, max_purchases=2, max_czk=250)(100, NOW)
    second = SpendGuard(tmp_path, max_purchases=2, max_czk=250)
    with pytest.raises(MenzaError):
        second(200, NOW)
    assert second.usage(NOW)["purchases_24h"] == 1
