#!/usr/bin/env python3
"""Shared core: read ICICI Bank's AUD rate, decide what to notify, push via ntfy.

Used by poll.py (one shot, e.g. a GitHub Actions run) and server.py (long-running, local).
Standard library only — no dependencies to install anywhere.
"""
import datetime as dt
import json
import os
import re
import ssl
import urllib.request

ICICI_URL = "https://www.icici.bank.in/corporate/global-markets/forex/forex-card-rate"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
AEST = dt.timezone(dt.timedelta(hours=10))      # times in pushes are Australian eastern
KEEP_DAYS = 90
COOLDOWN = 10 * 60                              # seconds before a range alert may re-fire
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")


# ---------- plumbing
def ssl_context():
    """python.org's macOS build ships no root certificates; borrow certifi's or the system bundle."""
    for cafile in (None, "/etc/ssl/cert.pem"):
        try:
            if cafile is None:
                import certifi  # optional
                cafile = certifi.where()
            ctx = ssl.create_default_context(cafile=cafile)
            if ctx.cert_store_stats().get("x509_ca", 0):
                return ctx
        except (ImportError, OSError, ssl.SSLError):
            continue
    return ssl.create_default_context()


SSL_CTX = ssl_context()


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj, indent=None):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---------- the rate
def fetch_html(url=ICICI_URL, timeout=30, bust=False):
    """bust=True appends a cache-buster: something in front of icici.bank.in serves a cached
    copy for the bare URL (observed a day-old rate), despite its no-store header."""
    if bust:
        url += ("&" if "?" in url else "?") + "cb=%d" % int(dt.datetime.now(dt.timezone.utc).timestamp())
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-AU,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "ignore")


def parse_card_rate(html):
    """Return {'t', 'ttBuy', 'cardBuy', 'ttSell', 'cardSell', 'ddSell'} for AUD.

    'ttSell' is the TT selling rate — what an outward INR->AUD transfer is priced at.
    't' is ICICI's own "rate set at" timestamp (published in IST), as epoch milliseconds.

    Columns are located by their HEADING, never by position: ICICI reshuffled this table in
    Oct 2026 (dropping a column, which shifted TT Selling from index 6 to 5). A fixed index
    would have silently returned the wrong rate, so a layout change must fail loudly instead.
    """
    when = _parse_timestamp(html)
    row, header = _find_currency_row(html, "Australian Dollar (AUD)")
    cells = _cells(row)
    labels = _cells(header)

    def col(*names, side=None):
        """Index of the first heading matching one of `names`, optionally on the buy/sell side."""
        for i, lab in enumerate(labels):
            low = lab.lower()
            if any(n in low for n in names):
                if side == "buy" and "sell" in low:
                    continue
                if side == "sell" and "sell" not in low and "buy" in low:
                    continue
                return i
        return None

    # the heading row has no cell for the currency name, so values sit one to the right
    def val(idx):
        if idx is None or idx + 1 >= len(cells):
            return None
        x = cells[idx + 1]
        return float(x) if re.fullmatch(r"\d+(\.\d+)?", x) else None

    sell_from = next((i for i, l in enumerate(labels) if "sell" in l.lower()), None)
    if sell_from is None:
        raise ValueError("no selling-rate column found; headings: %r" % labels)
    buy_labels = labels[:sell_from]

    tt_sell = val(next((i for i, l in enumerate(labels) if i >= sell_from and "tt" in l.lower()), None))
    card_sell = val(next((i for i, l in enumerate(labels) if i >= sell_from and "card" in l.lower()), None))
    tt_buy = val(next((i for i, l in enumerate(buy_labels) if "tt" in l.lower()), None))
    card_buy = val(next((i for i, l in enumerate(buy_labels) if "card" in l.lower()), None))

    if tt_sell is None:
        raise ValueError("TT selling rate not found; headings=%r cells=%r" % (labels, cells))
    # a transfer rate far from the buying rate, or an implausible level, means we misread the table
    if tt_buy is not None and not (tt_buy < tt_sell < tt_buy * 1.25):
        raise ValueError("TT selling %.2f is implausible against TT buying %.2f" % (tt_sell, tt_buy))
    if not 1 < tt_sell < 1000:
        raise ValueError("TT selling %.4f is out of range" % tt_sell)

    dd_sell = val(next((i for i, l in enumerate(labels) if i >= sell_from and "demand draft" in l.lower()), None))
    return {"t": int(when.timestamp() * 1000), "ttBuy": tt_buy, "cardBuy": card_buy,
            "ttSell": tt_sell, "cardSell": card_sell, "ddSell": dd_sell}


def _cells(row_html):
    out = []
    for m in re.finditer(r"<t([hd])[^>]*>(.*?)</t\1>", row_html, re.S):
        out.append(_text(m.group(2)))
    return out


