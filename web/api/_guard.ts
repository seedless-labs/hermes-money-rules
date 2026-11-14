// shared guard for the public functions: who is calling, and how often.
// on vercel the platform sets x-vercel-forwarded-for and x-real-ip from the connection and overwrites anything
// the client sent, so they are safe to key on. a client-supplied x-forwarded-for is never read.
export const clientIp = (req: Request): string =>
  req.headers.get('x-vercel-forwarded-for')?.split(',')[0]?.trim() || req.headers.get('x-real-ip') || 'unknown';

/** per-instance burst limit plus a daily ceiling. a firewall rate rule on /api/* is the shared limit across instances */
export function limiter(perMin: number, perDay: number) {
  const hits = new Map<string, { n: number; t: number }>();
  let day = { d: '', n: 0 };
  return (ip: string): boolean => {
    const now = Date.now();
    const h = hits.get(ip);
    if (!h || now - h.t > 60_000) hits.set(ip, { n: 1, t: now });
    else if (++h.n > perMin) return false;
    if (hits.size > 5_000) hits.clear();
    const d = new Date().toISOString().slice(0, 10);
    if (day.d !== d) day = { d, n: 0 };
    return ++day.n <= perDay;
  };
}

const pos = (v: string | undefined, fallback: number) => (Number(v) > 0 ? Number(v) : fallback);
export const limits = (prefix: string, perMin: number, perDay: number) =>
  limiter(pos(process.env[`${prefix}_PER_IP_PER_MIN`], perMin), pos(process.env[`${prefix}_DAILY`], perDay));
