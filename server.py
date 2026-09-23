#!/usr/bin/env python3
"""AUD Dip Watch server — watches ICICI Bank's AUD rate and pushes alerts via ntfy.

  - polls ICICI's published forex card rate every 5 min; AUD "TT selling" is what an outward
    INR->AUD transfer is priced at. History of every change kept in data/icici.json (90 days)
  - alerts (data/alerts.json): range alerts, a push on every rate change, an hourly update
  - pushes go to an ntfy topic (data/config.json) — every phone subscribed to it is told
  - serves the app folder; the page is just a window onto this process (/api/state)
  - writes (alerts, settings, test push) need the admin key from data/config.json, so the page
    can be public while only the family changes anything

Standard library only. Run:  python3 server.py   (the launchd agent does this at login)
"""
import datetime as dt
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
ICICI_FILE = os.path.join(DATA_DIR, "icici.json")
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

ICICI_URL = "https://www.icici.bank.in/corporate/global-markets/forex/forex-card-rate"
PORT = int(os.environ.get("PORT", "8765"))
POLL_SECONDS = 300          # ICICI re-prices a few times a day; 5 min catches each change promptly
KEEP_DAYS = 90
COOLDOWN = 10 * 60          # a range alert may re-fire at most every 10 min while hovering at a boundary
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
DEFAULT_NOTIFY = {"changes": True, "hourly": False, "activeFrom": 7, "activeTo": 22}  # hourly is opt-in; hours are server-local

lock = threading.Lock()
state = {"icici": {"latest": None, "history": [], "fetchedAt": 0, "error": None}, "alerts": [], "config": {}}


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# ---------- files
def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1 if path != ICICI_FILE else None, ensure_ascii=False)
    os.replace(tmp, path)


def load_all():
    ic = read_json(ICICI_FILE, {})
    state["icici"]["latest"] = ic.get("latest")
    state["icici"]["history"] = ic.get("history", [])
    state["alerts"] = [a for a in read_json(ALERTS_FILE, []) if isinstance(a, dict)]
    cfg = read_json(CONFIG_FILE, {})
    before = json.dumps(cfg, sort_keys=True)
    cfg.setdefault("ntfy", {})
    if not cfg["ntfy"].get("topic"):
        cfg["ntfy"]["topic"] = "aud-dip-" + secrets.token_hex(8)     # the topic name is the subscribers' secret
    cfg["ntfy"].setdefault("server", "https://ntfy.sh")
    if not cfg.get("adminKey"):
        cfg["adminKey"] = secrets.token_hex(6)                        # needed to change alerts from any device
    cfg.setdefault("appUrl", "")      # the deployed app: link on pushes, and where web push is sent
    cfg.setdefault("pushKey", "")     # shared secret for the deployed app's /api/push
    cfg["notify"] = {**DEFAULT_NOTIFY, **cfg.get("notify", {})}
    if json.dumps(cfg, sort_keys=True) != before:
        write_json(CONFIG_FILE, cfg)
    state["config"] = cfg


# ---------- TLS (python.org's macOS build ships no root certificates)
def ssl_context():
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


# ---------- ICICI card rate
def parse_card_rate(html):
    """Return {'t', 'ttBuy', 'cardBuy', 'ttSell', 'cardSell', 'ddSell'} for AUD from ICICI's page."""
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
           "ttSell": num(cells[6]), "cardSell": num(cells[9]), "ddSell": num(cells[10]) if len(cells) > 10 else None}
    if rec["ttSell"] is None:
        raise ValueError("TT selling rate missing: %r" % cells)
    return rec


