# money rules by seedless

a hermes plugin that pays stablecoins from your own solana wallet, under rules you set in chat.

- you sign once: a capped usdc allowance to a helper key that lives with your hermes. usdc stays in your wallet.
- hermes pays by rule: on a schedule, when money comes in, when a balance crosses a line, or when you ask in chat.
- gasless: a relay pays the solana fee and takes a small usdc fee in the same transaction. it can only co-sign three kinds of transaction: connect, revoke, pay.
- revoke any time with `/money revoke`.

## layout

- `web/` connect page + relay (vercel). `web/lib/check.ts` is the rule set the relay enforces, `web/test` covers it.
- plugin: `plugin.yaml`, `__init__.py` (tools + `/money` command + `pre_llm_call` hook), `rules.py` (rule engine), `watcher.py` (background checks), `solana_tx.py`, `store.py`. tests in `tests/`.

## install

```
hermes plugins install money-rules
/money connect 100      # opens the connect page, you sign a 100 usdc cap in your wallet
```

then talk to hermes: "pay my landlord $500 on the 1st at 9am", "when usdc comes in, send 10% to savings", "tell me if my balance drops under $50". every new rule waits for `/money approve`.

## rules

- when: `time` (daily, weekly, monthly, on a day, at a time) · `incoming` (usdc arrives, optional minimum) · `above` / `below` a balance.
- do: `pay` an amount · `split` incoming money by % (up to 4 people) · `sweep` everything over a kept amount · `alert` you.
- low-balance rules can only alert. every payment goes through one path and is checked against your on-chain cap.

## what keeps it safe

- every payment hermes starts from chat goes through the hermes approval prompt first, whoever the payee and whatever the amount. the prompt shows the amount and the full address.
- a payee you haven't saved with `/money allow` is always shown by its full address.
- the relay fee has a ceiling on your side: `max_fee_usd` (default 0.05) per payment, plus `max_account_fee_usd` (default 0.60) for each recipient who has no usdc account yet, since the relay funds that account's rent. a quote over the ceiling is refused and nothing is sent.
- the fee counts toward your daily cap along with the amount.
- if several hermes processes run at once, only one watcher works at a time (`watcher.lock`) and each rule run is claimed in the database, so a rule pays once.

## what it does that you should know

- network: talks to the relay at hermes-money-rules.vercel.app (connect page, fee co-sign, read-only solana rpc proxy) or your own `rpc_url`.
- background: a watcher thread checks your wallet every 30s (`interval`) while hermes runs.
- stored on disk: a helper keypair at `~/.hermes/plugin-data/money-rules/helper.json` plus your rules and history. it can only spend usdc up to the cap you signed, and `/money revoke` sets that to zero on-chain.
- no telemetry. reads no other credentials.

## license

MIT, Seedless Labs, Inc.
