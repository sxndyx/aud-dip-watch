# AUD Dip Watch

Watches **ICICI Bank's AUD rate** so you can send rupees when the Australian dollar is cheap.
Live rate, best-of-day / best-of-hour, chart, converter, and push alerts to any number of phones.
No accounts, no build step, standard-library Python.

The rate tracked is ICICI's published forex card rate, AUD **TT selling** — the rate an outward
INR→AUD transfer (Money2World) is priced at. Your quote at transaction time may differ slightly.

## Run it
Double-click `serve.command` (or `python3 server.py`), then open http://localhost:8765 on the Mac
or `http://<your-mac-ip>:8765` on a phone on the same Wi-Fi. A launchd agent already runs it at
login (see below), so normally there is nothing to start.

`server.py` does the work: polls ICICI every 5 minutes, keeps every rate change in
`data/icici.json` (90 days), holds the alerts in `data/alerts.json`, and sends pushes. The page is
just a window onto it — `GET /api/state` — so nothing depends on a browser being open.

## Pushes (ntfy)
Three kinds, all to the same topic:
- **Range alerts** — fire when the rate enters a range you set, re-arm when it leaves (10-min cooldown)
- **Every rate change** — each time ICICI re-prices, with the move and the day's best
- **Hourly update** — on the hour during waking hours (default 07:00–22:00, set in `data/config.json`)

The last two are switched on and off in the page's Alerts section.

**To receive them on a phone:** install the free **ntfy** app (App Store / Play Store), tap **+**,
subscribe to the topic below. Any number of phones can subscribe.

    (the topic is in data/config.json on the Mac — never commit it)

The topic name is the only secret — anyone who knows it can read (and post) messages, so share it
privately. Change it in `data/config.json` (then re-subscribe) if it ever leaks.

## Editing alerts (admin key)
Reading is open; changing alerts or settings needs a key sent as `X-Admin-Key`. The page asks for it
once per device and remembers it. The server prints it at startup and stores it here:

    (the key is in data/config.json on the Mac — never commit it)

This is what makes the page safe to expose publicly — visitors see the rate, only you change things.

## Install it on a phone (home-screen app)
Open the page in the phone's browser, then:
- **iPhone/iPad (Safari):** Share → *Add to Home Screen*
- **Android (Chrome):** ⋮ → *Add to Home screen* / *Install app*

It then launches full-screen with its own icon, like an app. There is nothing to download from a
store — this is a PWA, and `manifest.webmanifest` + the PNG icons here are what make it installable.
Note the install must come from a URL the phone can reach; for a plain `http://192.168…` address it
still works, but iOS keeps such an app pinned to that address, so a public https URL (below) is
better if you want it to work away from home.

## Where it runs

Two ways, sharing the same core (`icici.py`):

| | poller | page | Mac must be on? |
|---|---|---|---|
| **Cloud** (GitHub Actions + Vercel) | `poll.py`, every ~10 min on GitHub's runners | static, deployed to Vercel | no |
| **Local** (this Mac) | `server.py`, every 5 min | served at `localhost:8765`, full alert editing | yes |

Both push to the same ntfy topic, so phones get alerts either way. The page detects which it is:
with `server.py` it uses the live API and you can edit alerts; deployed statically it reads
`rate.json` / `alerts.json` and is read-only.

## Cloud setup (one time)

1. **Create a GitHub repo** — public, so Actions minutes are free. Then, in this folder:
   ```bash
   git remote add origin https://github.com/<you>/aud-dip-watch.git
   git push -u origin main
   ```
2. **Add the ntfy topic as a secret** (never commit it): repo → Settings → Secrets and variables →
   Actions → New repository secret → name `NTFY_TOPIC`, value = the topic from `data/config.json`.
   Optionally add repository *variables* `APP_URL` (the deployed URL, so pushes open the app) and
   `NTFY_SERVER` (if not using ntfy.sh).
3. **Check it runs**: Actions tab → "Poll ICICI rate" → Run workflow. It should print the rate and,
   when the rate has moved, commit `rate.json`.
