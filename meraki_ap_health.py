#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki MR access point health, per AP.

One sensor per access point, reading organization-wide datasets through
meraki_cache.py so the whole fleet costs a handful of API calls per scan
interval rather than several per AP:

  devices/statuses                          online / alerting / offline / dormant
  wireless/ssids/statuses/byDevice          which radios are broadcasting, channel, power
  wireless/devices/channelUtilization       total, Wi-Fi and non-Wi-Fi utilization per band
  wireless/devices/ethernet/statuses        link speed, duplex, power mode
  wireless/devices/packetLoss/byDevice      upstream / downstream packet loss

Channels (ids are fixed so history stays with the channel):
  10       Device Online (1 = online or alerting)
  20..25   2.4 GHz: Radio Broadcasting, Channel, Channel Utilization,
           Wi-Fi Utilization, Non-Wi-Fi Utilization, Transmit Power
  30..35   5 GHz, same layout
  40..45   6 GHz, same layout
  50       Ethernet Speed (Mbit/s)
  51       Ethernet Full Duplex (1 = full)
  52       Full Power (1 = full power mode; 0 = low power, which limits radios)
  53, 54   Packet Loss Downstream / Upstream (%)

A band gets channels when the AP reports it (a broadcasting SSID or channel
utilization on that band), or when it is listed in --bands. Listing the bands
you expect makes a radio that disappears alarm instead of going quiet.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <MR> --org-id <org> [--api-key %scriptplaceholder1]
  [--bands 2.4,5] [--util-warn 60] [--util-error 80] [--min-speed 1000]
  [--loss-warn N] [--loss-error N] [--offline-status error|warning]
  [--cache-ttl 240] [--max-stale 900] [--splay N]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import time
import random
import argparse

from prtg_out import emit, fail, chan, read_args, clean
from meraki_cache import org_get, check_key, MerakiError

BANDS = {"2.4": 20, "5": 30, "6": 40}


def band_of(value):
    """Normalise a Meraki band label ("2.4", "5", "6", "2.4 GHz") to a BANDS key."""
    v = str(value or "").lower().replace("ghz", "").strip()
    return v if v in BANDS else None


