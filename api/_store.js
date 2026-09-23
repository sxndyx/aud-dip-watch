// Push subscriptions live in one file in a private Vercel Blob store. They are credentials —
// anyone holding a subscription can send that phone a notification — so they are never public
// and never in the repo.
import { put, get } from '@vercel/blob';

const NAME = 'subscriptions.json';

export async function loadSubs() {
  try {
    // useCache: false — a stale read would resurrect subscriptions we just removed
    const res = await get(NAME, { access: 'private', useCache: false });
    if (!res) return [];
    const text = await new Response(res.stream).text();
    const j = JSON.parse(text);
    return Array.isArray(j) ? j : [];
  } catch (e) {
    if (e?.name === 'BlobNotFoundError') return [];   // nobody has subscribed yet
    throw e;
  }
}

export async function saveSubs(subs) {
  await put(NAME, JSON.stringify(subs), {
    access: 'private',
    addRandomSuffix: false,        // one canonical file, overwritten in place
    allowOverwrite: true,
    contentType: 'application/json',
    cacheControlMaxAge: 0,
  });
}
