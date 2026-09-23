// "Send a test" from the page: same delivery path as a real alert, no key needed —
// it can only ever send this one fixed message to phones that already opted in.
import webpush from 'web-push';
import { loadSubs, saveSubs } from './_store.js';

export const config = { runtime: 'nodejs' };

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  if (!process.env.VAPID_PUBLIC || !process.env.VAPID_PRIVATE) {
    return res.status(500).json({ error: 'VAPID keys are not configured' });
  }
  webpush.setVapidDetails('mailto:aryansandlesh23@gmail.com', process.env.VAPID_PUBLIC, process.env.VAPID_PRIVATE);

  let rate = null;
  try {
    const r = await fetch(new URL('/rate.json', process.env.APP_URL || 'https://aud-dip-watch.vercel.app'), { cache: 'no-store' });
    rate = (await r.json())?.latest?.ttSell ?? null;
  } catch {}
  const payload = JSON.stringify({
    title: 'AUD Dip Watch test',
    body: rate ? `Notifications are working. ICICI AUD is ₹${rate.toFixed(2)} right now.` : 'Notifications are working.',
    url: process.env.APP_URL || '/',
    tag: 'test',
  });

  const subs = await loadSubs();
  const keep = [];
  let sent = 0, gone = 0;
  await Promise.all(subs.map(async s => {
    try { await webpush.sendNotification(s, payload, { TTL: 600 }); keep.push(s); sent++; }
    catch (e) { if (e.statusCode === 404 || e.statusCode === 410) gone++; else keep.push(s); }
  }));
  if (gone) await saveSubs(keep);
  return res.json({ ok: true, sent, removed: gone });
}
