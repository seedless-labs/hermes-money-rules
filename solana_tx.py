"""solana side of money rules: read the user's usdc account, build helper-signed usdc payments, send them via the relay.

the helper key is a delegate on the user's own usdc account. it can only move usdc, only up to the cap the user
signed in their own wallet, and it never holds sol: the relay pays gas and takes a small usdc fee in the same tx.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

USDC = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
DECIMALS = 6

DEFAULT_BASE = "https://hermes-money-rules.vercel.app"
UA = "money-rules-hermes-plugin/0.1"


class MoneyError(Exception):
    """a failure with a message fit to show the user as is."""


# ---- amounts ----

def to_micro(value: Any) -> int:
    """'12.5' / 12.5 / '$12.50' → 12_500_000. refuses negatives, zero and more than 6 decimals."""
    s = str(value).strip().lstrip("$").replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise MoneyError(f"not an amount: {value}") from e
    if d <= 0 or d != d.quantize(Decimal("0.000001")) or d > Decimal(1_000_000_000):
        raise MoneyError(f"not a usdc amount: {value}")
    return int(d * 1_000_000)


def usd(micro: int) -> str:
    whole, frac = divmod(int(micro), 1_000_000)
    f = f"{frac:06d}".rstrip("0")
    return f"${whole:,}.{f.ljust(2, '0')}"


def pubkey(s: str) -> Pubkey:
    try:
        return Pubkey.from_string(str(s).strip())
    except Exception as e:  # solders raises ValueError on bad base58/length
        raise MoneyError(f"not a solana address: {s}") from e


def usdc_account(owner: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(TOKEN), bytes(USDC)], ATA)[0]


# ---- http ----

class Client:
    """talks to the read-only rpc proxy and the relay. both default to the hosted money rules endpoints."""

    def __init__(self, base: str = DEFAULT_BASE, rpc_url: Optional[str] = None, timeout: float = 12.0):
        self.base = base.rstrip("/")
        self.rpc_url = rpc_url or f"{self.base}/api/rpc"
        self.timeout = timeout
        self._relay: Optional[dict] = None
        self._relay_at = 0.0

    def _post(self, url: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "user-agent": UA})
        return self._open(req)

    def _open(self, req: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": f"http {e.code}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MoneyError("can't reach solana right now, try again in a bit") from e

    def rpc(self, method: str, params: list, retries: int = 3) -> Any:
        for i in range(retries):
            code, j = self._post(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if code == 429 or code >= 500:
                time.sleep(0.6 * (i + 1) ** 2)
                continue
            if j.get("error"):
                raise MoneyError(f"solana rpc: {j['error'].get('message', j['error'])}")
            return j.get("result")
        raise MoneyError("solana rpc is busy, try again in a bit")

    def relay(self, fresh: bool = False) -> dict:
        if fresh or not self._relay or time.time() - self._relay_at > 600:
            req = urllib.request.Request(f"{self.base}/api/relay", headers={"user-agent": UA})
            code, j = self._open(req)
            if code != 200 or "payer" not in j:
                raise MoneyError(j.get("error", "the relay is not answering"))
            self._relay, self._relay_at = j, time.time()
        return self._relay

    def relay_post(self, wire_b64: str, dry: bool = False) -> dict:
        code, j = self._post(f"{self.base}/api/relay", {"transaction": wire_b64, "dry": dry})
        if code != 200:
            err = MoneyError(j.get("error", f"relay said {code}"))
            err.fee = j.get("fee")  # type: ignore[attr-defined]
            raise err
        return j

    # ---- reads ----

    def account(self, owner: Pubkey) -> dict:
        """the user's usdc account: balance, who may spend it and how much."""
        ata = usdc_account(owner)
        r = self.rpc("getAccountInfo", [str(ata), {"encoding": "jsonParsed", "commitment": "confirmed"}])
        info = (((r or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info") if r else None
        if not info:
            return {"ata": str(ata), "exists": False, "balance": 0, "delegate": None, "allowance": 0}
        return {
            "ata": str(ata), "exists": True,
            "balance": int(info.get("tokenAmount", {}).get("amount", 0)),
            "delegate": info.get("delegate"),
            "allowance": int((info.get("delegatedAmount") or {}).get("amount", 0)),
            "state": info.get("state"),
        }

    def exists(self, addr: Pubkey) -> bool:
        r = self.rpc("getAccountInfo", [str(addr), {"encoding": "base64", "commitment": "confirmed"}])
        return bool(r and r.get("value"))

    def find_connect(self, helper: Pubkey) -> Optional[str]:
        """the wallet that approved this helper: read the approve tx that named it."""
        sigs = self.rpc("getSignaturesForAddress", [str(helper), {"limit": 10, "commitment": "confirmed"}]) or []
        for s in sigs:
            if s.get("err"):
                continue
            tx = self.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                               "maxSupportedTransactionVersion": 0}])
            for ix in ((tx or {}).get("transaction", {}).get("message", {}).get("instructions", [])):
                p = ix.get("parsed") if isinstance(ix, dict) else None
                if isinstance(p, dict) and p.get("type") in ("approveChecked", "approve"):
                    info = p.get("info", {})
                    if info.get("delegate") == str(helper) and info.get("owner"):
                        return info["owner"]
        return None

    def reference_paid(self, reference: Pubkey, owner_ata: str, amount: int) -> Optional[str]:
        """solana pay: a tx that carries the reference key and pays at least amount usdc into owner_ata."""
        sigs = self.rpc("getSignaturesForAddress", [str(reference), {"limit": 5, "commitment": "confirmed"}]) or []
        for s in sigs:
            if s.get("err"):
                continue
            tx = self.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                               "maxSupportedTransactionVersion": 0}])
            meta = (tx or {}).get("meta") or {}
            keys = [k.get("pubkey") if isinstance(k, dict) else k
                    for k in (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys", [])]
            if owner_ata not in keys:
                continue
            i = keys.index(owner_ata)
            pre = next((int(b["uiTokenAmount"]["amount"]) for b in meta.get("preTokenBalances", []) if b.get("accountIndex") == i), 0)
            post = next((int(b["uiTokenAmount"]["amount"]) for b in meta.get("postTokenBalances", []) if b.get("accountIndex") == i), 0)
            if post - pre >= amount:
                return s["signature"]
        return None


# ---- building ----

def _amount_data(tag: int, n: int) -> bytes:
    return bytes([tag]) + int(n).to_bytes(8, "little") + bytes([DECIMALS])


def transfer_ix(src: Pubkey, dst: Pubkey, authority: Pubkey, amount: int) -> Instruction:
    return Instruction(TOKEN, _amount_data(12, amount), [
        AccountMeta(src, False, True), AccountMeta(USDC, False, False),
        AccountMeta(dst, False, True), AccountMeta(authority, True, False)])


def create_ata_ix(funder: Pubkey, owner: Pubkey) -> Instruction:
    return Instruction(ATA, bytes([1]), [
        AccountMeta(funder, True, True), AccountMeta(usdc_account(owner), False, True),
        AccountMeta(owner, False, False), AccountMeta(USDC, False, False),
        AccountMeta(SYSTEM, False, False), AccountMeta(TOKEN, False, False)])


def memo_ix(text: str) -> Instruction:
    return Instruction(MEMO, text.encode()[:120], [])


def build_pay(helper: Keypair, owner: Pubkey, payments: list[tuple[Pubkey, int]], fee: int, relay: dict,
              blockhash: str, create_for: list[Pubkey] = (), memo: Optional[str] = None) -> str:
    """one tx: create missing usdc accounts (relay funds them), pay each recipient from the owner's usdc as delegate,
    pay the relay fee, all signed by the helper. returns base64 wire with the relay's signature left empty."""
    if not 1 <= len(payments) <= 4:
        raise MoneyError("one payment run can pay 1 to 4 people")
    payer = Pubkey.from_string(relay["payer"])
    src = usdc_account(owner)
    ixs = [create_ata_ix(payer, o) for o in create_for]
    for to, amt in payments:
        dst = usdc_account(to)
        if dst == src:
            raise MoneyError("that is your own wallet")
        ixs.append(transfer_ix(src, dst, helper.pubkey(), amt))
    ixs.append(transfer_ix(src, Pubkey.from_string(relay["payer_usdc"]), helper.pubkey(), fee))
    if memo:
        ixs.append(memo_ix(memo))
    msg = MessageV0.try_compile(payer, ixs, [], Hash.from_string(blockhash))
    sig = helper.sign_message(to_bytes_versioned(msg))
    keys = list(msg.account_keys)
    n = msg.header.num_required_signatures
    sigs = [sig if keys[i] == helper.pubkey() else Signature.default() for i in range(n)]
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


def send_pay(c: Client, helper: Keypair, owner: Pubkey, payments: list[tuple[Pubkey, int]],
             memo: Optional[str] = None) -> dict:
    """quote, rebuild at the quoted fee, send. returns {signature, fee}."""
    relay = c.relay()
    create = []
    for to, _ in payments:
        if not c.exists(usdc_account(to)) and to not in create:
            create.append(to)
    acct = c.account(owner)
    total = sum(a for _, a in payments)
    fee = int(relay.get("fee_floor", 10_000))
    if acct["delegate"] != str(helper.pubkey()):
        raise MoneyError("hermes isn't connected to this wallet anymore. run /money connect")
    for attempt in range(2):
        bh = c.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        wire = build_pay(helper, owner, payments, fee, relay, bh, create, memo)
        try:
            q = c.relay_post(wire, dry=True)
        except MoneyError as e:
            if getattr(e, "fee", None):
                fee = int(e.fee)  # type: ignore[attr-defined]
                continue
            raise
        if int(q["fee"]) > fee:
            fee = int(q["fee"])
            wire = build_pay(helper, owner, payments, fee, relay, bh, create, memo)
        if total + fee > acct["allowance"]:
            raise MoneyError(f"over your cap: {usd(total + fee)} needed, {usd(acct['allowance'])} left. "
                             "run /money connect to set a new cap")
        if total + fee > acct["balance"]:
            raise MoneyError(f"not enough usdc: {usd(total + fee)} needed, {usd(acct['balance'])} in your wallet")
        try:
            r = c.relay_post(wire)
            return {"signature": r["signature"], "fee": fee}
        except MoneyError as e:
            if attempt == 0 and "expired" in str(e):
                continue
            raise
    raise MoneyError("couldn't send it, try again")


def new_reference() -> str:
    return str(Keypair().pubkey())


def pay_link(owner: str, amount: int, reference: str, label: str = "", message: str = "") -> str:
    """solana pay transfer request: any solana wallet can scan or open it and pay usdc to owner."""
    from urllib.parse import quote
    whole, frac = divmod(amount, 1_000_000)
    amt = f"{whole}.{frac:06d}".rstrip("0").rstrip(".")
    q = f"amount={amt}&spl-token={USDC}&reference={reference}"
    if label:
        q += f"&label={quote(label[:60])}"
    if message:
        q += f"&message={quote(message[:120])}"
    return f"solana:{owner}?{q}"
