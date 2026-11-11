import pytest
from solders.keypair import Keypair

import rules as R
from solana_tx import MoneyError

A, B = str(Keypair().pubkey()), str(Keypair().pubkey())


def test_every():
    assert R.every_seconds("daily") == 86400
    assert R.every_seconds("12h") == 43200
    assert R.every_seconds("monthly") is None
    for bad in ("30m", "sometimes"):
        with pytest.raises(MoneyError):
            R.every_seconds(bad)


def test_build_rejects():
    with pytest.raises(MoneyError):
        R.build("time", {"every": "daily"}, {"type": "split", "parts": [{"to": A, "pct": 50}]})
    with pytest.raises(MoneyError):
        R.build("below", {"amount": "10"}, {"type": "pay", "to": A, "amount": "1"})
    with pytest.raises(MoneyError):
        R.build("incoming", {}, {"type": "split", "parts": [{"to": A, "pct": 70}, {"to": B, "pct": 40}]})


def test_plan_split_and_sweep():
    spec, act = R.build("incoming", {}, {"type": "split", "parts": [{"to": A, "pct": 10}, {"to": B, "pct": 0.01}]})
    p = R.plan({"spec": spec, "action": act, "kind": "incoming"}, 0, incoming=100_000_000)
    assert p["pay"] == [(A, 10_000_000), (B, 10_000)]
    spec, act = R.build("above", {"amount": "100"}, {"type": "sweep", "to": A, "keep": "100"})
    p = R.plan({"spec": spec, "action": act, "kind": "above"}, 150_000_000)
    assert p["pay"] == [(A, 50_000_000 - 50_000)]
    assert "skip" in R.plan({"spec": spec, "action": act, "kind": "above"}, 100_040_000)


def test_next_run_monthly_moves_forward():
    spec, _ = R.build("time", {"every": "monthly", "day": 1, "at": "09:00"}, {"type": "alert"})
    now = 1_794_400_000
    first = R.next_run(spec, now)
    assert first > now
    assert R.next_run(spec, first, after_run=True) > first + 27 * 86400


def test_describe():
    spec, act = R.build("time", {"every": "weekly", "at": "09:00"}, {"type": "pay", "to": A, "amount": "5"})
    assert R.describe({"spec": spec, "action": act, "kind": "time"}, {A: "sam"}).startswith(
        "every weekly at 09:00: pay $5.00 to sam")
