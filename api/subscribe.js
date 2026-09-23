// A phone registers (or unregisters) itself for push notifications.
import { loadSubs, saveSubs } from './_store.js';

export const config = { runtime: 'nodejs' };

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
  const sub = body.subscription;
  if (!sub?.endpoint || !/^https:\/\//.test(sub.endpoint)) {
    return res.status(400).json({ error: 'a push subscription is required' });
  }
  const subs = await loadSubs();
  const rest = subs.filter(s => s.endpoint !== sub.endpoint);
  if (body.unsubscribe) {
    await saveSubs(rest);
    return res.json({ ok: true, subscribed: false, devices: rest.length });
  }
  rest.push({ endpoint: sub.endpoint, keys: sub.keys, label: String(body.label || '').slice(0, 40), added: Date.now() });
  await saveSubs(rest);
  return res.json({ ok: true, subscribed: true, devices: rest.length });
}
