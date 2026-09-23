#!/usr/bin/env python3
"""One polling pass — built for GitHub Actions, but runs anywhere.

Reads ICICI's AUD rate, appends it to rate.json if it changed, pushes to ntfy, and
evaluates the range alerts in alerts.json. Writes files back so the workflow can commit them.

Environment:
  NTFY_TOPIC    (required to push)  the ntfy topic — keep it in GitHub Secrets
  NTFY_SERVER   (default https://ntfy.sh)
  APP_URL       (optional)          link a push opens when tapped
  HOURLY        (optional)          set to "1" by the hourly workflow run

Exit code is 0 unless ICICI could not be read, so a failed run is visible in Actions.
"""
import datetime as dt
import os
import sys
import time

import icici

ROOT = os.path.dirname(os.path.abspath(__file__))
RATE_FILE = os.path.join(ROOT, "rate.json")
ALERTS_FILE = os.path.join(ROOT, "alerts.json")


def main():
    cfg = {"topic": os.environ.get("NTFY_TOPIC", "").strip(),
           "server": os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip()}
    click = os.environ.get("APP_URL", "").strip() or None
    if not cfg["topic"]:
        print("note: NTFY_TOPIC is not set — will record the rate but send no pushes")

    store = icici.read_json(RATE_FILE, {"latest": None, "history": []})
    doc = icici.read_json(ALERTS_FILE, {})
    alerts = doc.get("alerts", [])
    settings = {"changes": True, "hourly": False, **doc.get("settings", {})}

    try:
        rec = icici.parse_card_rate(icici.fetch_html())
    except Exception as e:
        print("FAILED to read ICICI: %s: %s" % (type(e).__name__, e))
        return 1

    prev, changed = icici.append_change(store, rec)
    stamp = "%s (set %s IST)" % (icici.fmt_inr(rec["ttSell"]), icici.fmt_ist(rec["t"]))
    print(("new rate " if changed else "unchanged ") + stamp)

    if changed:
        icici.write_json(RATE_FILE, store)
        if prev is not None and prev[1] != rec["ttSell"] and settings["changes"]:
            ok, info = icici.notify_change(cfg, prev, rec, store["history"], click=click)
            print("change push -> " + info)

    if os.environ.get("HOURLY") == "1" and settings["hourly"]:
        ok, info = icici.ntfy_send(cfg, "Hourly: ICICI AUD %.2f INR" % rec["ttSell"],
                                   icici.hourly_body(rec, store["history"]),
                                   priority="low", tags="clock3", click=click)
        print("hourly push -> " + info)

    touched, sent = icici.evaluate_alerts(cfg, alerts, rec["ttSell"], rec["t"], time.time(), click=click)
    for a, ok, info in sent:
        print("alert %s -> %s" % (a.get("id", "?"), info))
    if touched:
        doc["alerts"] = alerts
        doc["settings"] = settings
        icici.write_json(ALERTS_FILE, doc, indent=1)

    print("checked at %s UTC" % dt.datetime.now(dt.timezone.utc).strftime("%H:%M"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
