#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki MX WAN traffic for both uplinks, with a floor.

One sensor reports Traffic In/Out (Mbit/s) for wan1 and wan2, plus rolling
60-minute Peak In/Out channels. A lower error limit (a "floor") on the WAN2
out-peak catches a site that has silently stopped using WAN2: if the best minute
in a full hour is under the floor, the sensor goes to error. This is a useful
detector for SD-WAN flow-preference problems that quietly idle an uplink.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --network-id <NET> [--api-key %windowspassword] [--uplink both|wan1|wan2]
  [--floor-out-wan2 3.0]  (Mbit/s; default 3.0; 0 disables)
  [--floor-in-wan2 N] [--floor-out-wan1 N] [--floor-in-wan1 N] [--splay N]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import sys
import json
import time
import random
import argparse
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

from prtg_out import emit, fail, chan, read_args, clean

API_BASE = "https://api.meraki.com/api/v1"


def api(api_key, path, params, tries=3):
    url = "{}{}?{}".format(API_BASE, path, urllib.parse.urlencode(params, doseq=True))
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer {}".format(api_key), "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(min(float(e.headers.get("Retry-After") or 2), 15))
                continue
            fail("Meraki API HTTP {}: {}".format(e.code, e.read().decode("utf-8", "ignore")[:250]))
        except Exception as e:
            fail("Request failed: {}".format(e))


def secs(entry):
    try:
        t0 = datetime.fromisoformat(entry["startTime"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(entry["endTime"].replace("Z", "+00:00"))
        d = (t1 - t0).total_seconds()
        return d if d > 0 else 60
    except Exception:
        return 60


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--network-id", required=True)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--uplink", default="both", choices=["both", "wan1", "wan2"])
    p.add_argument("--floor-in-wan1", type=float)
    p.add_argument("--floor-out-wan1", type=float)
    p.add_argument("--floor-in-wan2", type=float)
    p.add_argument("--floor-out-wan2", type=float, default=3.0)
    p.add_argument("--splay", type=float, default=0)
    a = read_args(p)

    api_key = clean(a.api_key)
    if not api_key:
        fail("No API key (--api-key / placeholder / MERAKI_API_KEY empty).")
    if api_key.startswith("%") or " " in api_key:
        fail("API key looks unresolved/malformed.")
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    hist = api(api_key, "/networks/{}/appliance/uplinks/usageHistory".format(
               urllib.parse.quote(clean(a.network_id))),
               {"timespan": 3600, "resolution": 60})
    if not hist:
        fail("No usage samples in the last hour.")

    wanted = ["wan1", "wan2"] if a.uplink == "both" else [a.uplink]
    floors = {("in", "wan1"): a.floor_in_wan1, ("out", "wan1"): a.floor_out_wan1,
              ("in", "wan2"): a.floor_in_wan2, ("out", "wan2"): a.floor_out_wan2}

    # rolling 60-min peaks (Mbit/s) + latest complete-minute rate per uplink
    peaks, latest = {}, {}
    for e in hist:
        s = secs(e)
        for i in e.get("byInterface", []):
            k = i.get("interface", "").lower()
            imb = (float(i.get("received") or 0) * 8) / s / 1e6
            omb = (float(i.get("sent") or 0) * 8) / s / 1e6
            peaks.setdefault(k, [0.0, 0.0])
            peaks[k][0] = max(peaks[k][0], imb)
            peaks[k][1] = max(peaks[k][1], omb)
            if i.get("sent") is not None or i.get("received") is not None:
                latest[k] = (imb, omb)

    base = {"wan1": 10, "wan2": 20}
    channels, summary, floored = [], [], []
    for name in wanted:
        if name not in latest:
            continue
        b = base[name]
        rin, rout = latest[name]
        pin, pout = peaks.get(name, [0.0, 0.0])
        channels.append(chan(b + 0, "Traffic In ({})".format(name), round(rin, 3),
                             kind="custom", display_unit="Mbit/s"))
        channels.append(chan(b + 1, "Traffic Out ({})".format(name), round(rout, 3),
                             kind="custom", display_unit="Mbit/s"))
        for off, direction, pk in ((2, "in", pin), (3, "out", pout)):
            fl = floors.get((direction, name))
            fl = fl if (fl and fl > 0) else None
            channels.append(chan(b + off, "Peak {} 60m ({})".format(direction.title(), name),
                                 round(pk, 3), kind="custom", display_unit="Mbit/s",
                                 err_lower=fl))
            if fl is not None and pk < fl:
                floored.append("{}/{}".format(name, direction))
        summary.append("{} in {:.2f}/out {:.2f} Mbit/s (60m peak {:.1f}/{:.1f})".format(
            name, rin, rout, pin, pout))

    if not channels:
        fail("No data for uplink(s) {} -- check MX / network id / reporting delay.".format(wanted))
    text = "WAN traffic: " + "; ".join(summary)
    status = "ok"
    if floored:
        text = "LOW TRAFFIC " + ",".join(floored) + " | " + text
        status = "error"
    emit(channels, text, status=status)


if __name__ == "__main__":
    main()
