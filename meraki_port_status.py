#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki MS switch port status with label-aware alarming.

Per-port channels named "NN: <label>" (1 = up, 0 = down). A port only alarms on
down if it is in the alarm set:

- --alert-mode active (the recommended default): the port is up now, or passed
  traffic within --lookback-hours. A normally-used port going dark alarms;
  unused spares stay quiet.
- --alert-mode all-labeled: any port with a Meraki label.
- --alert-mode selective: the Meraki port tag --alert-tag, --alert-labels
  globs, or an explicit --alert-ports list.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <SW> [--api-key %scriptplaceholder1] [--alert-mode active]
  [--lookback-hours 24] [--alert-tag prtg-alarm] [--alert-labels "UPLINK*"]
  [--alert-ports 1,2] [--splay N]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import sys
import json
import time
import random
import fnmatch
import argparse
import urllib.request
import urllib.parse
import urllib.error

from prtg_out import emit, fail, chan, read_args, clean

API_BASE = "https://api.meraki.com/api/v1"


def api(api_key, path, tries=3):
    req = urllib.request.Request(API_BASE + path, headers={
        "Authorization": "Bearer {}".format(api_key), "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(min(float(e.headers.get("Retry-After") or 2), 15))
                continue
            fail("Meraki API HTTP {} on {}: {}".format(
                e.code, path, e.read().decode("utf-8", "ignore")[:250]))
        except Exception as e:
            fail("Request failed on {}: {}".format(path, e))


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--serial", required=True)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--alert-mode", default="active",
                   choices=["active", "all-labeled", "selective"])
    p.add_argument("--lookback-hours", type=float, default=24)
    p.add_argument("--alert-tag", default="prtg-alarm")
    p.add_argument("--alert-labels", default="")
    p.add_argument("--alert-ports", default="")
    p.add_argument("--splay", type=float, default=0)
    a = read_args(p)

    api_key = clean(a.api_key)
    if not api_key:
        fail("No API key (--api-key / placeholder / MERAKI_API_KEY empty).")
    if api_key.startswith("%") or " " in api_key:
        fail("API key looks unresolved/malformed.")
    serial = clean(a.serial)
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    lookback = int(a.lookback_hours * 3600)
    conf = {c["portId"]: c for c in api(api_key, "/devices/{}/switch/ports".format(
            urllib.parse.quote(serial)))}
    stat = api(api_key, "/devices/{}/switch/ports/statuses?timespan={}".format(
               urllib.parse.quote(serial), lookback))

    globs = [g.strip() for g in a.alert_labels.split(",") if g.strip()]
    explicit = {x.strip() for x in a.alert_ports.split(",") if x.strip()}

    def in_alarm(pid, label, tags, has_label, up, was_active):
        if pid in explicit:
            return True
        if a.alert_tag and a.alert_tag in tags:
            return True
        if a.alert_mode == "all-labeled" and has_label:
            return True
        if a.alert_mode == "active" and (up or was_active):
            return True
        return any(fnmatch.fnmatch(label, g) for g in globs)

    per_port, down_alarm = [], []
    connected = errors = warnings = clients = alarm_count = 0
    poe = 0.0
    for i, s in enumerate(sorted(stat, key=lambda x: int(x["portId"]))):
        pid = s["portId"]
        c = conf.get(pid, {})
        has_label = bool(c.get("name"))
        label = c.get("name") or "(unused)"
        tags = c.get("tags") or []
        up = 1 if s.get("status") == "Connected" else 0
        connected += up
        clients += s.get("clientCount") or 0
        poe += s.get("powerUsageInWh") or 0.0
        if [e for e in s.get("errors", []) if e != "Port disconnected"]:
            errors += 1
        if s.get("warnings"):
            warnings += 1
        was_active = (s.get("usageInKb") or {}).get("total", 0) > 0
        alarm = in_alarm(pid, label, tags, has_label, up, was_active)
        ch = chan(100 + i, "{:02d}: {}".format(int(pid), label), up,
                  ctype="integer", kind="custom", display_unit="up")
        if alarm:
            alarm_count += 1
            ch["limits"] = {"error": {"lower": 1}}
            if not up:
                down_alarm.append("{}: {}".format(pid, label))
        per_port.append(ch)

    aggregates = [
        chan(10, "Alarm Ports Down", len(down_alarm), ctype="integer", kind="count",
             err_upper=0.5),
        chan(11, "Alarm Ports (selected)", alarm_count, ctype="integer", kind="count"),
        chan(12, "Ports Connected", connected, ctype="integer", kind="count"),
        chan(13, "Ports With Errors", errors, ctype="integer", kind="count"),
        chan(14, "Ports With Warnings", warnings, ctype="integer", kind="count"),
        chan(15, "Connected Clients", clients, ctype="integer", kind="count"),
        chan(16, "PoE Usage", round(poe, 1), ctype="float", kind="custom",
             display_unit="Wh"),
    ]

    text = "{} of {} ports connected; {} in alarm set".format(connected, len(stat), alarm_count)
    if down_alarm:
        text += " | DOWN: " + "; ".join(down_alarm)
    elif alarm_count == 0:
        text += " | no alarm ports (tag 'prtg-alarm' in Dashboard, or --alert-mode)"
    status = "error" if down_alarm else "ok"
    emit(aggregates + per_port, text, status=status)


if __name__ == "__main__":
    main()
