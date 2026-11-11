"""a fake chain + relay: no network, records what would be sent."""
import base64

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import solana_tx as T

RELAY = {"payer": str(Keypair().pubkey()), "fee_floor": 10_000}
RELAY["payer_usdc"] = str(T.usdc_account(T.pubkey(RELAY["payer"])))


class FakeClient:
    def __init__(self, helper: str, owner: str, balance=500_000_000, allowance=200_000_000):
        self.helper, self.owner = helper, owner
        self.balance, self.allowance = balance, allowance
        self.sent, self.connected, self.paid_refs = [], True, {}

    def relay(self, fresh=False):
        return RELAY

    def exists(self, addr):
        return True

    def account(self, owner):
        return {"ata": str(T.usdc_account(owner)), "exists": True, "balance": self.balance,
                "delegate": self.helper if self.connected else None,
                "allowance": self.allowance if self.connected else 0, "state": "initialized"}

    def find_connect(self, helper):
        return self.owner if self.connected else None

    def reference_paid(self, reference, ata, amount):
        return self.paid_refs.get(str(reference))

    def rpc(self, method, params, retries=3):
        assert method == "getLatestBlockhash"
        return {"value": {"blockhash": "11111111111111111111111111111111"}}

    def relay_post(self, wire, dry=False):
        tx = VersionedTransaction.from_bytes(base64.b64decode(wire))
        if dry:
            return {"fee": 10_000}
        total = 0
        for ix in tx.message.instructions:
            data = bytes(ix.data)
            if data and data[0] == 12:
                total += int.from_bytes(data[1:9], "little")
        self.balance -= total
        self.allowance -= total
        self.sent.append(tx)
        return {"signature": f"sig{len(self.sent)}"}
