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

The score is only served per device (there is no organization-wide call), so
on an organization near its API rate limit a slow or rejected call is common.
The call goes through meraki_cache.py, keyed per appliance: when it fails, the
last good score up to --max-stale seconds old is reported instead, with its
age in the message, rather than failing the scan.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <MX> [--api-key %scriptplaceholder1]
  [--util-warn 75] [--util-error 90] [--splay N]
  [--idle-status ok|warning] [--max-stale 1500]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import time
import random
import argparse
import urllib.parse

from prtg_out import emit, fail, chan, read_args, clean
from meraki_cache import org_get, check_key, MerakiError


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
    p.add_argument("--max-stale", type=float, default=1500,
                   help="seconds a last good score may be reused when the API call fails")
    a = read_args(p)

    api_key = clean(a.api_key)
    problem = check_key(api_key)
    if problem:
        fail(problem)
    serial = clean(a.serial)
    if a.splay:
        time.sleep(random.uniform(0, a.splay))

    path = "/devices/{}/appliance/performance".format(urllib.parse.quote(serial))
    try:
        perf, age = org_get(api_key, path, ttl=120, max_stale=a.max_stale)
    except MerakiError as e:
        if "not supported" in str(e).lower():
            # No channel value: reporting 0 would read as a healthy, idle appliance.
            emit([], "{}: HA passive unit (Meraki scores only the active unit) -- "
                     "monitor the primary instead".format(serial), status=a.idle_status)
            return
        fail(str(e))

    if not perf:  # HTTP 204, empty body
        emit([], "{}: no utilization reported (appliance dormant or not checked in)".format(
            serial), status=a.idle_status)
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
    text = "Device Utilization: {:.1f}%".format(utilization)
    if age > 120:
        text += " (API unavailable; last good value {:.0f} s old)".format(age)
    status = "error" if utilization >= a.util_error else \
             ("warning" if utilization >= a.util_warn else "ok")
    emit(channels, text, status=status)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail("Unexpected error: {}: {}".format(type(e).__name__, e))
