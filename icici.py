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
def fetch_html(url=ICICI_URL, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "ignore")


def parse_card_rate(html):
    """Return {'t', 'ttBuy', 'cardBuy', 'ttSell', 'cardSell', 'ddSell'} for AUD.

    'ttSell' is the TT selling rate — what an outward INR->AUD transfer is priced at.
    't' is ICICI's own "rate set at" timestamp (published in IST), as epoch milliseconds.
    """
    md = re.search(r"Date:\s*(?:</?\w+[^>]*>\s*)*(\d{2})-(\d{2})-(\d{4})", html)
    mt = re.search(r"Time:\s*(?:</?\w+[^>]*>\s*)*(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)", html, re.I)
    if not md or not mt:
        raise ValueError("rate timestamp not found")
    d, mo, y = md.groups()
    h, mi, s, ampm = mt.groups()
    h = int(h) % 12 + (12 if ampm.upper() == "PM" else 0)
    when = dt.datetime(int(y), int(mo), int(d), h, int(mi), int(s), tzinfo=IST)

    i = html.find("Australian Dollar (AUD)")
    if i < 0:
        raise ValueError("AUD row not found")
    row = html[html.rfind("<tr", 0, i):html.find("</tr>", i)]
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
    # Columns: currency, [buy] TT, bills, notes, card, DD, [sell] TT, bills, notes, card, DD
    if len(cells) < 10:
        raise ValueError("unexpected AUD row shape: %r" % cells)

    def num(x):
        return float(x) if re.fullmatch(r"\d+(\.\d+)?", x) else None

    rec = {"t": int(when.timestamp() * 1000), "ttBuy": num(cells[1]), "cardBuy": num(cells[4]),
           "ttSell": num(cells[6]), "cardSell": num(cells[9]),
           "ddSell": num(cells[10]) if len(cells) > 10 else None}
    if rec["ttSell"] is None:
        raise ValueError("TT selling rate missing: %r" % cells)
    return rec


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


def notify_change(cfg, prev, rec, history, click=None):
    d = rec["ttSell"] - prev[1]
    arrow = "▼" if d < 0 else "▲"
    title = "ICICI AUD %s %.2f INR" % ("down to" if d < 0 else "up to", rec["ttSell"])
    body = "%s %s%.2f from %s (set %s IST)." % (fmt_inr(rec["ttSell"]), arrow, abs(d), fmt_inr(prev[1]), fmt_ist(rec["t"]))
    s = today_summary(history)
    if s:
        body += "\nToday's best %s at %s." % (fmt_inr(s[0]), fmt_local(s[1]))
    return ntfy_send(cfg, title, body, tags="chart_with_downwards_trend" if d < 0 else "chart_with_upwards_trend", click=click)


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
