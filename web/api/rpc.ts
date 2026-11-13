// read-only solana rpc for the connect page and the plugin's watcher, so the rpc key stays on the server.
// only the read methods money rules uses. anything else is refused.
import { clientIp, limits } from './_guard.js';

const RPC = process.env.ALCHEMY_RPC_URL ?? 'https://api.mainnet-beta.solana.com';
const METHODS = new Set([
  'getLatestBlockhash', 'getBalance', 'getAccountInfo', 'getTokenAccountBalance',
  'getSignatureStatuses', 'getSignaturesForAddress', 'getTransaction',
]);
const MAX_BODY = 4 * 1024;
const H = { 'content-type': 'application/json', 'cache-control': 'no-store' };
// no cross-origin header: the connect page is same origin and the plugin is not a browser
const allow = limits('RPC', 60, 20_000);
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: H });

export async function OPTIONS() {
  return new Response(null, { status: 204, headers: H });
}

export async function POST(req: Request): Promise<Response> {
  if (!allow(clientIp(req))) return json({ jsonrpc: '2.0', id: null, error: { code: -32005, message: 'busy, try again in a minute' } }, 429);
  const raw = await req.arrayBuffer();
  if (raw.byteLength > MAX_BODY) return json({ error: 'body too large' }, 413);
  let rpc: any;
  try { rpc = JSON.parse(new TextDecoder().decode(raw)); } catch { return json({ error: 'bad json' }, 400); }
  if (Array.isArray(rpc) || !METHODS.has(rpc?.method)) return json({ jsonrpc: '2.0', id: rpc?.id ?? null, error: { code: -32601, message: 'method not allowed' } }, 403);
  try {
    const r = await fetch(RPC, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: rpc.id ?? 1, method: rpc.method, params: rpc.params ?? [] }),
      signal: AbortSignal.timeout(8000),
    });
    return new Response(await r.text(), { status: r.status, headers: H });
  } catch {
    return json({ jsonrpc: '2.0', id: rpc.id ?? null, error: { code: -32000, message: 'rpc is busy, try again' } }, 502);
  }
}
