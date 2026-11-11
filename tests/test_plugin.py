import importlib.util
import sys
from pathlib import Path

import pytest
from solders.keypair import Keypair

from fakes import FakeClient
from store import Store

ROOT = Path(__file__).resolve().parent.parent


def load_plugin():
    spec = importlib.util.spec_from_file_location("money_rules", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["money_rules"] = mod
    spec.loader.exec_module(mod)
    return mod


M = load_plugin()


class Ctx:
    def __init__(self):
        self.tools, self.commands, self.hooks, self.posted = {}, {}, {}, []

    def get_config(self, k, d=None):
        return d

    def register_tool(self, name, handler, **kw):
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_hook(self, name, fn, **kw):
        self.hooks[name] = fn

    def __getattr__(self, n):  # anything else register() may call
        return lambda *a, **k: None


@pytest.fixture
def plug(tmp_path):
    store = Store(tmp_path)
    owner = str(Keypair().pubkey())
    c = FakeClient(str(store.helper().pubkey()), owner)
    p = M.Plugin(None, store, c)
    return p, c


def test_connect_then_pay_needs_approval(plug):
    p, c = plug
    p.watcher.tick()
    assert p.store.get("owner") == c.owner
    to = str(Keypair().pubkey())
    out = p._tool(p.t_pay, {"to": to, "amount": "25"})
    assert "/money approve p1" in out and not c.sent  # nothing moved
    out = p.command("approve p1")
    assert out.startswith("sent $25.00") and len(c.sent) == 1
    assert p.command("approve p1").startswith("nothing waiting")  # no double pay


def test_allowed_payee_pays_now_and_daily_cap_holds(plug):
    p, c = plug
    p.watcher.tick()
    to = str(Keypair().pubkey())
    p.command(f"allow {to} 50 sam")
    assert "paid $20.00 to sam" in p._tool(p.t_pay, {"to": "sam", "amount": "20"})
    p.command("cap 30")
    assert "held" in p._tool(p.t_pay, {"to": "sam", "amount": "20"})
    assert len(c.sent) == 1


def test_rule_is_off_until_approved_and_runs(plug):
    p, c = plug
    p.watcher.tick()
    to = str(Keypair().pubkey())
    out = p._tool(p.t_rule_add, {"kind": "incoming", "action": {"type": "split", "parts": [{"to": to, "pct": 10}]}})
    assert "/money approve r1" in out
    c.balance += 100_000_000
    p.watcher.tick()
    assert not c.sent  # not approved yet
    p.command("approve r1")
    c.balance += 100_000_000
    p.watcher.tick()
    assert len(c.sent) == 1 and c.balance == 700_000_000 - 10_000_000 - 10_000


def test_revoked_wallet_pauses(plug):
    p, c = plug
    p.watcher.tick()
    c.connected = False
    p.watcher.tick()
    notes = p.store.take_notices()
    assert any("can't pay from your wallet" in n for n in notes)


def test_notices_reach_the_agent(plug):
    p, c = plug
    p.watcher.tick()
    r = p.pre_llm_call(session_id="s")
    assert r and "connected to" in r["context"]
    assert p.pre_llm_call(session_id="s") is None


def test_register_wires_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "Store", lambda: Store(tmp_path))
    ctx = Ctx()
    M.register(ctx)
    M._plugin.stop()
    assert set(ctx.tools) == {"money_status", "money_connect", "money_pay", "money_rule_add", "money_rule_list",
                              "money_request", "money_history"}
    assert "error" in ctx.tools["money_pay"]({"to": "nope", "amount": "1"})
