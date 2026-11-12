"""money rules: when something happens, do one money thing. pure functions, no network, easy to test.

kinds (the "when")
  time      every daily / weekly / monthly / 3d / 12h, optionally at "09:00" local time
  incoming  usdc landed in your wallet (optionally only when at least `min`)
  above     your balance went over `amount`
  below     your balance fell under `amount`

actions (the "do")
  pay    {to, amount, memo}            pay a fixed amount
  split  {parts: [{to, pct}], memo}    pay a share of what just came in (incoming rules only)
  sweep  {keep, to, memo}              pay everything over `keep` to one address
  alert  {text}                        just tell you in chat
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any, Optional

try:
    from .solana_tx import MoneyError, pubkey, to_micro, usd
except ImportError:  # run as plain modules (tests)
    from solana_tx import MoneyError, pubkey, to_micro, usd

KINDS = ("time", "incoming", "above", "below")
ACTIONS = ("pay", "split", "sweep", "alert")
_EVERY = re.compile(r"^(?:every\s+)?(\d{1,3})\s*(d|day|days|h|hr|hrs|hour|hours|w|wk|week|weeks)$")


def every_seconds(every: str) -> Optional[int]:
    """'daily' → 86400, '12h' → 43200, 'monthly' → None (calendar month). raises on nonsense."""
    e = str(every).strip().lower()
    named = {"daily": 86400, "day": 86400, "weekly": 604800, "week": 604800, "hourly": 3600}
    if e in named:
        return named[e]
    if e in ("monthly", "month"):
        return None
    m = _EVERY.match(e)
    if not m:
        raise MoneyError(f"don't know how often '{every}' is. use daily, weekly, monthly, 3d or 12h")
    n, unit = int(m.group(1)), m.group(2)[0]
    s = n * {"d": 86400, "h": 3600, "w": 604800}[unit]
    if s < 3600:
        raise MoneyError("the shortest schedule is every hour")
    return s


def _at(at: Optional[str]) -> Optional[tuple[int, int]]:
    if not at:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(at).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise MoneyError(f"'{at}' isn't a time. use 24h like 09:00")
    return int(m.group(1)), int(m.group(2))


def next_run(spec: dict, now: Optional[float] = None, after_run: bool = False) -> float:
    """the next time a time rule fires. first run is the next matching slot; after a run, one period later."""
    now = time.time() if now is None else now
    secs = every_seconds(spec.get("every", "daily"))
    hm = _at(spec.get("at"))
    t = time.localtime(now)
    if secs is None:  # monthly, on the day of month given (default today's), at hm or now
        day = int(spec.get("day") or t.tm_mday)
        y, mo = t.tm_year, t.tm_mon
        for _ in range(3):
            d = min(day, calendar.monthrange(y, mo)[1])
            h, mi = hm or (t.tm_hour, t.tm_min)
            cand = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
            if cand > now + (60 if not after_run else 3600):
                return cand
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
        raise MoneyError("couldn't work out the next month")
    if hm is None:
        return now + secs if after_run or spec.get("start") != "now" else now
    cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hm[0], hm[1], 0, 0, 0, -1))
    while cand <= now + 30:
        cand += secs if secs >= 86400 else 3600 * max(1, secs // 3600)
    return cand


def build(kind: str, when: dict, action: dict) -> tuple[dict, dict]:
    """check and normalise a rule. amounts become micro-usdc ints, addresses are checked."""
    if kind not in KINDS:
        raise MoneyError(f"rule kind must be one of {', '.join(KINDS)}")
    if action.get("type") not in ACTIONS:
        raise MoneyError(f"action must be one of {', '.join(ACTIONS)}")
    spec: dict[str, Any] = {}
    if kind == "time":
        every_seconds(when.get("every", "daily"))
        _at(when.get("at"))
        spec = {"every": str(when.get("every", "daily")).lower(), "at": when.get("at") or None}
        if when.get("day"):
            spec["day"] = max(1, min(31, int(when["day"])))
    elif kind == "incoming":
        spec = {"min": to_micro(when["min"]) if when.get("min") else 0}
    else:
        if not when.get("amount"):
            raise MoneyError(f"an '{kind}' rule needs an amount")
        spec = {"amount": to_micro(when["amount"])}

    t = action["type"]
    memo = str(action.get("memo") or "")[:80]
    if t == "pay":
        a = {"type": "pay", "to": str(pubkey(action["to"])), "amount": to_micro(action["amount"]), "memo": memo}
    elif t == "split":
        if kind != "incoming":
            raise MoneyError("split shares out money that just came in, so it only works on an incoming rule")
        parts = [{"to": str(pubkey(p["to"])), "pct": float(p["pct"])} for p in action.get("parts", [])]
        if not 1 <= len(parts) <= 4:
            raise MoneyError("split pays 1 to 4 people")
        if any(p["pct"] <= 0 for p in parts) or sum(p["pct"] for p in parts) > 100:
            raise MoneyError("split shares must be above 0 and add up to 100% or less")
        a = {"type": "split", "parts": parts, "memo": memo}
    elif t == "sweep":
        a = {"type": "sweep", "to": str(pubkey(action["to"])), "keep": to_micro(action.get("keep") or "0.000001"),
             "memo": memo}
    else:
        a = {"type": "alert", "text": str(action.get("text") or "")[:200]}
    if kind == "below" and t != "alert":
        raise MoneyError("a low-balance rule can only alert you. there is nothing to pay from")
    return spec, a


def describe(rule: dict, names: Optional[dict] = None) -> str:
    """one plain line a person can approve: 'every monthly on the 1st at 09:00: pay $500.00 to landlord (7xKX…Ab3c)'."""
    names = names or {}
    n = lambda a: f"{names[a]} ({a[:4]}…{a[-4:]})" if a in names else a  # not saved: show all of it
    s, a, k = rule["spec"], rule["action"], rule["kind"]
    if k == "time":
        when = f"every {s['every'].replace('every ', '')}"
        if s.get("day"):
            when += f" on day {s['day']}"
        if s.get("at"):
            when += f" at {s['at']}"
    elif k == "incoming":
        when = "when usdc comes in" + (f" (at least {usd(s['min'])})" if s.get("min") else "")
    elif k == "above":
        when = f"when your balance goes over {usd(s['amount'])}"
    else:
        when = f"when your balance drops under {usd(s['amount'])}"
    if a["type"] == "pay":
        do = f"pay {usd(a['amount'])} to {n(a['to'])}"
    elif a["type"] == "split":
        do = "send " + ", ".join(f"{p['pct']:g}% to {n(p['to'])}" for p in a["parts"]) + " of it"
    elif a["type"] == "sweep":
        do = f"move everything over {usd(a['keep'])} to {n(a['to'])}"
    else:
        do = "tell you" + (f": {a['text']}" if a.get("text") else "")
    return f"{when}: {do}"


def plan(rule: dict, balance: int, incoming: int = 0, reserve: int = 50_000) -> dict:
    """what a fired rule should do right now. {'pay': [(addr, micro)], 'memo'} or {'alert': text} or {'skip': why}.
    reserve keeps a little usdc back for the relay fee on sweeps."""
    a = rule["action"]
    if a["type"] == "alert":
        return {"alert": a.get("text") or describe(rule)}
    if a["type"] == "pay":
        return {"pay": [(a["to"], a["amount"])], "memo": a.get("memo") or rule.get("label") or ""}
    if a["type"] == "split":
        pays = [(p["to"], int(incoming * p["pct"] / 100)) for p in a["parts"]]
        pays = [(t, m) for t, m in pays if m >= 10_000]
        if not pays:
            return {"skip": f"{usd(incoming)} came in, too small to split"}
        return {"pay": pays, "memo": a.get("memo") or rule.get("label") or "split"}
    over = balance - a["keep"] - reserve
    if over < 10_000:
        return {"skip": "nothing over the amount you keep"}
    return {"pay": [(a["to"], over)], "memo": a.get("memo") or rule.get("label") or "sweep"}


def total(p: dict) -> int:
    return sum(m for _, m in p.get("pay", []))
