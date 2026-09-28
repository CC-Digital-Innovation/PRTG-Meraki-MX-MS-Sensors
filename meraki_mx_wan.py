#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki MX WAN uplinks -- state, loss, latency and
traffic for every uplink of one appliance, in one sensor.

Reads organization-wide datasets through meraki_cache.py, so a whole fleet of
appliances costs three API calls per scan interval:

  appliance/uplink/statuses          active / ready / failed / not connected
  devices/uplinksLossAndLatency      loss and latency to the uplink probe target
  appliance/uplinks/usage/byNetwork  bytes sent / received over the last 5 min

Appliance channels:
  40  Appliance Reporting (1 = checked in within --offline-after seconds)
  41  Uplinks In Service (count of uplinks that are active or ready)

Meraki keeps the last known uplink state for an appliance that stops
reporting, so a dead appliance can still read "active". Channel 40 catches
that from the last check-in time.

Channels per uplink (wan1 10..14, wan2 20..24, cellular 30..34):
  +0  Uplink Failed (1 = failed; error)
  +1  Loss (%)
  +2  Latency (ms)
  +3  Traffic In (Mbit/s, 5 min average)
  +4  Traffic Out (Mbit/s, 5 min average)

An uplink gets channels when it is not "not connected", or when it is listed
in --uplinks. List the uplinks that should be in service so one that is
unplugged alarms instead of dropping out of the sensor.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <MX> --org-id <org> [--api-key %scriptplaceholder1]
  [--uplinks wan1,wan2] [--ip 8.8.8.8]
  [--loss-warn 2] [--loss-error 5] [--lat-warn 150] [--lat-error 300]
  [--offline-after 900] [--cache-ttl 240] [--max-stale 900] [--splay N]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import time
import random
import argparse
from datetime import datetime

from prtg_out import emit, fail, chan, read_args, clean
from meraki_cache import org_get, check_key, MerakiError

