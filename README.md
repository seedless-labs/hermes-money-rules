# money rules by seedless

a hermes plugin that pays stablecoins from your own solana wallet, under rules you set in chat.

- you sign once: a capped usdc allowance to a helper key that lives with your hermes. usdc stays in your wallet.
- hermes pays by rule: on a schedule, when money comes in, when a balance crosses a line, or when you ask in chat.
- gasless: a relay pays the solana fee and takes a small usdc fee in the same transaction. it can only co-sign three kinds of transaction: connect, revoke, pay.
- revoke any time with `/money revoke`.

## layout

- `web/` connect page + relay (vercel). `web/lib/check.ts` is the rule set the relay enforces, `web/test` covers it.
- plugin: coming in this repo.

## license

MIT, Seedless Labs, Inc.
