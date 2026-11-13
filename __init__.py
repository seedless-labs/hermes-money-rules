"""money rules for hermes: let your agent pay usdc from your own solana wallet, inside limits you sign.

you connect once by signing an spl approve in your own wallet: a local helper key may spend up to a cap of your
usdc. the token program enforces that cap. rules you approve here (pay rent monthly, split what comes in, sweep
over a balance, chase a payment request) run in the background. gas is paid by a relay, so you need no sol.
the agent can propose payments and rules. only you approve them, with /money.
"""
from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Any, Optional

try:
    from . import rules as R
    from .solana_tx import DEFAULT_BASE, Client, MoneyError, new_reference, pay_link, pubkey, to_micro, usd
    from .store import Store
    from .watcher import Money, Watcher, short, tx_link
except ImportError:  # run as plain modules (tests)
    import rules as R  # type: ignore
    from solana_tx import DEFAULT_BASE, Client, MoneyError, new_reference, pay_link, pubkey, to_micro, usd  # type: ignore
    from store import Store  # type: ignore
    from watcher import Money, Watcher, short, tx_link  # type: ignore

log = logging.getLogger("money-rules")

DEFAULT_AUTO_LIMIT = 0  # agent-started payments always wait for a yes unless you /money allow a payee

PROMPT = """you can move usdc for the user with the money_* tools, from their own solana wallet, within a cap
they signed. rules:
- never claim money moved unless a tool result says "paid" with a solscan link.
- every money_pay asks the user in the hermes approval prompt before it runs. if they say no, nothing was sent.
- money_pay and money_rule_add usually return something the user must approve with a /money command. tell them
  the exact command. you can't approve for them.
- amounts are usdc dollars. addresses are solana wallet addresses. if the user names a saved payee, pass the name.
- if money is not connected, call money_connect and give them the link."""


def exact(micro: int) -> str:
    """micro usdc as a plain decimal, no rounding."""
    whole, frac = divmod(int(micro), 1_000_000)
    return f"{whole}.{frac:06d}".rstrip("0").rstrip(".")