def poll_icici():
    ic = state["icici"]
    try:
        req = urllib.request.Request(ICICI_URL, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            html = r.read().decode("utf-8", "ignore")
        rec = parse_card_rate(html)
    except Exception as e:  # network, HTTP or layout change: keep the last good value, report the error
        with lock:
            ic["error"] = "%s: %s" % (type(e).__name__, e)
            ic["fetchedAt"] = int(time.time() * 1000)
        log("icici: " + ic["error"])
        return
    prev = None
    with lock:
        ic["error"] = None
        ic["fetchedAt"] = int(time.time() * 1000)
        ic["latest"] = rec
        hist = ic["history"]
        point = [rec["t"], rec["ttSell"], rec["cardSell"]]
        changed = not hist or hist[-1][0] != point[0] or hist[-1][1] != point[1]
        if changed:
            prev = hist[-1] if hist else None
            hist.append(point)
            cutoff = int((time.time() - KEEP_DAYS * 86400) * 1000)
            ic["history"] = [p for p in hist if p[0] >= cutoff]
            write_json(ICICI_FILE, {"latest": rec, "history": ic["history"]})
            log("icici: AUD TT sell %.2f (set %s IST)" % (rec["ttSell"], fmt_ist(rec["t"])))
    if changed:
        if prev is not None and prev[1] != rec["ttSell"]:
            notify_change(prev, rec)
        evaluate_alerts()


# ---------- messages
def fmt_inr(v):
    return "₹%.2f" % v


def fmt_ist(t_ms):
    return dt.datetime.fromtimestamp(t_ms / 1000, IST).strftime("%H:%M")


def fmt_local(t_ms):
    return dt.datetime.fromtimestamp(t_ms / 1000).strftime("%H:%M")


def today_summary():
    """(best, best_t, high, changes) for the local calendar day, counting the rate in force at midnight."""
    now = dt.datetime.now()
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    hist = state["icici"]["history"]
    carried = None
    for p in hist:
        if p[0] < start:
            carried = p
    pts = ([carried] if carried else []) + [p for p in hist if p[0] >= start]
    if not pts:
        return None
    best = min(pts, key=lambda p: p[1])
    return best[1], best[0], max(p[1] for p in pts), len([p for p in hist if p[0] >= start])


def alert_text(a):
    lo, hi = a.get("low"), a.get("high")
    if lo is not None and hi is not None:
        return "%s – %s" % (fmt_inr(lo), fmt_inr(hi))
    if hi is not None:
        return "%s or below" % fmt_inr(hi)
    if lo is not None:
        return "%s or above" % fmt_inr(lo)
    return "any rate"


def ntfy_send(title, body, priority="default", tags="bell"):
    cfg = state["config"].get("ntfy", {})
    if not cfg.get("topic"):
        return False, "no topic configured"
    url = cfg.get("server", "https://ntfy.sh").rstrip("/") + "/" + cfg["topic"]
    # HTTP headers can't carry the rupee sign: ASCII title, symbols in the body
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": tags, "User-Agent": UA}
    if state["config"].get("appUrl"):
        headers["Click"] = state["config"]["appUrl"]
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return True, "sent (%s)" % r.status
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def notify_change(prev, rec):
    if not state["config"]["notify"].get("changes"):
        return
    d = rec["ttSell"] - prev[1]
    arrow = "▼" if d < 0 else "▲"
    title = "ICICI AUD %s %.2f INR" % ("down to" if d < 0 else "up to", rec["ttSell"])
    body = "%s %s%s from %s (set %s IST)." % (fmt_inr(rec["ttSell"]), arrow, "%.2f" % abs(d), fmt_inr(prev[1]), fmt_ist(rec["t"]))
    s = today_summary()
    if s:
        body += "\nToday's best %s at %s." % (fmt_inr(s[0]), fmt_local(s[1]))
    ok, info = ntfy_send(title, body, tags="chart_with_downwards_trend" if d < 0 else "chart_with_upwards_trend")
    log("ntfy push -> " + info)
    import icici  # shared with poll.py
    ok, info = icici.web_push(state["config"].get("appUrl"), state["config"].get("pushKey"), title, body)
    log("web push  -> " + info)


def hourly_update():
    ic = state["icici"]["latest"]
    if not ic:
        return
    s = today_summary()
    body = "ICICI AUD %s (set %s IST)." % (fmt_inr(ic["ttSell"]), fmt_ist(ic["t"]))
    if s:
        body += "\nToday: best %s at %s, high %s, %d change%s." % (fmt_inr(s[0]), fmt_local(s[1]), fmt_inr(s[2]), s[3], "" if s[3] == 1 else "s")
    ok, info = ntfy_send("Hourly: ICICI AUD %.2f INR" % ic["ttSell"], body, priority="low", tags="clock%d" % (dt.datetime.now().hour % 12 or 12))
    log("hourly push -> " + info)


def hourly_loop():
    while True:
        now = dt.datetime.now()
        nxt = (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1))
        time.sleep(max(1, (nxt - now).total_seconds()))
        n = state["config"]["notify"]
        h = dt.datetime.now().hour
        if n.get("hourly") and n["activeFrom"] <= h < n["activeTo"]:
            try:
                hourly_update()
            except Exception as e:
                log("hourly crashed: %s: %s" % (type(e).__name__, e))


def evaluate_alerts():
    """Edge-triggered: fire when the rate enters the range, re-arm when it leaves, 10-min cooldown."""
    fired = []
    with lock:
        ic = state["icici"]["latest"]
        rate = ic and ic["ttSell"]
        if rate is None:
            return
        now = time.time()
        changed = False
        for a in state["alerts"]:
            if not a.get("on"):
                continue
            inside = (a.get("low") is None or rate >= a["low"]) and (a.get("high") is None or rate <= a["high"])
            if inside and not a.get("inRange") and now - a.get("firedAt", 0) >= COOLDOWN:
                a["firedAt"] = now
                fired.append(dict(a))
            if inside != bool(a.get("inRange")):
                a["inRange"] = inside
                changed = True
        if changed or fired:
            write_json(ALERTS_FILE, state["alerts"])
        set_at = fmt_ist(ic["t"])
    for a in fired:
        body = "ICICI AUD %s is in your alert range %s (set %s IST)." % (fmt_inr(rate), alert_text(a), set_at)
        ok, info = ntfy_send("ALERT: ICICI AUD at %.2f INR" % rate, body, priority="high", tags="rotating_light,bell")
        log("alert %s -> %s" % (a.get("id"), info))


