#!/usr/bin/env python3
"""PRTG Script v2 sensor: Meraki MX appliance utilization (perfScore).

Device-scoped: needs only the MX serial. This is the same statistic the
Dashboard shows under Organization > Summary report > (an -appliance network)
> Utilization. Meraki publishes it as the signal for when an appliance is
running out of headroom and should be sized up, so the defaults alarm at
75% (warning) and 90% (error).

Two appliance states return no score, and both are reported as-is rather
than as a failure:

- HTTP 204 with an empty body: the appliance is dormant or has not reported
  recently. There is nothing to score.
- HTTP 400 "Feature not supported": the passive unit of a warm-spare HA
  pair. Only the active unit carries a perfScore; monitor the primary.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <MX> [--api-key %windowspassword]
  [--util-warn 75] [--util-error 90] [--splay N]
  [--idle-status ok|warning]

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

# Meraki returns these instead of a score; they are states, not errors.
NO_SCORE_UNSUPPORTED = "unsupported"   # HTTP 400 "Feature not supported"
NO_SCORE_IDLE = "idle"                 # HTTP 204, empty body


def api(api_key, path, tries=3):
    """GET path and return (payload, no_score_reason).

    payload is None when Meraki reports no score for this appliance, in which
    case no_score_reason says which of the two states it is.
    """
    req = urllib.request.Request(API_BASE + path, headers={
        "Authorization": "Bearer {}".format(api_key), "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8").strip()
                if r.status == 204 or not body:
                    return None, NO_SCORE_IDLE
                return json.loads(body), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(min(float(e.headers.get("Retry-After") or 2), 15))
                continue
            detail = e.read().decode("utf-8", "ignore")[:250]
            if e.code == 400 and "not supported" in detail.lower():
                return None, NO_SCORE_UNSUPPORTED
            fail("Meraki API HTTP {} on {}: {}".format(e.code, path, detail))
        except Exception as e:
            fail("Request failed on {}: {}".format(path, e))


def main():
    p = argparse.ArgumentParser(exit_on_error=False)
    p.add_argument("--serial", required=True)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"))
    p.add_argument("--util-warn", type=float, default=75)
    p.add_argument("--util-error", type=float, default=90)
    p.add_argument("--splay", type=float, default=0)
    p.add_argument("--idle-status", default="ok", choices=["ok", "warning"],
                   help="sensor status when the appliance reports no score "
                        "(dormant, or an HA passive unit)")
    a = read_args(p)

    api_key = clean(a.api_key)
    if not api_key:
        fail("No API key (--api-key / placeholder / MERAKI_API_KEY came through empty).")
    if api_key.startswith("%") or " " in api_key:
        fail("API key looks unresolved/malformed (starts with '{}').".format(api_key[:1]))
    serial = clean(a.serial)
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    perf, no_score = api(api_key, "/devices/{}/appliance/performance".format(
                         urllib.parse.quote(serial)))

    if no_score:
        note = ("HA passive unit (Meraki scores only the active unit) -- "
                "monitor the primary instead" if no_score == NO_SCORE_UNSUPPORTED
                else "no utilization reported (appliance dormant or not checked in)")
        # No channel value: reporting 0 here would read as a healthy appliance.
        emit([], "{}: {}".format(serial, note), status=a.idle_status)
        return

    score = perf.get("perfScore")
    if score is None:
        fail("No perfScore in the response for {} (got keys: {}).".format(
            serial, sorted(perf)[:6]))
    utilization = float(score)

    channels = [
        chan(10, "Device Utilization", round(utilization, 1),
             ctype="float", kind="percent",
             warn_upper=a.util_warn, err_upper=a.util_error),
    ]
    status = "error" if utilization >= a.util_error else \
             ("warning" if utilization >= a.util_warn else "ok")
    emit(channels, "Device Utilization: {:.1f}%".format(utilization), status=status)


if __name__ == "__main__":
    main()
