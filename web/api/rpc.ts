// read-only solana rpc for the connect page and the plugin's watcher, so the rpc key stays on the server.
// only the read methods money rules uses. anything else is refused.
const RPC = process.env.ALCHEMY_RPC_URL ?? 'https://api.mainnet-beta.solana.com';
const METHODS = new Set([
  'getLatestBlockhash', 'getBalance', 'getAccountInfo', 'getTokenAccountBalance',
  'getSignatureStatuses', 'getSignaturesForAddress', 'getTransaction',
]);
const MAX_BODY = 4 * 1024;
const H = { 'content-type': 'application/json', 'cache-control': 'no-store', 'access-control-allow-origin': '*' };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: H });

export async function OPTIONS() {
  return new Response(null, { status: 204, headers: { ...H, 'access-control-allow-methods': 'POST', 'access-control-allow-headers': 'content-type' } });
}

export async function POST(req: Request): Promise<Response> {
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
