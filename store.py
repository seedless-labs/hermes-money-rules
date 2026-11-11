"""local state for money rules: rules, trusted payees, payments waiting for a yes, payment requests, a log.

nothing here can move money on its own. the helper key file is the only secret, and it can only spend usdc
the user allowed it to, up to the cap they signed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Optional

from solders.keypair import Keypair

SCHEMA = """
create table if not exists rules (id integer primary key, kind text not null, spec text not null, action text not null,
  label text, enabled integer not null default 0, next_run real, latched integer not null default 0, created real not null);
create table if not exists payees (address text primary key, label text, auto_limit integer not null default 0);
create table if not exists pending (id integer primary key, payto text not null, amount integer not null, memo text,
  reason text, status text not null default 'waiting', sig text, created real not null);
create table if not exists requests (reference text primary key, amount integer not null, label text, memo text,
  every_s integer, last_remind real, status text not null default 'open', sig text, created real not null);
create table if not exists log (id integer primary key, at real not null, what text not null, sig text);
create table if not exists kv (k text primary key, v text);
"""


def _data_dir() -> Path:
    try:  # inside hermes
        from plugins.plugin_storage import plugin_data_dir  # type: ignore
        return Path(plugin_data_dir("money-rules"))
    except Exception:
        d = Path(os.environ.get("MONEY_RULES_HOME", Path.home() / ".hermes" / "plugin-data" / "money-rules"))
        d.mkdir(parents=True, exist_ok=True)
        return d


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.dir = Path(path) if path else _data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "money.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ---- helper key ----

    def helper(self) -> Keypair:
        p = self.dir / "helper.json"
        with self.lock:
            if p.exists():
                return Keypair.from_bytes(bytes(json.loads(p.read_text())))
            kp = Keypair()
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w") as f:
                json.dump(list(bytes(kp)), f)
            return kp

    # ---- kv ----

    def get(self, k: str, default: Any = None) -> Any:
        r = self.db.execute("select v from kv where k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def put(self, k: str, v: Any) -> None:
        with self.lock:
            self.db.execute("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))

    # ---- rules ----

    def add_rule(self, kind: str, spec: dict, action: dict, label: str = "", next_run: Optional[float] = None) -> int:
        with self.lock:
            c = self.db.execute("insert into rules(kind,spec,action,label,next_run,created) values(?,?,?,?,?,?)",
                                (kind, json.dumps(spec), json.dumps(action), label, next_run, time.time()))
            return int(c.lastrowid)

    def rules(self, enabled_only: bool = False) -> list[dict]:
        q = "select * from rules" + (" where enabled=1" if enabled_only else "") + " order by id"
        return [{**dict(r), "spec": json.loads(r["spec"]), "action": json.loads(r["action"])} for r in self.db.execute(q)]

    def rule(self, rid: int) -> Optional[dict]:
        r = self.db.execute("select * from rules where id=?", (rid,)).fetchone()
        return {**dict(r), "spec": json.loads(r["spec"]), "action": json.loads(r["action"])} if r else None

    def set_rule(self, rid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"update rules set {cols} where id=?", (*fields.values(), rid))

    def delete_rule(self, rid: int) -> bool:
        with self.lock:
            return self.db.execute("delete from rules where id=?", (rid,)).rowcount > 0

    # ---- payees ----

    def allow(self, address: str, limit: int, label: str = "") -> None:
        with self.lock:
            self.db.execute("insert into payees(address,label,auto_limit) values(?,?,?) on conflict(address) do update set "
                            "auto_limit=excluded.auto_limit, label=coalesce(nullif(excluded.label,''), payees.label)",
                            (address, label, limit))

    def payee(self, address: str) -> Optional[dict]:
        r = self.db.execute("select * from payees where address=?", (address,)).fetchone()
        return dict(r) if r else None

    def payee_by_label(self, label: str) -> Optional[dict]:
        r = self.db.execute("select * from payees where lower(label)=lower(?)", (label.strip(),)).fetchone()
        return dict(r) if r else None

    def payees(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from payees order by label")]

    # ---- pending ----

    def hold(self, payto: str, amount: int, memo: str = "", reason: str = "") -> int:
        with self.lock:
            c = self.db.execute("insert into pending(payto,amount,memo,reason,created) values(?,?,?,?,?)",
                                (payto, amount, memo, reason, time.time()))
            return int(c.lastrowid)

    def pending(self, pid: Optional[int] = None) -> Any:
        if pid is not None:
            r = self.db.execute("select * from pending where id=?", (pid,)).fetchone()
            return dict(r) if r else None
        return [dict(r) for r in self.db.execute("select * from pending where status='waiting' order by id")]

    def set_pending(self, pid: int, status: str, sig: Optional[str] = None) -> None:
        with self.lock:
            self.db.execute("update pending set status=?, sig=? where id=?", (status, sig, pid))

    # ---- requests ----

    def add_request(self, reference: str, amount: int, label: str, memo: str, every_s: Optional[int]) -> None:
        with self.lock:
            self.db.execute("insert into requests(reference,amount,label,memo,every_s,last_remind,created) values(?,?,?,?,?,?,?)",
                            (reference, amount, label, memo, every_s, time.time(), time.time()))

    def requests(self, status: str = "open") -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from requests where status=? order by created", (status,))]

    def set_request(self, reference: str, **fields: Any) -> None:
        cols = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"update requests set {cols} where reference=?", (*fields.values(), reference))

    # ---- spend + log ----

    def spent_today(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        d = self.get("spent", {})
        return int(d.get(day, 0))

    def add_spent(self, micro: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.lock:
            d = {k: v for k, v in self.get("spent", {}).items() if k == day}
            d[day] = int(d.get(day, 0)) + int(micro)
            self.put("spent", d)

    def log(self, what: str, sig: Optional[str] = None) -> None:
        with self.lock:
            self.db.execute("insert into log(at,what,sig) values(?,?,?)", (time.time(), what, sig))

    def history(self, n: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from log order by id desc limit ?", (n,))]

    # ---- notices for the chat ----

    def queue_notice(self, text: str) -> None:
        with self.lock:
            q = self.get("notices", [])
            q.append(text)
            self.put("notices", q[-20:])

    def take_notices(self) -> list[str]:
        with self.lock:
            q = self.get("notices", [])
            if q:
                self.put("notices", [])
            return q