def find(rows, serial, *path):
    """Return the row whose serial (at the given key path) matches."""
    for r in rows or []:
        v = r
        for k in path:
            v = (v or {}).get(k)
        if v == serial:
            return r
    return None


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--serial", required=True)
    p.add_argument("--org-id", required=True)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--bands", default="", help="expected bands, e.g. 2.4,5 (default: as reported)")
    p.add_argument("--util-warn", type=float, default=60)
    p.add_argument("--util-error", type=float, default=80)
    p.add_argument("--min-speed", type=float, default=1000,
                   help="warn when the uplink negotiates below this Mbit/s (0 disables)")
    p.add_argument("--loss-warn", type=float)
    p.add_argument("--loss-error", type=float)
    p.add_argument("--offline-status", default="error", choices=["error", "warning"])
    p.add_argument("--cache-ttl", type=float, default=240)
    p.add_argument("--max-stale", type=float, default=900)
    p.add_argument("--splay", type=float, default=0)
    a = read_args(p)

    key = clean(a.api_key)
    problem = check_key(key)
    if problem:
        fail(problem)
    serial, org = clean(a.serial).upper(), clean(a.org_id)
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    def get(path, **params):
        return org_get(key, "/organizations/{}{}".format(org, path), params,
                       ttl=a.cache_ttl, max_stale=a.max_stale)

    try:
        statuses, age = get("/devices/statuses", perPage=1000, **{"productTypes[]": "wireless"})
        st = find(statuses, serial, "serial")
        if not st:
            fail("{} is not a wireless device in organization {}.".format(serial, org))
        name = st.get("name") or serial
        state = st.get("status") or "unknown"
        online = state in ("online", "alerting")
        channels = [chan(10, "Device Online", 1 if online else 0, ctype="integer",
                         kind="custom", display_unit="state", err_lower=0.5)]
        if not online:
            emit(channels, "{} is {} (last reported {})".format(
                name, state, st.get("lastReportedAt") or "never"), status=a.offline_status)
            return

        ssids, a1 = get("/wireless/ssids/statuses/byDevice", perPage=500)
        util, a2 = get("/wireless/devices/channelUtilization/byDevice",
                       perPage=1000, timespan=600, interval=300)
        eth, a3 = get("/wireless/devices/ethernet/statuses", perPage=1000)
        loss, a4 = get("/wireless/devices/packetLoss/byDevice", perPage=1000, timespan=900)
        age = max(age, a1, a2, a3, a4)
    except MerakiError as e:
        fail(str(e))

    # radios: a band is up if any BSS on it is broadcasting
    radio = {}
    for bss in (find(ssids, serial, "serial") or {}).get("basicServiceSets", []):
        r = bss.get("radio") or {}
        b = band_of(r.get("band"))
        if not b:
            continue
        cur = radio.setdefault(b, {"up": 0})
        if r.get("isBroadcasting"):
            cur["up"] = 1
            cur["channel"] = r.get("channel")
            cur["power"] = r.get("power")
    usage = {}
    for bb in (find(util, serial, "serial") or {}).get("byBand", []):
        b = band_of(bb.get("band"))
        if b:
            usage[b] = bb
    expected = {band_of(x) for x in a.bands.split(",") if band_of(x)}
    bands = sorted(set(radio) | set(usage) | expected, key=float)

    summary, down = [], []
    for b in bands:
        base, r, u = BANDS[b], radio.get(b, {"up": 0}), usage.get(b, {})
        label = "{} GHz".format(b)
        channels.append(chan(base, "Radio Broadcasting ({})".format(label), r["up"],
                             ctype="integer", kind="custom", display_unit="state",
                             err_lower=0.5))
        if not r["up"]:
            down.append(label)
        if r.get("channel") is not None:
            channels.append(chan(base + 1, "Channel ({})".format(label), r["channel"],
                                 ctype="integer", kind="custom", display_unit="#"))
        pct = lambda k: (u.get(k) or {}).get("percentage")
        if pct("total") is not None:
            channels.append(chan(base + 2, "Channel Utilization ({})".format(label),
                                 round(float(pct("total")), 1), kind="percent",
                                 warn_upper=a.util_warn, err_upper=a.util_error))
            summary.append("{} ch{} {:.0f}%".format(label, r.get("channel", "?"), float(pct("total"))))
        if pct("wifi") is not None:
            channels.append(chan(base + 3, "Wi-Fi Utilization ({})".format(label),
                                 round(float(pct("wifi")), 1), kind="percent"))
        if pct("nonWifi") is not None:
            channels.append(chan(base + 4, "Non-Wi-Fi Utilization ({})".format(label),
                                 round(float(pct("nonWifi")), 1), kind="percent"))
        if r.get("power") is not None:
            channels.append(chan(base + 5, "Transmit Power ({})".format(label), r["power"],
                                 ctype="integer", kind="custom", display_unit="dBm"))

    e = find(eth, serial, "serial") or {}
    port = next(iter(e.get("ports") or []), {})
    neg = port.get("linkNegotiation") or {}
    slow = False
    if neg.get("speed") is not None:
        channels.append(chan(50, "Ethernet Speed", neg["speed"], ctype="integer",
                             kind="custom", display_unit="Mbit/s",
                             warn_lower=a.min_speed if a.min_speed else None))
        slow = bool(a.min_speed) and neg["speed"] < a.min_speed
    if neg.get("duplex"):
        channels.append(chan(51, "Ethernet Full Duplex", 1 if neg["duplex"] == "full" else 0,
                             ctype="integer", kind="custom", display_unit="state",
                             warn_lower=0.5))
    if (e.get("power") or {}).get("mode"):
        channels.append(chan(52, "Full Power", 1 if e["power"]["mode"] == "full" else 0,
                             ctype="integer", kind="custom", display_unit="state",
                             warn_lower=0.5))

    lrow = find(loss, serial, "device", "serial") or {}
    for cid, direction in ((53, "downstream"), (54, "upstream")):
        v = (lrow.get(direction) or {}).get("lossPercentage")
        if v is not None:
            channels.append(chan(cid, "Packet Loss {}".format(direction.title()),
                                 round(float(v), 2), kind="percent",
                                 warn_upper=a.loss_warn, err_upper=a.loss_error))

    text = "{}: {}".format(name, "; ".join(summary) or "no radio data")
    if neg.get("speed") is not None:
        text += "; eth {} Mbit/s {}".format(neg["speed"], neg.get("duplex", ""))
    if age > a.cache_ttl:
        text += " (data {:.0f} s old)".format(age)
    status = "ok"
    if down:
        status, text = "error", "RADIO DOWN {} | {}".format(",".join(down), text)
    elif slow or neg.get("duplex") not in (None, "full"):
        status = "warning"
    emit(channels, text, status=status)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # report it as a sensor message, not a bare "exit code 1"
        fail("Unexpected error: {}: {}".format(type(e).__name__, e))