UPLINKS = {"wan1": 10, "wan2": 20, "cellular": 30}
WINDOW = 300  # seconds of traffic the usage call totals


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--serial", required=True)
    p.add_argument("--org-id", required=True)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--uplinks", default="", help="uplinks expected in service, e.g. wan1,wan2")
    p.add_argument("--ip", default="8.8.8.8",
                   help="preferred loss/latency target; falls back to the best-answering one")
    p.add_argument("--loss-warn", type=float, default=2)
    p.add_argument("--loss-error", type=float, default=5)
    p.add_argument("--lat-warn", type=float, default=150)
    p.add_argument("--lat-error", type=float, default=300)
    p.add_argument("--offline-after", type=float, default=900,
                   help="seconds since the last check-in before the appliance counts as not reporting")
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
        statuses, age = get("/appliance/uplink/statuses", perPage=1000)
        lossl, a1 = get("/devices/uplinksLossAndLatency", timespan=WINDOW)
        usage, a2 = get("/appliance/uplinks/usage/byNetwork", timespan=WINDOW)
        age = max(age, a1, a2)
    except MerakiError as e:
        fail(str(e))

    dev = next((d for d in statuses or [] if d.get("serial") == serial), None)
    if not dev:
        fail("{} is not an appliance in organization {}.".format(serial, org))
    state = {u.get("interface"): u.get("status") for u in dev.get("uplinks", [])}
    try:
        seen = datetime.fromisoformat(dev["lastReportedAt"].replace("Z", "+00:00")).timestamp()
        since = time.time() - seen
    except Exception:
        since = None
    reporting = since is not None and since <= a.offline_after
    in_service = sum(1 for s in state.values() if s in ("active", "ready"))
    head = [chan(40, "Appliance Reporting", 1 if reporting else 0, ctype="integer",
                 kind="custom", display_unit="state", err_lower=0.5),
            chan(41, "Uplinks In Service", in_service, ctype="integer", kind="count",
                 err_lower=0.5)]
    if not reporting:
        emit(head, "{} {} is not reporting (last check-in {})".format(
            serial, dev.get("model", ""), dev.get("lastReportedAt") or "never"), status="error")
        return

    # Latest loss/latency sample per uplink. An appliance often monitors
    # several targets, and some never answer (a provider gateway that drops
    # ICMP, an IPv6 target on an IPv4-only uplink); taking whichever comes
    # first reports a healthy link as 100% loss. Use --ip when it is monitored,
    # otherwise the target with the lowest current loss.
    probe = {}
    for row in lossl or []:
        if row.get("serial") != serial or row.get("uplink") not in UPLINKS:
            continue
        samples = [s for s in row.get("timeSeries") or [] if s.get("lossPercent") is not None]
        if not samples:
            continue
        last = max(samples, key=lambda s: s.get("ts", ""))
        rank = (0 if a.ip and row.get("ip") == a.ip else 1, float(last["lossPercent"]))
        cur = probe.get(row["uplink"])
        if cur is None or rank < cur["rank"]:
            probe[row["uplink"]] = {"sample": last, "ip": row.get("ip"), "rank": rank}

    traffic = {}
    for net in usage or []:
        for u in net.get("byUplink") or []:
            if u.get("serial") == serial:
                traffic[u.get("interface")] = u

    expected = {x.strip() for x in a.uplinks.split(",") if x.strip() in UPLINKS}
    shown = sorted({i for i, s in state.items() if i in UPLINKS and s != "not connected"} | expected,
                   key=lambda i: UPLINKS[i])
    if not shown:
        emit(head, "{} reports no uplinks in service (states: {})".format(
            serial, state or "none"), status="error")
        return

    channels, summary, failed, worst = list(head), [], [], "ok"
    for up in shown:
        base, st = UPLINKS[up], state.get(up, "missing")
        bad = st not in ("active", "ready")
        channels.append(chan(base, "Uplink Failed ({})".format(up), 1 if bad else 0,
                             ctype="integer", kind="custom", display_unit="state",
                             err_upper=0.5))
        if bad:
            failed.append("{} {}".format(up, st))
        part = "{} {}".format(up, st)
        pr = probe.get(up)
        if pr:
            loss = float(pr["sample"]["lossPercent"])
            lat = float(pr["sample"].get("latencyMs") or 0)
            channels.append(chan(base + 1, "Loss ({})".format(up), round(loss, 2), kind="percent",
                                 warn_upper=a.loss_warn, err_upper=a.loss_error))
            channels.append(chan(base + 2, "Latency ({})".format(up), round(lat, 1),
                                 kind="time_milliseconds",
                                 warn_upper=a.lat_warn, err_upper=a.lat_error))
            part += " loss {:.1f}% {:.0f} ms to {}".format(loss, lat, pr["ip"])
            if loss > a.loss_error or lat > a.lat_error:
                worst = "error"
            elif (loss > a.loss_warn or lat > a.lat_warn) and worst == "ok":
                worst = "warning"
        t = traffic.get(up)
        if t:
            rx = float(t.get("received") or 0) * 8 / WINDOW / 1e6
            tx = float(t.get("sent") or 0) * 8 / WINDOW / 1e6
            channels.append(chan(base + 3, "Traffic In ({})".format(up), round(rx, 3),
                                 kind="custom", display_unit="Mbit/s"))
            channels.append(chan(base + 4, "Traffic Out ({})".format(up), round(tx, 3),
                                 kind="custom", display_unit="Mbit/s"))
            part += " in {:.1f}/out {:.1f} Mbit/s".format(rx, tx)
        summary.append(part)

    text = "; ".join(summary)
    if age > a.cache_ttl:
        text += " (data {:.0f} s old)".format(age)
    if failed:
        worst, text = "error", "UPLINK DOWN {} | {}".format(", ".join(failed), text)
    emit(channels, text, status=worst)


if __name__ == "__main__":
    main()
