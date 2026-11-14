// the connect page. hermes sends you here with ?do=connect&helper=<key>&cap=<usdc> (or ?do=revoke).
// you sign one transaction in your own wallet: allow the helper key to spend up to the cap of your usdc, or take that back.
// the relay pays the solana gas and takes a small usdc fee in the same transaction. nothing else can move.
import {
  address, AccountRole, pipe, createTransactionMessage, setTransactionMessageFeePayer,
  setTransactionMessageLifetimeUsingBlockhash, appendTransactionMessageInstructions, compileTransaction,
  getBase64EncodedWireTransaction, getAddressEncoder, getProgramDerivedAddress, type Instruction,
} from '@solana/kit';
import type { Wallet, WalletAccount } from '@wallet-standard/base';
import { solanaWallets, onWalletsChange, connect, signWire } from './wallet';
import { USDC, TOKEN, ATA } from '../lib/check.ts';

const $ = (id: string) => document.getElementById(id)!;
const q = new URLSearchParams(location.search);
const action = q.get('do') === 'revoke' ? 'revoke' : 'connect';
const helper = q.get('helper') ?? '';
const capText = q.get('cap') ?? '';
const B58 = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

const toMicro = (s: string) => {
  const m = /^(\d{1,9})(?:\.(\d{1,6}))?$/.exec(s.trim());
  return m ? BigInt(m[1]) * 1_000_000n + BigInt((m[2] ?? '').padEnd(6, '0')) : 0n;
};
const usd = (n: bigint) => (Number(n) / 1e6).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const short = (a: string) => `${a.slice(0, 4)}…${a.slice(-4)}`;

async function rpc<T = any>(method: string, params: unknown[]): Promise<T> {
  const r = await fetch('/api/rpc', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const j = await r.json();
  if (j.error) throw new Error(j.error.message ?? 'rpc error');
  return j.result;
}

async function usdcAccount(owner: string) {
  const enc = getAddressEncoder();
  const [ata] = await getProgramDerivedAddress({ programAddress: address(ATA), seeds: [enc.encode(address(owner)), enc.encode(address(TOKEN)), enc.encode(address(USDC))] });
  const r = await rpc('getAccountInfo', [ata, { encoding: 'jsonParsed', commitment: 'confirmed' }]);
  const info = r?.value?.data?.parsed?.info;
  return {
    ata: ata as string, exists: !!info,
    balance: BigInt(info?.tokenAmount?.amount ?? 0),
    delegate: info?.delegate as string | undefined,
    allowance: BigInt(info?.delegatedAmount?.amount ?? 0),
  };
}

const amountData = (tag: number, n: bigint) => {
  const d = new Uint8Array(10); d[0] = tag; new DataView(d.buffer).setBigUint64(1, n, true); d[9] = 6; return d;
};
const W = AccountRole.WRITABLE, R = AccountRole.READONLY, RS = AccountRole.READONLY_SIGNER;
const ix = (accounts: [string, AccountRole][], data: Uint8Array): Instruction =>
  ({ programAddress: address(TOKEN), accounts: accounts.map(([a, role]) => ({ address: address(a), role })), data });

type Relay = { payer: string; payer_usdc: string; fee_floor: number };

async function build(relay: Relay, owner: string, src: string, cap: bigint, fee: bigint) {
  const main = action === 'connect'
    ? ix([[src, W], [USDC, R], [helper, R], [owner, RS]], amountData(13, cap))
    : ix([[src, W], [owner, RS]], new Uint8Array([5]));
  const feeIx = ix([[src, W], [USDC, R], [relay.payer_usdc, W], [owner, RS]], amountData(12, fee));
  const { value: bh } = await rpc('getLatestBlockhash', [{ commitment: 'confirmed' }]);
  return getBase64EncodedWireTransaction(compileTransaction(pipe(
    createTransactionMessage({ version: 0 }),
    m => setTransactionMessageFeePayer(address(relay.payer), m),
    m => setTransactionMessageLifetimeUsingBlockhash({ blockhash: bh.blockhash, lastValidBlockHeight: BigInt(bh.lastValidBlockHeight) }, m),
    m => appendTransactionMessageInstructions([main, feeIx], m),
  )));
}

async function relayPost(body: unknown) {
  const r = await fetch('/api/relay', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.error ?? 'relay error'), { fee: j.fee });
  return j;
}

// ---- ui ----
let wallet: Wallet | undefined, account: WalletAccount | undefined;
const say = (t: string, bad = false) => { const s = $('status'); s.textContent = t; s.className = bad ? 'status bad' : 'status'; };

