// money rules relay: pays the solana fee for money rules' own transactions from a payer used for nothing else.
// the payer key lives in server env (HERMES_PAYER_SECRET) and nowhere else, and holds a small balance on purpose.
// GET  → { payer, payer_usdc, fee_floor }      (what a client needs to build a tx)
// POST { transaction: base64 } → { signature } (the tx must pass lib/check.ts, carry a fee that covers its simulated
//      cost, and already carry every signature but the relay's)
import {
  address, getAddressEncoder, getProgramDerivedAddress, getBase64Encoder, getTransactionDecoder,
  getCompiledTransactionMessageDecoder, createKeyPairFromBytes, getAddressFromPublicKey,
  partiallySignTransaction, getBase64EncodedWireTransaction, getSignatureFromTransaction,
} from '@solana/kit';
import { inspect, USDC, TOKEN, ATA, type Msg } from '../lib/check.js';

const MAX_BODY = 8 * 1024;
const BASE_FEE = 10_000n;       // 0.01 usdc, the floor
const MAX_SPEND = 3_000_000n;   // lamports one tx may cost the payer (covers one new usdc account's rent)
const FLOOR = 3_000_000n;       // stop below 0.003 sol so nothing fails half way
const DAILY_SPEND = Number(process.env.HERMES_DAILY_LAMPORTS) > 0 ? BigInt(process.env.HERMES_DAILY_LAMPORTS!) : 50_000_000n;
const PER_IP_PER_MIN = 20;
const RPC = process.env.ALCHEMY_RPC_URL ?? 'https://api.mainnet-beta.solana.com';
const SOL = 'So11111111111111111111111111111111111111112';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store', 'access-control-allow-origin': '*' } });

const hits = new Map<string, { n: number; t: number }>();
function allow(ip: string): boolean {
  const now = Date.now(); const h = hits.get(ip);
  if (!h || now - h.t > 60_000) hits.set(ip, { n: 1, t: now });
  else if (++h.n > PER_IP_PER_MIN) return false;
  if (hits.size > 5_000) hits.clear();
  return true;
}

let cache: { kp: CryptoKeyPair; addr: string; usdc: string } | undefined;
async function payer() {
  if (cache) return cache;
  const bytes = new Uint8Array(JSON.parse(process.env.HERMES_PAYER_SECRET!));
  if (bytes.length !== 64) throw new Error('payer key must be 64 bytes');
  const kp = await createKeyPairFromBytes(bytes);
  const addr = await getAddressFromPublicKey(kp.publicKey);
  const enc = getAddressEncoder();
  const [usdc] = await getProgramDerivedAddress({ programAddress: address(ATA), seeds: [enc.encode(addr), enc.encode(address(TOKEN)), enc.encode(address(USDC))] });
  cache = { kp, addr, usdc };
  return cache;
}

const call = (method: string, params: unknown[]) => fetch(RPC, {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }), signal: AbortSignal.timeout(8000),
}).then(r => r.json());

let solUsd = { v: 0, t: 0 };
async function solPrice(): Promise<number> {
  if (solUsd.v && Date.now() - solUsd.t < 5 * 60_000) return solUsd.v;
  try {
    const r = await fetch(`https://lite-api.jup.ag/price/v3?ids=${SOL}`, { signal: AbortSignal.timeout(4000) });
    const v = Number((await r.json())?.[SOL]?.usdPrice);
    if (v > 0) { solUsd = { v, t: Date.now() }; return v; }
  } catch { /* high fallback below never undercharges */ }
  return 400;
}

// simulate and read the payer's lamports before and after: network fee plus any rent it funds.
// the usdc fee must cover that with 20% on top, never under the floor.
async function cost(wire: string, payerAddr: string) {
  const [bal, sim] = await Promise.all([
    call('getBalance', [payerAddr, { commitment: 'processed' }]),
    call('simulateTransaction', [wire, { encoding: 'base64', sigVerify: false, replaceRecentBlockhash: true, commitment: 'processed', accounts: { encoding: 'base64', addresses: [payerAddr] } }]),
  ]);
  const v = sim?.result?.value; const after = v?.accounts?.[0]?.lamports;
  if (v?.err || typeof after !== 'number' || typeof bal?.result?.value !== 'number') {
    return { why: 'this would fail on solana: ' + JSON.stringify(v?.err ?? 'simulation failed').slice(0, 120), logs: (v?.logs ?? []).slice(-4) };
  }
  const spent = BigInt(bal.result.value) - BigInt(after);
  const s = spent > 0n ? spent : 0n;
  const cents = BigInt(Math.ceil((await solPrice()) * 100));
  const usd = (s * cents * 12n) / (1000n * 100n * 10n) + 1n; // lamports → micro-usdc, +20%
  return { fee: usd > BASE_FEE ? usd : BASE_FEE, spent: s, bal: BigInt(bal.result.value) };
}