def _text(frag):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", frag)).replace("&nbsp;", " ").strip()


def _parse_timestamp(html):
    """ICICI's 'rate set at' time, in IST. Handles both the current and the pre-Oct-2026 layout."""
    # current: <div id="date-field">13-10-2026</div> ... <div id="timestamp">09:13:41 AM</div>
    md = re.search(r'id="date-field"[^>]*>\s*(\d{2})-(\d{2})-(\d{4})', html)
    mt = re.search(r'id="timestamp"[^>]*>\s*(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)', html, re.I)
    if not (md and mt):
        # older layout: <strong>Date:</strong> 22-09-2026 ... <strong>Time:</strong> 09:15:47 AM
        md = re.search(r"Date:\s*(?:</?\w+[^>]*>\s*)*(\d{2})-(\d{2})-(\d{4})", html)
        mt = re.search(r"Time:\s*(?:</?\w+[^>]*>\s*)*(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)", html, re.I)
    if not (md and mt):
        raise ValueError("rate timestamp not found")
    d, mo, y = md.groups()
    h, mi, sec, ampm = mt.groups()
    h = int(h) % 12 + (12 if ampm.upper() == "PM" else 0)
    return dt.datetime(int(y), int(mo), int(d), h, int(mi), int(sec), tzinfo=IST)


def _find_currency_row(html, label):
    """The table row for `label`, plus the heading row above it.

    The currency name also appears in a filter dropdown, so match a real <tr> with numbers in it.
    """
    rows = [(m.start(), m.group(1)) for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S)]
    target = None
    for i, (_pos, r) in enumerate(rows):
        cells = _cells(r)
        if cells and label in cells[0] and sum(bool(re.fullmatch(r"\d+(\.\d+)?", c)) for c in cells) >= 4:
            target = i
            break
    if target is None:
        raise ValueError("%s row not found in any rate table" % label)
    # the nearest preceding row that names the rate columns
    for j in range(target - 1, -1, -1):
        labels = _cells(rows[j][1])
        if any("sell" in l.lower() for l in labels) and any("tt" in l.lower() for l in labels):
            return rows[target][1], rows[j][1]
    raise ValueError("heading row with TT/selling columns not found above the %s row" % label)


def append_change(store, rec):
    """Add rec to store {'latest','history'} if it is a new rate. Returns the previous point or None."""
    hist = store.setdefault("history", [])
    point = [rec["t"], rec["ttSell"], rec["cardSell"]]
    store["latest"] = rec
    if hist and hist[-1][0] == point[0] and hist[-1][1] == point[1]:
        return None, False
    prev = hist[-1] if hist else None
    hist.append(point)
    cutoff = int((dt.datetime.now(dt.timezone.utc).timestamp() - KEEP_DAYS * 86400) * 1000)
    store["history"] = [p for p in hist if p[0] >= cutoff]
    return prev, True


# ---------- formatting
def fmt_inr(v):
    return "₹%.2f" % v


def fmt_ist(t_ms):
    return dt.datetime.fromtimestamp(t_ms / 1000, IST).strftime("%H:%M")


def fmt_local(t_ms, tz=None):
    return dt.datetime.fromtimestamp(t_ms / 1000, tz or AEST).strftime("%H:%M")


def today_summary(history, tz=None):
    """(best, best_t, high, n_changes) for today, counting the rate in force at midnight."""
    tz = tz or AEST
    now = dt.datetime.now(tz)
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    carried = None
    for p in history:
        if p[0] < start:
            carried = p
    pts = ([carried] if carried else []) + [p for p in history if p[0] >= start]
    if not pts:
        return None
    best = min(pts, key=lambda p: p[1])
    return best[1], best[0], max(p[1] for p in pts), len([p for p in history if p[0] >= start])


def alert_text(a):
    lo, hi = a.get("low"), a.get("high")
    if lo is not None and hi is not None:
        return "%s – %s" % (fmt_inr(lo), fmt_inr(hi))
    if hi is not None:
        return "%s or below" % fmt_inr(hi)
    if lo is not None:
        return "%s or above" % fmt_inr(lo)
    return "any rate"


# ---------- ntfy
def ntfy_send(cfg, title, body, priority="default", tags="bell", click=None):
    """cfg: {'server':..., 'topic':...}. Returns (ok, info)."""
    topic = (cfg or {}).get("topic")
    if not topic:
        return False, "no ntfy topic configured"
    url = (cfg.get("server") or "https://ntfy.sh").rstrip("/") + "/" + topic
    # HTTP headers can't carry the rupee sign: ASCII title, symbols in the body
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority,
               "Tags": tags, "User-Agent": UA}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return True, "sent (%s)" % r.status
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def change_message(prev, rec, history):
    """(title, body) for a rate change — the same words on every channel."""
    d = rec["ttSell"] - prev[1]
    arrow = "▼" if d < 0 else "▲"
    title = "ICICI AUD %s %.2f INR" % ("down to" if d < 0 else "up to", rec["ttSell"])
    body = "%s %s%.2f from %s (set %s IST)." % (fmt_inr(rec["ttSell"]), arrow, abs(d), fmt_inr(prev[1]), fmt_ist(rec["t"]))
    s = today_summary(history)
    if s:
        body += "\nToday's best %s at %s." % (fmt_inr(s[0]), fmt_local(s[1]))
    return title, body


