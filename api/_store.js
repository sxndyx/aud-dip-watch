// Push subscriptions live in one Vercel Blob file. They are credentials — anyone holding a
// subscription can send that phone a notification — so they never go in the repo.
import { put, list } from '@vercel/blob';

const NAME = 'subscriptions.json';

export async function loadSubs() {
  const { blobs } = await list({ prefix: NAME, limit: 1 });
  if (!blobs.length) return [];
  const r = await fetch(blobs[0].url, { cache: 'no-store' });
  if (!r.ok) return [];
  try {
    const j = await r.json();
    return Array.isArray(j) ? j : [];
  } catch {
    return [];
  }
}

export async function saveSubs(subs) {
  await put(NAME, JSON.stringify(subs), {
    access: 'public',            // the URL is unguessable; `addRandomSuffix: false` keeps one canonical file
    addRandomSuffix: false,
    contentType: 'application/json',
    allowOverwrite: true,
    cacheControlMaxAge: 0,
  });
}