let today = { d: '', n: 0n };

export async function OPTIONS() {
  return new Response(null, { status: 204, headers: { 'access-control-allow-origin': '*', 'access-control-allow-methods': 'GET, POST', 'access-control-allow-headers': 'content-type' } });
}

export async function GET() {
  if (!process.env.HERMES_PAYER_SECRET) return json({ error: 'relay is not configured' }, 503);
  const p = await payer();
  return json({ payer: p.addr, payer_usdc: p.usdc, fee_floor: Number(BASE_FEE), usdc: USDC });
}

export async function POST(req: Request): Promise<Response> {
  if (!process.env.HERMES_PAYER_SECRET) return json({ error: 'relay is not configured' }, 503);
  const ip = req.headers.get('x-real-ip') ?? req.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ?? 'unknown';
  if (!allow(ip)) return json({ error: 'busy, try again in a minute' }, 429);
  const raw = await req.arrayBuffer();
  if (raw.byteLength > MAX_BODY) return json({ error: 'body too large' }, 413);
  let body: any;
  try { body = JSON.parse(new TextDecoder().decode(raw)); } catch { return json({ error: 'bad json' }, 400); }
  const wire = typeof body?.transaction === 'string' ? body.transaction : '';
  const dry = body?.dry === true; // quote only: returns the fee this tx needs, signs nothing

  try {
    const p = await payer();
    let tx;
    try { tx = getTransactionDecoder().decode(getBase64Encoder().encode(wire)); } catch { return json({ error: 'not a transaction' }, 400); }
    const msg = getCompiledTransactionMessageDecoder().decode(tx.messageBytes) as unknown as Msg;
    const c = inspect(msg, p.addr, p.usdc);
    if (c.why) { console.warn('[relay] refused', c.why); return json({ error: c.why }, 403); }
    const n = await cost(wire, p.addr);
    if (n.why) return json({ error: n.why, logs: n.logs }, 400);
    if (n.spent! > MAX_SPEND) return json({ error: 'this costs the relay too much' }, 403);
    if (n.bal! - n.spent! < FLOOR) { console.error('[relay] payer low', String(n.bal)); return json({ error: 'the relay is out of gas right now. try again later' }, 503); }
    if (dry) return json({ fee: Number(n.fee), kind: c.kind });
    if (c.fee! * 10n < n.fee! * 9n) return json({ error: 'relay fee missing or too low', fee: Number(n.fee) }, 402);
    const d = new Date().toISOString().slice(0, 10);
    if (today.d !== d) today = { d, n: 0n };
    if (today.n + n.spent! > DAILY_SPEND) return json({ error: 'the relay hit its daily limit. try again tomorrow' }, 503);
    const missing = Object.entries(tx.signatures).some(([a, sig]) => a !== p.addr && (!sig || sig.every(b => b === 0)));
    if (missing) return json({ error: 'sign it first: every signature but the relay\'s must be there' }, 400);
    const signed = await partiallySignTransaction([p.kp], tx);
    const sent = await call('sendTransaction', [getBase64EncodedWireTransaction(signed), { encoding: 'base64', preflightCommitment: 'processed', maxRetries: 5 }]);
    if (sent?.error) {
      const m = String(sent.error.message ?? '');
      console.warn('[relay] send failed', m.slice(0, 200));
      return json({ error: /blockhash/i.test(m) ? 'expired before it was sent. try again' : 'solana refused it. try again' }, 400);
    }
    today.n += n.spent!;
    return json({ signature: getSignatureFromTransaction(signed), kind: c.kind });
  } catch (e) {
    console.error('[relay]', (e as Error).message);
    return json({ error: 'relay is busy, try again' }, 502);
  }
}
