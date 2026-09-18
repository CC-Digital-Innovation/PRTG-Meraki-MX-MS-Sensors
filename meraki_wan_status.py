#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki MX WAN uplink loss, latency, jitter, and state.

Device-scoped: needs only the MX serial. Thresholds ship as channel limits
(loss warn > 2% / error > 3%, latency warn > 150 / error > 300 ms by default).
Pass --org-id to add an "Uplink Failed" channel from the organization uplink
status call; its history is the WAN connection-loss record, which is separate
from packet loss.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <MX> --uplink wan1|wan2 --ip 8.8.8.8 [--api-key %scriptplaceholder1]
  [--org-id <org-id>] [--loss-warn 2] [--loss-error 3]
  [--lat-warn 150] [--lat-error 300] [--splay N]

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
            fail("Meraki API HTTP {} on {}: {}".format(
                e.code, path, e.read().decode("utf-8", "ignore")[:250]))
        except Exception as e:
            fail("Request failed on {}: {}".format(path, e))


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--serial", required=True)
    p.add_argument("--uplink", default="wan1")
    p.add_argument("--ip", default="8.8.8.8")
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--org-id")
    p.add_argument("--loss-warn", type=float, default=2)
    p.add_argument("--loss-error", type=float, default=3)
    p.add_argument("--lat-warn", type=float, default=150)
    p.add_argument("--lat-error", type=float, default=300)
    p.add_argument("--splay", type=float, default=0)
    a = read_args(p)

    api_key = clean(a.api_key)
    if not api_key:
        fail("No API key (--api-key / placeholder / MERAKI_API_KEY came through empty).")
    if api_key.startswith("%") or " " in api_key:
        fail("API key looks unresolved/malformed (starts with '{}').".format(api_key[:1]))
    serial, uplink, ip = clean(a.serial), clean(a.uplink).lower(), clean(a.ip)
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    hist = api(api_key, "/devices/{}/lossAndLatencyHistory".format(urllib.parse.quote(serial)),
               {"timespan": 600, "uplink": uplink, "ip": ip})
    samples = [h for h in hist if h.get("lossPercent") is not None]
    if not samples:
        fail("No loss/latency sample for {} {} to {} in 10 min (check --ip matches a "
             "monitored uplink target, and the uplink is up).".format(serial, uplink, ip))
    last = max(samples, key=lambda h: h.get("endTime", ""))
    loss = float(last["lossPercent"])
    latency = float(last.get("latencyMs") or 0)
    jitter = float(last.get("jitter") or 0)

    channels = [
        chan(10, "Loss Percent ({})".format(uplink), round(loss, 2),
             ctype="float", kind="percent",
             warn_upper=a.loss_warn, err_upper=a.loss_error),
        chan(11, "Latency ({})".format(uplink), round(latency, 1),
             ctype="float", kind="time_milliseconds",
             warn_upper=a.lat_warn, err_upper=a.lat_error),
        chan(12, "Jitter ({})".format(uplink), round(jitter, 2),
             ctype="float", kind="time_milliseconds"),
    ]
    text = "WAN {} to {}: loss {:.2f}%, latency {:.0f} ms".format(uplink, ip, loss, latency)

    if a.org_id:
        try:
            st = api(api_key, "/organizations/{}/appliance/uplink/statuses".format(
                     clean(a.org_id)), {"serials[]": serial})
            up = next((u for d in st for u in d.get("uplinks", [])
                       if u.get("interface") == uplink), None)
            state = (up or {}).get("status", "missing")
            channels.append(chan(13, "Uplink Failed ({})".format(uplink),
                                 0 if state in ("active", "ready") else 1,
                                 ctype="integer", kind="custom", display_unit="state",
                                 err_upper=0.5))
            text += ", status {}".format(state)
        except Exception:
            pass

    status = "error" if loss > a.loss_error or latency > a.lat_error else \
             ("warning" if loss > a.loss_warn or latency > a.lat_warn else "ok")
    emit(channels, text, status=status)


if __name__ == "__main__":
    main()
