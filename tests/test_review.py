"""the three things a plugin that moves money has to get right: ask first, cap the fee, pay once."""
import time

import pytest
from solders.keypair import Keypair

import watcher as W
from fakes import FakeClient
from store import Store
from test_plugin import M, Ctx


class Greedy(FakeClient):
    """a relay that quotes whatever it likes."""
    quote, has_account = 80_000, True

    def exists(self, addr):
        return self.has_account

    def relay_post(self, wire, dry=False):
        return {"fee": self.quote} if dry else super().relay_post(wire)


def make(tmp_path, cls=FakeClient):
    store = Store(tmp_path)
    c = cls(str(store.helper().pubkey()), str(Keypair().pubkey()))
    p = M.Plugin(None, store, c)
    p.watcher.tick()
    return p, c


def test_every_chat_payment_asks_first(tmp_path):
    p, c = make(tmp_path)
    to = str(Keypair().pubkey())
    p.command(f"allow {to} 50 sam")
    a = p.guard(tool_name="money_pay", args={"to": "sam", "amount": "20"}, tool_call_id="c1")
    b = p.guard(tool_name="money_pay", args={"to": "sam", "amount": "20"}, tool_call_id="c2")
    assert a["action"] == b["action"] == "approve"  # a saved payee under their auto limit still asks
    assert "$20.00" in a["message"] and to in a["message"]  # the whole address, not a short form
    assert a["rule_key"] != b["rule_key"]  # "always" can't cover the next payment
    assert p.guard(tool_name="money_pay", args={"to": "garbage", "amount": "x"})["action"] == "approve"
    assert p.guard(tool_name="money_status", args={}) is None


def test_register_adds_the_approval_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "Store", lambda: Store(tmp_path))
    ctx = Ctx()
    M.register(ctx)
    M._plugin.stop()
    assert ctx.hooks["pre_tool_call"](tool_name="money_pay", args={"to": "x", "amount": "1"})["action"] == "approve"


def test_fee_over_the_limit_is_refused(tmp_path):
    p, c = make(tmp_path, Greedy)
    to = str(Keypair().pubkey())
    p.command(f"allow {to} 50 sam")
    out = p._tool(p.t_pay, {"to": "sam", "amount": "20"})
    assert "over your $0.05 limit" in out and not c.sent
    assert p.store.spent_today() == 0  # the reservation was given back


def test_new_account_fee_is_allowed_but_bounded(tmp_path):
    p, c = make(tmp_path, Greedy)
    to = str(Keypair().pubkey())
    p.command(f"allow {to} 50 sam")
    c.has_account, c.quote = False, 400_000  # the relay funds the recipient's usdc account
    assert "paid $20.00" in p._tool(p.t_pay, {"to": "sam", "amount": "20"})
    assert p.store.spent_today() == 20_400_000
    c.quote = 700_000
    assert "over your $0.65 limit" in p._tool(p.t_pay, {"to": "sam", "amount": "1"}) and len(c.sent) == 1


def test_fee_counts_toward_the_daily_cap(tmp_path):
    p, c = make(tmp_path)
    to = str(Keypair().pubkey())
    p.command(f"allow {to} 50 sam")
    p.command("cap 20")
    assert "held" in p._tool(p.t_pay, {"to": "sam", "amount": "20"})  # $20 + fee is over a $20 cap
    assert not c.sent and p.store.spent_today() == 0
    p.command("cap 21")
    assert "paid" in p._tool(p.t_pay, {"to": "sam", "amount": "20"})
    assert p.store.spent_today() == 20_010_000


def test_two_processes_pay_a_due_rule_once(tmp_path):
    p1, c = make(tmp_path)
    p2 = M.Plugin(None, Store(tmp_path), c)  # a second hermes process on the same money.db
    to = str(Keypair().pubkey())
    p1._tool(p1.t_rule_add, {"kind": "time", "when": {"every": "daily"}, "action": {"type": "pay", "to": to, "amount": "5"}})
    p1.command("approve r1")
    due = time.time() + 2 * 86400
    p1.watcher.tick(due)
    p2.watcher.tick(due)
    assert len(c.sent) == 1
    # even if both read the rule before either wrote, one claim wins
    nr = p1.store.rule(1)["next_run"]
    assert p1.store.claim_rule(1, "next_run", nr, nr + 1) and not p2.store.claim_rule(1, "next_run", nr, nr + 1)
    assert p1.store.claim_pending(p1.store.hold(to, 1, "", "t"), "waiting", "sending")
    assert not p2.store.claim_pending(1, "waiting", "sending")


@pytest.mark.skipif(W.fcntl is None, reason="no flock here")
def test_tick_skips_while_another_process_holds_the_lock(tmp_path):
    p, c = make(tmp_path)
    to = str(Keypair().pubkey())
    p._tool(p.t_rule_add, {"kind": "time", "when": {"every": "daily"}, "action": {"type": "pay", "to": to, "amount": "5"}})
    p.command("approve r1")
    due = time.time() + 2 * 86400
    with W.only_one(p.store.dir) as mine:
        assert mine
        p.watcher.tick(due)
        assert not c.sent
    p.watcher.tick(due)
    assert len(c.sent) == 1