4. **Deploy the page**: vercel.com → Add New → Project → import the repo → Deploy. No framework, no
   build command, no environment variables. Each commit from the workflow redeploys it.

Editing alerts in the cloud setup means editing `alerts.json` in GitHub (or running `server.py`
locally and letting the next push sync it).

### Trade-offs of the cloud setup
- **Run one poller, not two.** The Mac and GitHub keep separate state, so if both run you get two
  pushes per rate change. Once the cloud is live either stop the `aud-dip-watch` launchd agent, or
  set `"changes": false` in `data/config.json` to keep the local UI without its pushes.
- **The deployed page is read-only** — alerts are edited in `alerts.json` on GitHub, or locally.
- **Timing is best-effort.** GitHub can run a scheduled job 5–15 minutes late. ICICI re-prices only a
  few times a day and each rate stands for hours, so this doesn't matter in practice. (The schedule
  deliberately avoids the top of the hour, which GitHub delays most.)
- **60-day inactivity.** GitHub disables scheduled workflows in public repos after 60 days without
  repository activity, and the workflow's own commits may not count. If pushes stop, open the
  Actions tab and click "Enable workflow".

## Public URL from the Mac (Cloudflare Tunnel)
A second launchd agent, `com.aryansandlesh.aud-dip-tunnel`, runs
`cloudflared tunnel --url http://localhost:8765` and gives the app a public https address that works
from any network. **The URL is random and changes whenever the tunnel restarts** (reboot, crash,
`kickstart`). To read the current one: double-click `geturl.command`, or

```bash
grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' data/tunnel.log | tail -1
```

After it changes, update `"appUrl"` in `data/config.json` (so pushes open the app) and restart the
server agent. For a URL that never changes you need a **named tunnel** on your own domain
(`cloudflared tunnel create` + a CNAME) — about A$15/yr for the domain.

Visitors can only read; changing alerts needs the admin key. The API has no rate limiting, so for a
long-lived public deployment put **Cloudflare Access** in front of it. Alternatives to the tunnel:
**Tailscale** (private network, nothing public) or running `server.py` on an always-on host.

```bash
launchctl print gui/$(id -u)/com.aryansandlesh.aud-dip-tunnel | grep -E "state|pid"   # status
launchctl kickstart -k gui/$(id -u)/com.aryansandlesh.aud-dip-tunnel                   # restart (new URL!)
launchctl bootout gui/$(id -u)/com.aryansandlesh.aud-dip-tunnel                        # take it off the internet
```

## Runs at login (launchd)
`~/Library/LaunchAgents/com.aryansandlesh.aud-dip-watch.plist` starts `server.py` at login and
restarts it if it exits. Logs: `data/server.log`.

```bash
launchctl print gui/$(id -u)/com.aryansandlesh.aud-dip-watch | grep -E "state|pid"   # status
launchctl kickstart -k gui/$(id -u)/com.aryansandlesh.aud-dip-watch                   # restart after editing server.py
launchctl bootout gui/$(id -u)/com.aryansandlesh.aud-dip-watch                        # stop until next login
```

## Files
| file | what it is |
|---|---|
| `index.html` | the whole app (no build step) |
| `icici.py` | shared core: fetch, parse, format, push |
| `poll.py` | one polling pass — what GitHub Actions runs |
| `rate.json` | public rate history (committed by the workflow) |
| `alerts.json` | public alerts + push settings |
| `.github/workflows/poll.yml` | the every-10-minutes schedule |
| `server.py` | poller, alert engine, ntfy sender, static server |
| `data/config.json` | ntfy topic, admin key, push settings, appUrl |
| `data/icici.json` | every rate change, 90 days |
| `data/alerts.json` | your range alerts |
| `serve.command` | double-click launcher (not needed while the launchd agent runs) |
| `geturl.command` | double-click to print the current public URL |
| `data/tunnel.log` | cloudflared's log — the public URL is in here |