ECB_URL = "https://api.frankfurter.dev/v1/%s..?base=AUD&symbols=INR"


def estimate_margin(history, days=120):
    """How much ICICI's TT selling rate sits above the interbank mid-market rate.

    ICICI publishes one rate each weekday morning and leaves it, so its rate is a stale
    snapshot of a market that keeps moving. Knowing the margin lets the page turn the LIVE
    market rate into an estimate of the next rate ICICI will set — which is measurably better
    than assuming today's rate repeats. Returns None when there is not enough overlap yet.

    `mae` is this estimator's own mean absolute error over the sample, so the page can show an
    honest error bar rather than a bare number.
    """
    if len(history) < 4:
        return None
    start = dt.datetime.fromtimestamp(history[0][0] / 1000, dt.timezone.utc) - dt.timedelta(days=1)
    try:
        body = fetch_html(ECB_URL % start.strftime("%Y-%m-%d"), timeout=25)
        mkt = {d: v["INR"] for d, v in json.loads(body)["rates"].items() if v.get("INR")}
    except Exception:
        return None

    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() * 1000 - days * 86400_000
    pairs = []
    for t, tt, _card in history:
        if t < cutoff:
            continue
        day = dt.datetime.fromtimestamp(t / 1000, IST).strftime("%Y-%m-%d")
        if day in mkt:
            pairs.append((mkt[day], tt))
    if len(pairs) < 4:
        return None

    margins = [(tt / m - 1) * 100 for m, tt in pairs]
    mean = sum(margins) / len(margins)
    var = sum((x - mean) ** 2 for x in margins) / len(margins)
    errs = [abs(m * (1 + mean / 100) - tt) for m, tt in pairs]
    return {
        "pct": round(mean, 3),
        "sd": round(var ** 0.5, 3),
        "mae": round(sum(errs) / len(errs), 3),
        "n": len(pairs),
        "asOf": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
    }


def web_push(app_url, push_key, title, body, tag="rate"):
    """Ask the app's own /api/push to notify every phone registered in the web app."""
    if not app_url or not push_key:
        return False, "not configured"
    payload = json.dumps({"title": title, "body": body, "tag": tag}).encode("utf-8")
    req = urllib.request.Request(app_url.rstrip("/") + "/api/push", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "X-Push-Key": push_key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as r:
            return True, "sent to %s phone(s)" % json.loads(r.read().decode()).get("sent", "?")
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def notify_change(cfg, prev, rec, history, click=None):
    title, body = change_message(prev, rec, history)
    down = rec["ttSell"] < prev[1]
    return ntfy_send(cfg, title, body, tags="chart_with_downwards_trend" if down else "chart_with_upwards_trend", click=click)


def hourly_body(rec, history):
    body = "ICICI AUD %s (set %s IST)." % (fmt_inr(rec["ttSell"]), fmt_ist(rec["t"]))
    s = today_summary(history)
    if s:
        body += "\nToday: best %s at %s, high %s, %d change%s." % (
            fmt_inr(s[0]), fmt_local(s[1]), fmt_inr(s[2]), s[3], "" if s[3] == 1 else "s")
    return body


def evaluate_alerts(cfg, alerts, rate, set_at_ms, now_s, click=None):
    """Edge-triggered range alerts. Mutates `alerts`; returns (changed, [(alert, ok, info)])."""
    sent, changed = [], False
    for a in alerts:
        if not a.get("on") or rate is None:
            continue
        inside = (a.get("low") is None or rate >= a["low"]) and (a.get("high") is None or rate <= a["high"])
        if inside and not a.get("inRange") and now_s - a.get("firedAt", 0) >= COOLDOWN:
            a["firedAt"] = now_s
            body = "ICICI AUD %s is in your alert range %s (set %s IST)." % (fmt_inr(rate), alert_text(a), fmt_ist(set_at_ms))
            ok, info = ntfy_send(cfg, "ALERT: ICICI AUD at %.2f INR" % rate, body,
                                 priority="high", tags="rotating_light,bell", click=click)
            sent.append((a, ok, info))
            changed = True
        if inside != bool(a.get("inRange")):
            a["inRange"] = inside
            changed = True
    return changed, sent