function renderWallets() {
  const list = $('wallets'); list.innerHTML = '';
  const ws = solanaWallets();
  if (!ws.length) { list.innerHTML = '<p class="muted">no solana wallet found in this browser. open this link inside your wallet app\'s browser, or install a wallet.</p>'; return; }
  for (const w of ws) {
    const b = document.createElement('button'); b.className = 'wallet';
    b.innerHTML = `<img alt="" src="${w.icon}"><span></span>`; b.querySelector('span')!.textContent = w.name;
    b.onclick = () => pick(w);
    list.appendChild(b);
  }
}

async function pick(w: Wallet) {
  try {
    say('connecting…');
    account = await connect(w); wallet = w;
    $('pick').hidden = true; $('review').hidden = false;
    $('owner').textContent = short(account.address);
    const a = await usdcAccount(account.address);
    if (!a.exists || (action === 'connect' && a.balance === 0n)) { say('this wallet has no usdc on solana yet. add some, then reload this page.', true); return; }
    $('balance').textContent = `$${usd(a.balance)}`;
    $('current').textContent = a.delegate ? (a.delegate === helper ? `your helper, $${usd(a.allowance)} left` : `another key (${short(a.delegate)}), $${usd(a.allowance)}`) : 'none';
    if (action === 'revoke' && !a.delegate) { say('nothing to revoke. no key can spend this usdc.'); return; }
    // connect needs the tick: the helper key and cap come from the link, so the person confirms both on the page
    const ok = $('ok') as HTMLInputElement;
    const ready = () => { ($('go') as HTMLButtonElement).disabled = action === 'connect' && !ok.checked; };
    ok.onchange = ready; ready();
    ($('go') as HTMLButtonElement).onclick = () => send(a.ata);
    say('');
  } catch (e) { say((e as Error).message, true); }
}

async function send(src: string) {
  const go = $('go') as HTMLButtonElement; go.disabled = true;
  try {
    const relay: Relay = await (await fetch('/api/relay')).json();
    const cap = toMicro(capText);
    say('checking the cost…');
    let fee = BigInt(relay.fee_floor);
    const quote = await relayPost({ transaction: await build(relay, account!.address, src, cap, fee), dry: true });
    if (BigInt(quote.fee) > fee) fee = BigInt(quote.fee);
    $('fee').textContent = `$${usd(fee)} usdc`;
    say('sign in your wallet…');
    const signed = await signWire(wallet!, account!, await build(relay, account!.address, src, cap, fee));
    say('sending…');
    const { signature } = await relayPost({ transaction: signed });
    $('review').hidden = true; $('done').hidden = false;
    ($('tx') as HTMLAnchorElement).href = `https://solscan.io/tx/${signature}`;
    say('');
  } catch (e) { say((e as Error).message.replace(/^User rejected.*/i, 'you cancelled in your wallet.'), true); go.disabled = false; }
}

function boot() {
  const theme = (() => { try { return localStorage.getItem('theme'); } catch { return null; } })();
  if (theme) document.documentElement.dataset.theme = theme;
  $('theme').onclick = () => {
    const t = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = t; try { localStorage.setItem('theme', t); } catch { /* fine */ }
  };
  if (action === 'connect' && (!B58.test(helper) || toMicro(capText) <= 0n)) {
    $('title').textContent = 'this link is incomplete';
    $('lede').textContent = 'ask hermes for a new connect link: /money connect';
    $('pick').hidden = true; return;
  }
  if (action === 'connect') {
    $('title').textContent = `let hermes pay up to $${usd(toMicro(capText))}`;
    $('lede').textContent = 'you sign once. hermes gets a helper key that can spend your usdc up to this cap, only under rules you set in chat. your usdc stays in your wallet until a rule pays.';
    $('helper').textContent = helper; // the whole key, so it can be checked against what hermes printed
    $('helper').style.wordBreak = 'break-all';
    $('ok-row').hidden = false;
    $('ok-text').textContent = `i asked hermes for this link. this helper key may spend up to $${usd(toMicro(capText))} of my usdc.`;
    $('cap').textContent = `$${usd(toMicro(capText))}`;
    $('go').textContent = 'sign and connect';
  } else {
    $('title').textContent = 'stop hermes from paying';
    $('lede').textContent = 'you sign once. the helper key loses its allowance right away, and no rule can move your usdc again.';
    $('helper-row').hidden = true; $('cap-row').hidden = true;
    $('go').textContent = 'sign and revoke';
  }
  renderWallets(); onWalletsChange(renderWallets);
}
boot();
