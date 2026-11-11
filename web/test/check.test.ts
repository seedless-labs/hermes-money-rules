import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inspect, USDC, TOKEN, ATA, SYSTEM, COMPUTE, MEMO, type Msg } from '../lib/check.ts';

const PAYER = 'Payer1111111111111111111111111111111111111';
const PAYER_USDC = 'PayerUsdc11111111111111111111111111111111';
// 0 payer · 1 owner/helper · 2 src · 3 dst · 4 payer usdc · 5 usdc · 6 token · 7 ata · 8 system · 9 compute · 10 memo · 11 other
const KEYS = [PAYER, 'Owner11111111111111111111111111111111111111', 'Src1111111111111111111111111111111111111111',
  'Dst1111111111111111111111111111111111111111', PAYER_USDC, USDC, TOKEN, ATA, SYSTEM, COMPUTE, MEMO, 'Jup1111111111111111111111111111111111111111'];

const amt = (tag: number, n: bigint) => { const d = new Uint8Array(10); d[0] = tag; new DataView(d.buffer).setBigUint64(1, n, true); d[9] = 6; return d; };
const xfer = (dst: number, n: bigint, auth = 1) => ({ programAddressIndex: 6, accountIndices: [2, 5, dst, auth], data: amt(12, n) });
const fee = xfer(4, 10_000n);
const msg = (...instructions: Msg['instructions']): Msg => ({ staticAccounts: KEYS, instructions });
const run = (m: Msg) => inspect(m, PAYER, PAYER_USDC);

test('connect: approve + fee', () => {
  assert.deepEqual(run(msg({ programAddressIndex: 6, accountIndices: [2, 5, 11, 1], data: amt(13, 50_000_000n) }, fee)), { fee: 10_000n, kind: 'connect' });
});
test('revoke + fee', () => {
  assert.deepEqual(run(msg({ programAddressIndex: 6, accountIndices: [2, 1], data: new Uint8Array([5]) }, fee)), { fee: 10_000n, kind: 'revoke' });
});
test('pay with ata create funded by relay, compute budget and memo', () => {
  const cu = new Uint8Array(9); cu[0] = 3; new DataView(cu.buffer).setBigUint64(1, 1000n, true);
  const r = run(msg({ programAddressIndex: 9, data: cu },
    { programAddressIndex: 7, accountIndices: [0, 3, 11, 5, 8, 6], data: new Uint8Array([1]) },
    xfer(3, 5_000_000n), fee, { programAddressIndex: 10, accountIndices: [], data: new Uint8Array([104, 105]) }));
  assert.deepEqual(r, { fee: 10_000n, kind: 'pay' });
});
test('refuses: wrong fee payer', () => assert.match(run({ ...msg(fee), staticAccounts: [KEYS[1], ...KEYS.slice(1)] }).why!, /fee payer/));
test('refuses: relay as token authority', () => assert.match(run(msg(xfer(3, 1n, 0), fee)).why!, /spends the relay/));
test('refuses: foreign program', () => assert.match(run(msg({ programAddressIndex: 11, data: new Uint8Array([1]) }, fee)).why!, /program not allowed/));
test('refuses: system transfer from relay', () => assert.match(run(msg({ programAddressIndex: 8, accountIndices: [0, 3], data: new Uint8Array(12) }, fee)).why!, /program not allowed/));
test('refuses: non-usdc mint', () => assert.match(run(msg({ programAddressIndex: 6, accountIndices: [2, 11, 3, 1], data: amt(12, 1n) }, fee)).why!, /usdc only/));
test('refuses: plain Transfer (unchecked)', () => {
  const d = new Uint8Array(9); d[0] = 3;
  assert.match(run(msg({ programAddressIndex: 6, accountIndices: [2, 3, 1], data: d }, fee)).why!, /not allowed/);
});
test('refuses: close account / set authority', () => {
  assert.match(run(msg({ programAddressIndex: 6, accountIndices: [2, 3, 1], data: new Uint8Array([9]) }, fee)).why!, /not allowed/);
  assert.match(run(msg({ programAddressIndex: 6, accountIndices: [2, 1], data: new Uint8Array([6, 2, 0]) }, fee)).why!, /not allowed/);
});
test('refuses: ata for another mint, or relay as owner', () => {
  assert.match(run(msg({ programAddressIndex: 7, accountIndices: [0, 3, 11, 11, 8, 6], data: new Uint8Array([1]) }, xfer(3, 1n), fee)).why!, /only usdc/);
  assert.match(run(msg({ programAddressIndex: 7, accountIndices: [0, 3, 0, 5, 8, 6], data: new Uint8Array([1]) }, xfer(3, 1n), fee)).why!, /only fund/);
});
test('refuses: approve mixed with pay, priority fee too high, lookup tables', () => {
  assert.match(run(msg({ programAddressIndex: 6, accountIndices: [2, 5, 11, 1], data: amt(13, 1n) }, xfer(3, 1n), fee)).why!, /one action/);
  const cu = new Uint8Array(9); cu[0] = 3; new DataView(cu.buffer).setBigUint64(1, 10_000_000n, true);
  assert.match(run(msg({ programAddressIndex: 9, data: cu }, xfer(3, 1n), fee)).why!, /priority/);
  assert.match(run({ ...msg(xfer(3, 1n), fee), addressTableLookups: [{}] }).why!, /lookup/);
});
test('refuses: fee only', () => assert.match(run(msg(fee)).why!, /nothing to do/));