class Plugin:
    """everything register() wires up. kept as an object so tests can drive it without hermes."""

    def __init__(self, ctx: Any = None, store: Optional[Store] = None, client: Optional[Client] = None):
        self.ctx = ctx
        cfg = (lambda k, d=None: ctx.get_config(k, d)) if ctx is not None else (lambda k, d=None: d)
        self.base = str(cfg("base", DEFAULT_BASE) or DEFAULT_BASE).rstrip("/")
        self.store = store or Store()
        self.client = client or Client(self.base, cfg("rpc_url", None) or None)
        self.chat_ctx: Optional[contextvars.Context] = None
        self.money = Money(self.store, self.client, self.notify, self._fee(cfg("max_fee_usd", None), 50_000),
                           self._fee(cfg("max_account_fee_usd", None), 600_000))
        self.watcher = Watcher(self.money, float(cfg("interval", 30) or 30))

    # ---- telling the user ----

    def remember_chat(self) -> None:
        """called inside tool, command and hook calls: keep the chat binding so the watcher can post later."""
        self.chat_ctx = contextvars.copy_context()

    def notify(self, text: str) -> None:
        msg = f"money rules: {text}"
        if self.ctx is not None and self.chat_ctx is not None:
            try:
                h = self.chat_ctx.run(self.ctx.post_message, msg)
                if getattr(h, "ok", True) is not False:
                    return
            except Exception as e:  # fall through to the queue
                log.debug("post_message failed: %s", e)
        self.store.queue_notice(msg)

    def pre_llm_call(self, *args: Any, **kwargs: Any) -> Optional[dict]:
        self.remember_chat()
        notes = self.store.take_notices()
        if not notes:
            return None
        return {"context": "news from money rules since the last message (tell the user):\n- " + "\n- ".join(notes)}

    # ---- helpers ----

    @staticmethod
    def _fee(v: Any, default: int) -> int:
        try:
            return max(0, int(round(float(v) * 1_000_000)))
        except (TypeError, ValueError):
            return default

    def guard(self, tool_name: str = "", args: Any = None, **kw: Any) -> Optional[dict]:
        """pre_tool_call: every chat-started payment goes through the hermes approval prompt, whatever the payee
        or amount. the key is unique per call, so an 'always' answer never covers a later payment."""
        if not str(tool_name).endswith("money_pay"):
            return None
        a = args if isinstance(args, dict) else {}
        to = str(a.get("to") or "").strip()
        try:
            addr = self.resolve(to)
            p = self.store.payee(addr)
            who = f"{p['label']} ({addr})" if p and p.get("label") else addr
        except Exception:
            who = to or "an unknown payee"
        try:
            amt = usd(to_micro(a.get("amount")))
        except Exception:
            amt = f"{a.get('amount')} usdc"
        memo = f", memo: {str(a['memo'])[:80]}" if a.get("memo") else ""
        return {"action": "approve", "message": f"money rules: pay {amt} from your wallet to {who}{memo}",
                "rule_key": f"money_pay:{kw.get('tool_call_id') or uuid.uuid4().hex}"}

    def resolve(self, to: str) -> str:
        to = str(to or "").strip()
        try:
            p = self.store.payee_by_label(to)
        except LookupError as e:
            raise MoneyError(f"more than one saved payee is called '{to}'. use the address instead") from e
        if p:
            return p["address"]
        try:
            return str(pubkey(to))
        except Exception as e:
            raise MoneyError(f"'{to}' isn't a saved payee or a solana address. save one with /money allow") from e

    def names(self) -> dict:
        return {p["address"]: p["label"] for p in self.store.payees() if p.get("label")}

    def connect_link(self, cap: Any = None, revoke: bool = False) -> str:
        helper = str(self.store.helper().pubkey())
        if revoke:
            return f"{self.base}/?do=revoke&helper={helper}"
        return f"{self.base}/?do=connect&helper={helper}&cap={exact(to_micro(cap or 100))}"

    def status(self) -> str:
        owner = self.money.owner()
        if not owner:
            return ("not connected. run /money connect <cap> (for example /money connect 200) and sign once in your "
                    "wallet. hermes can then spend up to that much of your usdc, and only under rules you approve.")
        a = self.client.account(pubkey(owner))
        live = a["delegate"] == str(self.store.helper().pubkey()) and a["allowance"] > 0
        lines = [f"wallet {short(owner)}: {usd(a['balance'])} usdc",
                 f"hermes may spend: {usd(a['allowance']) if live else 'nothing (not connected)'}",
                 f"today: {usd(self.store.spent_today())} of {usd(self.money.daily_cap())} daily limit",
                 f"rules on: {len(self.store.rules(enabled_only=True))} · waiting for you: {len(self.store.pending())}"
                 f" · open requests: {len(self.store.requests('open'))}"]
        return "\n".join(lines)

    def rules_text(self) -> str:
        rs = self.store.rules()
        names = self.names()
        out = [f"r{r['id']} [{'on' if r['enabled'] else 'needs /money approve r' + str(r['id'])}] "
               f"{r['label'] + ': ' if r['label'] else ''}{R.describe(r, names)}" for r in rs]
        out += [f"p{p['id']} waiting: {usd(p['amount'])} to {self.money.name(p['payto'])}"
                f"{' (' + p['reason'] + ')' if p['reason'] else ''}. /money approve p{p['id']}"
                for p in self.store.pending()]
        out += [f"request {usd(q['amount'])} from {q['label'] or 'someone'}: open" for q in self.store.requests("open")]
        return "\n".join(out) or "no rules yet. ask hermes to set one up, then approve it with /money approve r<id>."

    def history_text(self, n: int = 10) -> str:
        h = self.store.history(n)
        return "\n".join(f"- {x['what']}" + (f" {tx_link(x['sig'])}" if x["sig"] else "") for x in h) or "nothing yet."

    # ---- tools (the agent's side: propose, never approve) ----

    def _tool(self, fn, args: dict) -> str:
        self.remember_chat()
        try:
            return fn(**(args or {}))
        except MoneyError as e:
            return f"error: {e}"
        except (KeyError, TypeError, ValueError) as e:
            return f"error: bad arguments ({e})"
        except Exception as e:  # nothing a tool raises reaches the host
            return f"error: {type(e).__name__}: {e}"

    def t_status(self) -> str:
        return self.status() + "\n" + self.rules_text()

    def t_connect(self, cap: Any = 100) -> str:
        return (f"send the user this link. they open it, connect their wallet and sign once to let hermes spend up "
                f"to ${cap} of their usdc. nothing leaves their wallet until a rule they approve pays:\n"
                f"{self.connect_link(cap)}")

    def t_pay(self, to: str, amount: Any, memo: str = "") -> str:
        addr, micro = self.resolve(to), to_micro(amount)
        p = self.store.payee(addr)
        limit = int(p["auto_limit"]) if p else DEFAULT_AUTO_LIMIT
        if micro <= limit:
            r = self.money.pay_now([(addr, micro)], memo, why="asked in chat")
            if "paid" in r:
                return f"paid {usd(micro)} to {self.money.name(addr)}. {tx_link(r['paid'])}"
            return f"held: over today's limit. the user approves with /money approve p{r['held'][0]}"
        pid = self.store.hold(addr, micro, memo, "asked in chat")
        why = "not a saved payee" if not p else f"over the {usd(limit)} auto limit for this payee"
        self.notify(f"{usd(micro)} to {self.money.name(addr)} is waiting ({why}). /money approve p{pid} to send it, "
                    f"/money deny p{pid} to drop it")
        return f"waiting for the user: they send it with /money approve p{pid} ({why})."

    def t_rule_add(self, kind: str, action: dict, when: Optional[dict] = None, label: str = "") -> str:
        when = dict(when or {})
        action = dict(action or {})
        for key in ("to",):
            if action.get(key):
                action[key] = self.resolve(action[key])
        if action.get("parts"):
            action["parts"] = [{**p, "to": self.resolve(p["to"])} for p in action["parts"]]
        spec, act = R.build(kind, when, action)
        rid = self.store.add_rule(kind, spec, act, str(label or "")[:40])
        line = R.describe(self.store.rule(rid), self.names())
        self.notify(f"new rule r{rid}: {line}. /money approve r{rid} to turn it on")
        return f"saved as r{rid}, off until the user runs /money approve r{rid}: {line}"

    def t_rule_list(self) -> str:
        return self.rules_text()

    def t_request(self, amount: Any, label: str = "", memo: str = "", remind_every: str = "") -> str:
        owner = self.money.owner()
        if not owner:
            raise MoneyError("connect a wallet first (money_connect), so there is somewhere to get paid")
        micro = to_micro(amount)
        every = R.every_seconds(remind_every) if remind_every else None
        ref = new_reference()
        self.store.add_request(ref, micro, str(label or "")[:60], str(memo or "")[:120], every)
        link = pay_link(owner, micro, ref, label, memo)
        return (f"payment request for {usd(micro)} made. any solana wallet can pay it from this link (or a qr of it). "
                f"hermes will tell the user when it lands{' and remind them to chase it' if every else ''}:\n{link}")

    def t_history(self, n: int = 10) -> str:
        return self.history_text(max(1, min(50, int(n))))

    # ---- /money (the user's side) ----

    def command(self, raw: str) -> str:
        self.remember_chat()
        parts = (raw or "").split()
        if not parts:
            return self.status() + "\n\n" + HELP
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            return self._command(cmd, rest)
        except MoneyError as e:
            return f"error: {e}"
        except (IndexError, ValueError):
            return HELP
        except Exception as e:  # nothing a command raises reaches the host
            return f"error: {type(e).__name__}: {e}"

    def _id(self, s: str, letter: str) -> int:
        s = s.lower()
        if not s.startswith(letter) or not s[1:].isdigit():
            raise MoneyError(f"use an id like {letter}1")
        return int(s[1:])

    def _command(self, cmd: str, rest: list[str]) -> str:
        if cmd == "status":
            return self.status()
        if cmd == "connect":
            return ("open this, connect your wallet and sign once:\n" + self.connect_link(rest[0] if rest else 100)
                    + "\nhermes sees it within a minute.")
        if cmd == "revoke":
            return ("open this and sign to take hermes' spending rights away. the token program stops it at once:\n"
                    + self.connect_link(revoke=True))
        if cmd in ("approve", "deny"):
            target = rest[0].lower()
            if target.startswith("r"):
                rid = self._id(target, "r")
                r = self.store.rule(rid)
                if not r:
                    return f"no rule r{rid}"
                if cmd == "deny":
                    self.store.delete_rule(rid)
                    return f"r{rid} dropped"
                nr = R.next_run(r["spec"]) if r["kind"] == "time" else None
                self.store.set_rule(rid, enabled=1, next_run=nr)
                self.store.log(f"rule r{rid} on: {R.describe(r, self.names())}")
                return f"r{rid} is on: {R.describe(r, self.names())}"
            pid = self._id(target, "p")
            p = self.store.pending(pid)
            if not p or p["status"] != "waiting":
                return f"nothing waiting as p{pid}"
            if cmd == "deny":
                self.store.set_pending(pid, "denied")
                return f"p{pid} dropped. nothing was sent"
            if not self.store.claim_pending(pid, "waiting", "sending"):  # another hermes process took it
                return f"nothing waiting as p{pid}"
            try:
                r = self.money.pay_now([(p["payto"], p["amount"])], p["memo"] or "", why=f"you approved p{pid}",
                                       force=True)
            except BaseException:
                self.store.claim_pending(pid, "sending", "waiting")
                raise
            self.store.set_pending(pid, "paid", r.get("paid"))
            return f"sent {usd(p['amount'])} to {self.money.name(p['payto'])}. {tx_link(r['paid'])}"
        if cmd == "allow":
            addr = str(pubkey(rest[0]))
            limit = to_micro(rest[1]) if len(rest) > 1 else 0
            label = " ".join(rest[2:])[:40]
            try:
                self.store.allow(addr, limit, label)
            except ValueError as e:
                raise MoneyError(f"another saved payee is already called '{label}'. pick a different name") from e
            return (f"saved {label or short(addr)}. hermes may pay them up to {usd(limit)} at a time without asking"
                    if limit else f"saved {label or short(addr)}. every payment to them still asks you first")
        if cmd == "cap":
            self.store.put("daily_cap", to_micro(rest[0]))
            return f"daily limit is now {usd(to_micro(rest[0]))}. anything over it waits for you"
        if cmd == "rules":
            return self.rules_text()
        if cmd == "pause":
            rid = self._id(rest[0], "r")
            self.store.set_rule(rid, enabled=0)
            return f"r{rid} paused. /money approve r{rid} turns it back on"
        if cmd == "delete":
            rid = self._id(rest[0], "r")
            return f"r{rid} deleted" if self.store.delete_rule(rid) else f"no rule r{rid}"
        if cmd == "history":
            return self.history_text(int(rest[0]) if rest else 10)
        return HELP

    def start(self) -> None:
        self.watcher.start()

    def stop(self) -> None:
        self.watcher.stop()


