// wallet standard only: list whatever solana wallets the browser has, no default brand.
import { getWallets } from '@wallet-standard/app';
import type { Wallet, WalletAccount } from '@wallet-standard/base';
import { StandardConnect, type StandardConnectFeature } from '@wallet-standard/features';
import { SolanaSignTransaction, type SolanaSignTransactionFeature } from '@solana/wallet-standard-features';

const CHAIN = 'solana:mainnet';

export const solanaWallets = (): Wallet[] =>
  getWallets().get().filter(w => SolanaSignTransaction in w.features && StandardConnect in w.features);

export const onWalletsChange = (fn: () => void) => {
  const api = getWallets();
  const a = api.on('register', fn), b = api.on('unregister', fn);
  return () => { a(); b(); };
};

export async function connect(w: Wallet): Promise<WalletAccount> {
  const { accounts } = await (w.features[StandardConnect] as StandardConnectFeature[typeof StandardConnect]).connect();
  const acct = accounts.find(a => a.chains.includes(CHAIN)) ?? accounts[0];
  if (!acct) throw new Error('no solana account in this wallet');
  return acct;
}

const fromB64 = (s: string) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const toB64 = (b: Uint8Array) => { let s = ''; for (const x of b) s += String.fromCharCode(x); return btoa(s); };

// you sign only. the relay adds the fee-payer signature and sends
export async function signWire(w: Wallet, account: WalletAccount, wireB64: string): Promise<string> {
  const f = w.features[SolanaSignTransaction] as SolanaSignTransactionFeature[typeof SolanaSignTransaction];
  const [out] = await f.signTransaction({ account, chain: CHAIN, transaction: fromB64(wireB64) });
  return toB64(out.signedTransaction);
}

