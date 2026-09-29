#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki network health -- device and uplink states
for one network, as counts.

A replacement for PRTG's native "Cisco Meraki Network Health" sensor with the
same channels, read from two organization-wide calls through meraki_cache.py:

  networks            network names (cached for an hour)
  devices/statuses    online / alerting / offline / dormant, every device
  uplinks/statuses    active / ready / failed / not connected, every uplink
                      of every appliance and cellular gateway

The native sensor queries per network on every scan. With one sensor per
network that is dozens of calls per interval, on an organization whose API
budget is shared with every other integration; under load it reports "API
request limit reached" or no data. Here every network's sensor reads the same
two cached datasets, so a whole organization costs two calls per cache period.

Channels (ids are fixed so history stays with the channel):
  10..13  Uplinks "Active", "Ready", "Failed", "Not Connected"
  20..23  MX "Online", "Alerting", "Offline", "Dormant"   (appliances and
          cellular gateways, grouped as the native sensor does)
  30..33  MS "Online", "Alerting", "Offline", "Dormant"
  40..43  MR "Online", "Alerting", "Offline", "Dormant"
  50..53  IOT "Online", "Alerting", "Offline", "Dormant"  (cameras, sensors, other)

A device group gets channels when the network has devices of that group. The
message names the devices that are offline or alerting and the uplinks that
have failed, so the sensor says what is wrong, not only how many.

Limits default to the native sensor's layout and can be changed per group:
  --uplink-failed    error     (Uplinks "Failed" above 0)
  --alerting         error     (any group's "Alerting" above 0)
  --mx-offline       error     (MX "Offline" above 0)
  --offline          warning   (MS / MR / IOT "Offline" above 0)
  --mx-dormant       warning   (MX "Dormant" above 0)
  --dormant          off       (MS / MR / IOT "Dormant")
Each takes error, warning or off. Uplinks "Not Connected" never alarms: an
unused second WAN port reads as not connected.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --network-id <N> --org-id <org> [--api-key %scriptplaceholder1]
  [limit options above] [--list 5] [--cache-ttl 240] [--max-stale 900] [--splay N]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import time
import random
import argparse

from prtg_out import emit, fail, chan, read_args, clean
from meraki_cache import org_get, check_key, MerakiError

GROUPS = {"MX": 20, "MS": 30, "MR": 40, "IOT": 50}
PRODUCT = {"appliance": "MX", "cellularGateway": "MX", "switch": "MS", "wireless": "MR"}
STATES = ["Online", "Alerting", "Offline", "Dormant"]
UPLINKS = [("active", "Active"), ("ready", "Ready"), ("failed", "Failed"),
           ("not connected", "Not Connected")]
LEVELS = ["error", "warning", "off"]


def limit(level):
    """Channel limits for a count that should be zero."""
    if level == "error":
        return {"err_upper": 0.5}
    if level == "warning":
        return {"warn_upper": 0.5}
    return {}


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--network-id", required=True)
    p.add_argument("--org-id", required=True)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--uplink-failed", default="error", choices=LEVELS)
    p.add_argument("--alerting", default="error", choices=LEVELS)
    p.add_argument("--mx-offline", default="error", choices=LEVELS)
    p.add_argument("--offline", default="warning", choices=LEVELS)
    p.add_argument("--mx-dormant", default="warning", choices=LEVELS)
    p.add_argument("--dormant", default="off", choices=LEVELS)
    p.add_argument("--list", type=int, default=5, help="devices to name per state in the message")
    p.add_argument("--cache-ttl", type=float, default=240)
    p.add_argument("--max-stale", type=float, default=900)
    p.add_argument("--splay", type=float, default=0)
    a = read_args(p)

    key = clean(a.api_key)
    problem = check_key(key)
    if problem:
        fail(problem)
    net, org = clean(a.network_id), clean(a.org_id)
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    def get(path, **params):
        return org_get(key, "/organizations/{}{}".format(org, path), params,
                       ttl=a.cache_ttl, max_stale=a.max_stale)

    try:
        devices, age = get("/devices/statuses", perPage=1000)
        uplinks, a1 = get("/uplinks/statuses", perPage=1000)
        age = max(age, a1)
    except MerakiError as e:
        fail(str(e))

    try:
        networks, _ = org_get(key, "/organizations/{}/networks".format(org), {"perPage": 1000},
                              ttl=3600, max_stale=86400)
        netname = next((n.get("name") for n in networks or [] if n.get("id") == net), None)
    except MerakiError:
        networks, netname = None, net
    if networks is not None and netname is None:
        # the native sensor keeps showing Up with blank channels in this case
        fail("Network {} no longer exists in organization {} (deleted, or re-created "
             "under a new id); point this sensor at the current network.".format(net, org))

    mine = [d for d in devices or [] if d.get("networkId") == net]
    if not mine:
        emit([chan(10 + i, 'Uplinks "{}"'.format(label), 0, ctype="integer", kind="count")
              for i, (_, label) in enumerate(UPLINKS)],
             "{}: no devices in this network".format(netname), status="warning")
        return

    counts = {g: dict.fromkeys(STATES, 0) for g in GROUPS}
    named = {}
    for d in mine:
        g = PRODUCT.get(d.get("productType"), "IOT")
        state = (d.get("status") or "").title()
        if state in counts[g]:
            counts[g][state] += 1
        if state in ("Offline", "Alerting"):
            named.setdefault(state, []).append(d.get("name") or d.get("serial"))
    present = {PRODUCT.get(d.get("productType"), "IOT") for d in mine}

    up = dict.fromkeys([s for s, _ in UPLINKS], 0)
    failed = []
    for u in uplinks or []:
        if u.get("networkId") != net:
            continue
        for x in u.get("uplinks", []):
            s = x.get("status")
            if s in up:
                up[s] += 1
            if s == "failed":
                failed.append("{} {}".format(u.get("model") or u.get("serial"), x.get("interface")))

    channels = []
    for i, (s, label) in enumerate(UPLINKS):
        lim = limit(a.uplink_failed) if s == "failed" else {}
        channels.append(chan(10 + i, 'Uplinks "{}"'.format(label), up[s], ctype="integer",
                             kind="count", **lim))
    for g, base in GROUPS.items():
        if g not in present:
            continue
        for i, state in enumerate(STATES):
            if state == "Alerting":
                lim = limit(a.alerting)
            elif state == "Offline":
                lim = limit(a.mx_offline if g == "MX" else a.offline)
            elif state == "Dormant":
                lim = limit(a.mx_dormant if g == "MX" else a.dormant)
            else:
                lim = {}
            channels.append(chan(base + i, '{} "{}"'.format(g, state), counts[g][state],
                                 ctype="integer", kind="count", **lim))

    parts = ["{}: {} online".format(netname, sum(c["Online"] for c in counts.values()))]
    for state in ("Offline", "Alerting"):
        names = named.get(state, [])
        if names:
            more = len(names) - a.list
            parts.append("{} {}: {}{}".format(len(names), state.lower(), ", ".join(names[:a.list]),
                                              " +{} more".format(more) if more > 0 else ""))
    if failed:
        parts.append("uplink failed: " + ", ".join(failed))
    parts.append("uplinks {} active / {} ready".format(up["active"], up["ready"]))
    text = "; ".join(parts)
    if age > a.cache_ttl:
        text += " (data {:.0f} s old)".format(age)
    emit(channels, text)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail("Unexpected error: {}: {}".format(type(e).__name__, e))
