"""the watcher: a background thread that checks your wallet every 30s and runs the rules you approved.

it only ever spends through send_pay, which is bounded twice: by the on-chain cap you signed (the token program
enforces it, not us) and by a daily cap kept here. anything over the daily cap waits for /money approve.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

try:
    from . import rules as R
    from .solana_tx import Client, MoneyError, pubkey, send_pay, usd, pay_link
    from .store import Store
except ImportError:  # run as plain modules (tests)
    import rules as R  # type: ignore
    from solana_tx import Client, MoneyError, pubkey, send_pay, usd, pay_link  # type: ignore
    from store import Store  # type: ignore

DEFAULT_DAILY_CAP = 100_000_000  # $100 a day unless you set another with /money cap


def short(a: str) -> str:
    return f"{a[:4]}…{a[-4:]}"


def tx_link(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"


class Money:
    """the one place money moves. rules, money_pay and /money approve all come through here."""

    def __init__(self, store: Store, client: Client, notify: Callable[[str], None]):
        self.store, self.client, self.notify = store, client, notify
        self.lock = threading.Lock()

    def owner(self) -> Optional[str]:
        return self.store.get("owner")

    def daily_cap(self) -> int:
        return int(self.store.get("daily_cap", DEFAULT_DAILY_CAP))

    def name(self, addr: str) -> str:
        p = self.store.payee(addr)
        return f"{p['label']} ({short(addr)})" if p and p.get("label") else short(addr)

    def pay_now(self, payments: list[tuple[str, int]], memo: str = "", why: str = "", force: bool = False) -> dict:
        """pay or hold. returns {'paid': sig, 'fee'} or {'held': [ids]}. force skips the daily cap (a human said yes)."""
        owner = self.owner()
        if not owner:
            raise MoneyError("no wallet connected yet. run /money connect <cap>")
        total = sum(m for _, m in payments)
        with self.lock:
            if not force and self.store.spent_today() + total > self.daily_cap():
                ids = [self.store.hold(to, amt, memo, why or "over today's limit") for to, amt in payments]
                self.notify(f"holding {usd(total)} ({why or 'over your daily limit'}). "
                            + " ".join(f"/money approve p{i}" for i in ids) + " to send it")
                return {"held": ids}
            r = send_pay(self.client, self.store.helper(), pubkey(owner), [(pubkey(t), m) for t, m in payments],
                         memo or None)
            self.store.add_spent(total)
            what = ", ".join(f"{usd(m)} to {self.name(t)}" for t, m in payments)
            self.store.log(f"paid {what}" + (f" ({why})" if why else ""), r["signature"])
            self.store.put("paid_since_tick", int(self.store.get("paid_since_tick", 0)) + total + int(r["fee"]))
            self.notify(f"paid {what}. fee {usd(r['fee'])}. {tx_link(r['signature'])}")
            return {"paid": r["signature"], "fee": r["fee"]}


class Watcher:
    def __init__(self, money: Money, interval: float = 30):
        self.m, self.store, self.client = money, money.store, money.client
        self.interval = interval
        self.stop_ev = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.fails = 0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_ev.clear()
        self.thread = threading.Thread(target=self._loop, name="money-rules-watcher", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_ev.set()

    def _loop(self) -> None:
        while not self.stop_ev.is_set():
            try:
                self.tick()
                self.fails = 0
            except Exception as e:  # keep watching; tell the user once if it keeps failing
                self.fails += 1
                if self.fails == 5:
                    self.m.notify(f"money rules can't reach solana right now ({e}). still trying.")
            self.stop_ev.wait(min(300, self.interval * (2 ** min(self.fails, 4))))

    # ---- one pass ----

    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        helper = str(self.store.helper().pubkey())
        owner = self.m.owner()
        if not owner:
            found = self.client.find_connect(pubkey(helper))
            if not found:
                return
            self.store.put("owner", found)
            owner = found
            acct = self.client.account(pubkey(owner))
            self.store.put("last_balance", acct["balance"])
            self.store.put("connected", True)
            self.m.notify(f"connected to {short(owner)}. hermes can spend up to {usd(acct['allowance'])} of your usdc "
                          "under rules you approve. /money rules to see them.")
            return
        acct = self.client.account(pubkey(owner))
        live = acct["delegate"] == helper and acct["allowance"] > 0
        if not live:
            if self.store.get("connected", False):
                self.store.put("connected", False)
                self.m.notify("hermes can't pay from your wallet anymore (revoked or cap used up). "
                              "rules are paused until you run /money connect <cap>.")
        else:
            self.store.put("connected", True)

        last = self.store.get("last_balance")
        paid = int(self.store.get("paid_since_tick", 0))
        bal = acct["balance"]
        incoming = max(0, bal - (last - paid)) if last is not None else 0
        self.store.put("paid_since_tick", 0)
        self.store.put("last_balance", bal)

        self._requests(acct, now)
        if not live:
            return
        for rule in self.store.rules(enabled_only=True):
            try:
                self._rule(rule, bal, incoming, now)
            except MoneyError as e:
                self.m.notify(f"rule r{rule['id']} didn't run: {e}")
                self.store.log(f"rule r{rule['id']} failed: {e}")

    def _rule(self, rule: dict, bal: int, incoming: int, now: float) -> None:
        k, s = rule["kind"], rule["spec"]
        fire = False
        if k == "time":
            nr = rule.get("next_run") or R.next_run(s, now)
            if rule.get("next_run") is None:
                self.store.set_rule(rule["id"], next_run=nr)
            if now >= nr:
                fire = True
                self.store.set_rule(rule["id"], next_run=R.next_run(s, now, after_run=True))
        elif k == "incoming":
            fire = incoming > 0 and incoming >= s.get("min", 0)
        else:
            over = bal > s["amount"] if k == "above" else bal < s["amount"]
            if over and not rule["latched"]:
                fire = True
                self.store.set_rule(rule["id"], latched=1)
            elif not over and rule["latched"]:
                self.store.set_rule(rule["id"], latched=0)
        if not fire:
            return
        p = R.plan(rule, bal, incoming)
        if "alert" in p:
            self.m.notify(f"r{rule['id']}: {p['alert']}")
            self.store.log(f"rule r{rule['id']} alert")
        elif "skip" in p:
            self.store.log(f"rule r{rule['id']} skipped: {p['skip']}")
        else:
            self.m.pay_now(p["pay"], p.get("memo", ""), why=f"rule r{rule['id']}")

    def _requests(self, acct: dict, now: float) -> None:
        owner = self.m.owner()
        for q in self.store.requests("open"):
            sig = self.client.reference_paid(pubkey(q["reference"]), acct["ata"], q["amount"])
            if sig:
                self.store.set_request(q["reference"], status="paid", sig=sig)
                self.store.log(f"request paid: {usd(q['amount'])} {q['label'] or ''}".strip(), sig)
                self.m.notify(f"{q['label'] or 'your request'} paid you {usd(q['amount'])}. {tx_link(sig)}")
            elif q["every_s"] and now - (q["last_remind"] or 0) >= q["every_s"]:
                self.store.set_request(q["reference"], last_remind=now)
                link = pay_link(owner, q["amount"], q["reference"], q["label"] or "", q["memo"] or "")
                self.m.notify(f"still waiting on {usd(q['amount'])} from {q['label'] or 'them'}. "
                              f"send them this again: {link}")