def poll_loop():
    while True:
        try:
            poll_icici()
        except Exception as e:  # never let the poller die
            log("poll crashed: %s: %s" % (type(e).__name__, e))
        time.sleep(POLL_SECONDS)


# ---------- HTTP
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def authorized(self):
        key = self.headers.get("X-Admin-Key", "")
        return bool(key) and secrets.compare_digest(key, state["config"]["adminKey"])

    def api_state(self):
        with lock:
            cfg = state["config"]
            return {
                "icici": {"latest": state["icici"]["latest"], "history": state["icici"]["history"],
                          "fetchedAt": state["icici"]["fetchedAt"], "error": state["icici"]["error"]},
                "alerts": state["alerts"],
                "settings": cfg["notify"],
                "ntfy": {"server": cfg["ntfy"]["server"], "topic": cfg["ntfy"]["topic"]},
                "canEdit": self.authorized(),
                "now": int(time.time() * 1000),
            }

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self.send_json(self.api_state())
        if path.startswith("/data/") or path == "/server.py":
            return self.send_error(404)
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self.send_error(404)
        if not self.authorized():
            return self.send_json({"error": "admin key required"}, 401)
        if path == "/api/alerts":
            b = self.read_body()
            lo, hi = b.get("low"), b.get("high")
            lo = float(lo) if isinstance(lo, (int, float)) and lo > 0 else None
            hi = float(hi) if isinstance(hi, (int, float)) and hi > 0 else None
            if lo is None and hi is None:
                return self.send_json({"error": "need a low or high bound"}, 400)
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            a = {"id": secrets.token_hex(4), "low": lo, "high": hi, "on": True, "inRange": False, "firedAt": 0,
                 "created": int(time.time() * 1000)}
            with lock:
                state["alerts"].append(a)
                write_json(ALERTS_FILE, state["alerts"])
            evaluate_alerts()
            return self.send_json(self.api_state())
        if path == "/api/ntfy/test":
            ic = state["icici"]["latest"]
            body = "Test from AUD Dip Watch. ICICI AUD is %s right now. You're subscribed - alerts will arrive here." % (fmt_inr(ic["ttSell"]) if ic else "n/a")
            ok, info = ntfy_send("AUD Dip Watch test", body, tags="white_check_mark")
            log("ntfy test -> " + info)
            return self.send_json({"ok": ok, "info": info}, 200 if ok else 502)
        self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self.send_error(404)
        if not self.authorized():
            return self.send_json({"error": "admin key required"}, 401)
        b = self.read_body()
        m = re.fullmatch(r"/api/alerts/([0-9a-f]{8})", path)
        if m:
            with lock:
                for a in state["alerts"]:
                    if a["id"] == m.group(1) and "on" in b:
                        a["on"] = bool(b["on"])
                        if not a["on"]:
                            a["inRange"] = False
                write_json(ALERTS_FILE, state["alerts"])
            evaluate_alerts()
            return self.send_json(self.api_state())
        if path == "/api/settings":
            with lock:
                n = state["config"]["notify"]
                for k in ("changes", "hourly"):
                    if k in b:
                        n[k] = bool(b[k])
                for k in ("activeFrom", "activeTo"):
                    if isinstance(b.get(k), int) and 0 <= b[k] <= 24:
                        n[k] = b[k]
                write_json(CONFIG_FILE, state["config"])
            return self.send_json(self.api_state())
        self.send_error(404)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/alerts/([0-9a-f]{8})", urlparse(self.path).path)
        if not m:
            return self.send_error(404)
        if not self.authorized():
            return self.send_json({"error": "admin key required"}, 401)
        with lock:
            state["alerts"] = [a for a in state["alerts"] if a["id"] != m.group(1)]
            write_json(ALERTS_FILE, state["alerts"])
        return self.send_json(self.api_state())

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):  # quiet for normal traffic; 4xx/5xx still logged
        code = str(args[1]) if len(args) > 1 else ""
        if code[:1] in ("4", "5") and code != "401":
            super().log_message(fmt, *args)


def lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


if __name__ == "__main__":
    load_all()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=hourly_loop, daemon=True).start()
    ip = lan_ip()
    print("AUD Dip Watch", flush=True)
    print("  this Mac:   http://localhost:%d" % PORT, flush=True)
    if ip:
        print("  same Wi-Fi: http://%s:%d" % (ip, PORT), flush=True)
    print("  ntfy topic: %s   (subscribe in the ntfy app to get alerts)" % state["config"]["ntfy"]["topic"], flush=True)
    print("  admin key:  %s   (enter once per device to edit alerts)" % state["config"]["adminKey"], flush=True)
    print("  ICICI polled every %d min; alerts in data/alerts.json" % (POLL_SECONDS // 60), flush=True)
    print(flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
