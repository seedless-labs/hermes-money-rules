// what the relay will co-sign. money rules only ever sends three kinds of transaction:
// - connect: the owner signs an spl ApproveChecked that lets their local helper key spend usdc, up to a cap
// - revoke: the owner signs an spl Revoke
// - pay: the helper key (a delegate) signs a usdc TransferChecked from the owner's account, within that cap
// each one carries the relay's fee as a usdc TransferChecked to the relay's own usdc account, in the same tx.
// anything else is refused. the relay is the fee payer and nothing else: it never signs as a token authority,
// never sends sol, and may only fund rent for a usdc account it creates (which the simulated fee then covers).

export const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
export const TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
export const ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL';
export const SYSTEM = '11111111111111111111111111111111';
export const COMPUTE = 'ComputeBudget111111111111111111111111111111';
export const MEMO = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr';

const MAX_CU_PRICE = 2_000_000n; // micro-lamports per cu
const MAX_CU_LIMIT = 400_000;
const MAX_IX = 12;

export type Msg = {
  staticAccounts: readonly string[];
  instructions: readonly { programAddressIndex: number; accountIndices?: readonly number[]; data?: Uint8Array }[];
  addressTableLookups?: readonly unknown[];
};
export type Check = { why?: string; fee?: bigint; kind?: 'connect' | 'revoke' | 'pay' };

const u64 = (d: Uint8Array, at: number) => new DataView(d.buffer, d.byteOffset + at, 8).getBigUint64(0, true);

export function inspect(msg: Msg, payer: string, payerUsdc: string): Check {
  const keys = msg.staticAccounts.map(String);
  if (msg.addressTableLookups?.length) return { why: 'lookup tables are not accepted' };
  if (keys[0] !== payer) return { why: 'fee payer is not the relay' };
  if (!msg.instructions.length || msg.instructions.length > MAX_IX) return { why: 'wrong number of instructions' };
  let fee = 0n; let approve = 0; let revoke = 0; let pay = 0;
  for (const ix of msg.instructions) {
    const prog = keys[ix.programAddressIndex];
    const a = ix.accountIndices ?? [];
    const d = ix.data ?? new Uint8Array();
    const k = (i: number) => keys[a[i]];
    if (prog === COMPUTE) {
      if (d[0] === 2 && d.length === 5) { if (new DataView(d.buffer, d.byteOffset + 1, 4).getUint32(0, true) > MAX_CU_LIMIT) return { why: 'compute limit too high' }; continue; }
      if (d[0] === 3 && d.length === 9) { if (u64(d, 1) > MAX_CU_PRICE) return { why: 'priority fee too high' }; continue; }
      return { why: 'compute budget instruction not allowed' };
    }
    if (prog === MEMO) { if (a.includes(0)) return { why: 'memo may not involve the relay' }; continue; }
    if (prog === ATA) {
      // create (empty or 0) / create idempotent (1): [funder, ata, wallet, mint, system, token]. the relay may fund it
      if (!(d.length === 0 || (d.length === 1 && (d[0] === 0 || d[0] === 1)))) return { why: 'ata instruction not allowed' };
      if (a.length < 6 || k(3) !== USDC || k(4) !== SYSTEM || k(5) !== TOKEN) return { why: 'only usdc accounts may be created' };
      if (a.slice(1).includes(0)) return { why: 'the relay may only fund the account' };
      continue;
    }
    if (prog !== TOKEN) return { why: `program not allowed: ${prog ?? '?'}` };
    if (a.includes(0)) return { why: 'instruction spends the relay' };
    if (d[0] === 12 && d.length === 10) { // TransferChecked: [src, mint, dst, authority, ...] · amount · decimals
      if (k(1) !== USDC || d[9] !== 6) return { why: 'usdc only' };
      if (k(2) === payerUsdc) fee += u64(d, 1); else pay++;
      continue;
    }
    if (d[0] === 13 && d.length === 10) { // ApproveChecked: [src, mint, delegate, owner]
      if (k(1) !== USDC || d[9] !== 6) return { why: 'usdc only' };
      approve++; continue;
    }
    if (d[0] === 5 && d.length === 1) { revoke++; continue; } // Revoke: [src, owner]
    return { why: 'token instruction not allowed' };
  }
  if (approve + revoke > 1 || (approve + revoke && pay)) return { why: 'one action per transaction' };
  if (!approve && !revoke && !pay) return { why: 'nothing to do' };
  if (pay > 4) return { why: 'too many payments in one transaction' };
  return { fee, kind: approve ? 'connect' : revoke ? 'revoke' : 'pay' };
}
