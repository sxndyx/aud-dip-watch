// Called by the poller (GitHub Actions or the Mac) when ICICI's rate changes.
// Sends the notification to every registered phone and drops the ones that have gone away.
import webpush from 'web-push';
import { loadSubs, saveSubs } from './_store.js';

export const config = { runtime: 'nodejs' };

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  if (!process.env.PUSH_KEY || req.headers['x-push-key'] !== process.env.PUSH_KEY) {
    return res.status(401).json({ error: 'bad or missing push key' });
  }
  if (!process.env.VAPID_PUBLIC || !process.env.VAPID_PRIVATE) {
    return res.status(500).json({ error: 'VAPID keys are not configured' });
  }
  webpush.setVapidDetails('mailto:aryansandlesh23@gmail.com', process.env.VAPID_PUBLIC, process.env.VAPID_PRIVATE);

  const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
  const payload = JSON.stringify({
    title: String(body.title || 'AUD Dip Watch').slice(0, 120),
    body: String(body.body || '').slice(0, 400),
    url: process.env.APP_URL || '/',
    tag: String(body.tag || 'rate'),
  });

  const subs = await loadSubs();
  const keep = [];
  let sent = 0, gone = 0;
  await Promise.all(subs.map(async s => {
    try {
      await webpush.sendNotification(s, payload, { TTL: 6 * 3600, urgency: 'high' });
      keep.push(s); sent++;
    } catch (e) {
      // 404/410 mean the browser threw the subscription away; anything else may be transient
      if (e.statusCode === 404 || e.statusCode === 410) gone++;
      else keep.push(s);
    }
  }));
  if (gone) await saveSubs(keep);
  return res.json({ ok: true, sent, removed: gone, devices: keep.length });
}
