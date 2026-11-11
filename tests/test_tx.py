import base64

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import solana_tx as T
from fakes import RELAY


def test_amounts():
    assert T.to_micro("12.5") == 12_500_000
    assert T.to_micro("$1,000") == 1_000_000_000
    assert T.usd(12_500_000) == "$12.50"
    assert T.usd(1_234_567) == "$1.234567"
    for bad in ("0", "-1", "1.0000001", "abc"):
        with pytest.raises(T.MoneyError):
            T.to_micro(bad)


def test_build_pay_shape():
    helper, owner, to = Keypair(), Keypair().pubkey(), Keypair().pubkey()
    wire = T.build_pay(helper, owner, [(to, 5_000_000)], 10_000, RELAY, "11111111111111111111111111111111",
                       memo="rent")
    tx = VersionedTransaction.from_bytes(base64.b64decode(wire))
    m = tx.message
    keys = list(m.account_keys)
    assert str(keys[0]) == RELAY["payer"]  # relay pays gas
    assert m.header.num_required_signatures == 2
    assert tx.signatures[0] == T.Signature.default()  # relay signs later
    assert helper.pubkey() in keys
    progs = [str(keys[ix.program_id_index]) for ix in m.instructions]
    assert progs == [str(T.TOKEN), str(T.TOKEN), str(T.MEMO)]
    pay, fee = m.instructions[0], m.instructions[1]
    assert bytes(pay.data)[0] == 12 and int.from_bytes(bytes(pay.data)[1:9], "little") == 5_000_000
    assert str(keys[fee.accounts[2]]) == RELAY["payer_usdc"]
    assert int.from_bytes(bytes(fee.data)[1:9], "little") == 10_000
    # the source is always the owner's usdc account, authority always the helper
    src = str(T.usdc_account(owner))
    for ix in (pay, fee):
        assert str(keys[ix.accounts[0]]) == src
        assert keys[ix.accounts[3]] == helper.pubkey()


def test_build_pay_refuses_self_and_too_many():
    helper, owner = Keypair(), Keypair().pubkey()
    with pytest.raises(T.MoneyError):
        T.build_pay(helper, owner, [(owner, 1)], 10_000, RELAY, "11111111111111111111111111111111")
    many = [(Keypair().pubkey(), 1)] * 5
    with pytest.raises(T.MoneyError):
        T.build_pay(helper, owner, many, 10_000, RELAY, "11111111111111111111111111111111")


def test_pay_link():
    link = T.pay_link("Own1", 12_500_000, "Ref1", "sam", "dinner")
    assert link == f"solana:Own1?amount=12.5&spl-token={T.USDC}&reference=Ref1&label=sam&message=dinner"