HELP = """/money status · connect <cap> · revoke · rules · history
/money approve p1|r1 · deny p1|r1 · pause r1 · delete r1
/money allow <address> <auto limit> [name] · cap <daily limit>"""

_ADDR = {"type": "string", "description": "a solana wallet address, or the name of a saved payee"}
_AMT = {"type": "string", "description": "usdc amount in dollars, like \"25\" or \"12.50\""}

TOOLS = [
    ("money_status", "t_status", "💵", "show the connected wallet, usdc balance, how much hermes may spend, rules and "
     "payments waiting for approval.", {"type": "object", "properties": {}}),
    ("money_connect", "t_connect", "🔗", "get a link the user opens to connect their solana wallet and set a spending "
     "cap. use it when money is not connected or the cap ran out.",
     {"type": "object", "properties": {"cap": {**_AMT, "description": "most usdc hermes may ever spend, in dollars"}}}),
    ("money_pay", "t_pay", "💸", "pay usdc from the user's wallet. every call asks the user in the hermes approval "
     "prompt first. after their yes it pays at once only for a saved payee under their auto limit; otherwise it "
     "also waits for the user's /money approve.",
     {"type": "object", "properties": {"to": _ADDR, "amount": _AMT, "memo": {"type": "string"}},
      "required": ["to", "amount"]}),
    ("money_rule_add", "t_rule_add", "⚙️", "propose a money rule. it stays off until the user approves it. kinds: time "
     "(when.every daily|weekly|monthly|3d|12h, when.at \"09:00\", when.day for monthly), incoming (when.min), above / "
     "below (when.amount). actions: pay {to, amount, memo}, split {parts: [{to, pct}]} (incoming only), sweep {to, "
     "keep}, alert {text}.",
     {"type": "object", "properties": {
         "kind": {"type": "string", "enum": list(R.KINDS)},
         "when": {"type": "object", "properties": {"every": {"type": "string"}, "at": {"type": "string"},
                                                   "day": {"type": "integer"}, "min": _AMT, "amount": _AMT}},
         "action": {"type": "object", "properties": {
             "type": {"type": "string", "enum": list(R.ACTIONS)}, "to": _ADDR, "amount": _AMT, "keep": _AMT,
             "memo": {"type": "string"}, "text": {"type": "string"},
             "parts": {"type": "array", "items": {"type": "object", "properties": {"to": _ADDR, "pct": {"type": "number"}},
                                                  "required": ["to", "pct"]}}},
             "required": ["type"]},
         "label": {"type": "string", "description": "a short name like rent"}},
      "required": ["kind", "action"]}),
    ("money_rule_list", "t_rule_list", "📋", "list money rules, payments waiting for approval and open payment requests.",
     {"type": "object", "properties": {}}),
    ("money_request", "t_request", "🧾", "make a solana pay link asking someone to pay the user usdc. hermes watches for "
     "it and can remind the user to chase it.",
     {"type": "object", "properties": {"amount": _AMT, "label": {"type": "string", "description": "who owes it"},
                                       "memo": {"type": "string"},
                                       "remind_every": {"type": "string", "description": "like daily or 3d; empty for no reminders"}},
      "required": ["amount"]}),
    ("money_history", "t_history", "🧾", "recent money activity with solscan links.",
     {"type": "object", "properties": {"n": {"type": "integer"}}}),
]

_plugin: Optional[Plugin] = None


def register(ctx: Any) -> None:
    global _plugin
    p = Plugin(ctx)
    _plugin = p
    for name, meth, emoji, desc, params in TOOLS:
        fn = getattr(p, meth)
        schema = {"name": name, "description": desc, "parameters": params}
        ctx.register_tool(name=name, toolset="money-rules", schema=schema,
                          handler=(lambda args=None, _fn=fn, **kw: p._tool(_fn, args if isinstance(args, dict) else kw)),
                          description=desc, emoji=emoji)
    ctx.register_command("money", p.command, description="your money rules: status, approve, connect, revoke",
                         args_hint="status|connect <cap>|approve p1|rules|revoke")
    ctx.register_hook("pre_llm_call", p.pre_llm_call)
    ctx.register_hook("pre_tool_call", p.guard)
    ctx.register_system_prompt_section("money-rules", PROMPT)
    ctx.on_unload(p.stop)
    p.start()
