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

PRTG supports 50 channels per sensor, and this sensor spends 7 of them on
aggregates, so one sensor charts up to 43 ports. A switch with more (the
MS150-48MP-4X reports 54) is covered by two or more sensors on the same
serial, each charting a slice of the ports:

  Sensor A: --serial <SW> --split 1/2
  Sensor B: --serial <SW> --split 2/2

--split N/M cuts the switch's ports into M contiguous, near-equal slices and
charts slice N, so the same parameters fit whatever port count the switch
reports. --ports picks the ports by number instead, to set the boundaries by
hand (for example, to keep the uplinks on one sensor).

Each sensor's aggregates cover its own slice (--scope range), so the sensors
divide a switch's alarms between them instead of each raising the same down
port. --scope switch makes the aggregates switch-wide; --aggregates
minimal|none trims them to leave room for more ports. A sensor with neither
--ports nor --split on a switch that does not fit reports an error naming the
split to use.

Run by hand with --list-ports for the port inventory and the recommended split.

Parameters (the PRTG Script v2 "Parameters" field, delivered on stdin):
  --serial <SW> [--api-key %scriptplaceholder1] [--alert-mode active]
  [--lookback-hours 24] [--alert-tag prtg-alarm] [--alert-labels "UPLINK*"]
  [--alert-ports 1,2] [--splay N]
  [--ports 1-27] [--split 1/2] [--aggregates full|minimal|none]
  [--scope range|switch] [--max-channels 50] [--list-ports]

The API key can also come from the MERAKI_API_KEY environment variable.
"""
import os
import sys
import json
import time
import random
import re
import math
import zlib
import fnmatch
import argparse
import urllib.request
import urllib.parse
import urllib.error

from prtg_out import emit, fail, chan, read_args, clean

API_BASE = "https://api.meraki.com/api/v1"

# PRTG supports this many channels on one sensor.
PRTG_CHANNEL_LIMIT = 50
# Aggregate channels emitted for each --aggregates choice.
AGGREGATE_COUNT = {"full": 7, "minimal": 2, "none": 0}

# Meraki documents portId as "commonly just the port number", but it "may
# contain additional identifying information such as the slot and module-type
# if the port is located on a port module" -- so int(portId) is not safe. It
# raises on modular hardware (MS390/C9300 line cards, MS425 flexible stacking),
# and an uncaught ValueError leaves the probe with a Python traceback instead
# of a Script v2 result, which reads as a broken sensor rather than a port id
# the script could not parse.
#
# Channel ids have to stay put for the life of a sensor: PRTG stores history
# against the id, so an id that moves reattributes one port's history to
# another. A numeric port takes 99 + its number, which is what enumeration
# produced for the usual contiguous 1..N switch, so sensors already deployed
# keep their history. Anything non-numeric takes a hashed id in a band above.
_MODULAR_BASE, _MODULAR_SPAN = 1000, 8000


def port_sort_key(port_id):
    """Natural order: port 2 before port 10, and modular ids grouped sensibly."""
    parts = [p for p in re.split(r"(\d+)", str(port_id)) if p]
    # (0, n, "") for a number and (1, 0, text) for text, so the tuples stay
    # comparable -- mixing int and str at one position raises TypeError.
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


def port_label(port_id):
    """The name shown in PRTG. Numeric ports keep their zero-padded form so
    channel names on sensors already deployed do not change."""
    pid = str(port_id)
    return "{:02d}".format(int(pid)) if pid.isdigit() else pid


def port_channel_id(port_id, taken):
    """A stable channel id for a port, avoiding collisions within one sensor."""
    pid = str(port_id)
    if pid.isdigit():
        cid = 99 + int(pid)
    else:
        cid = _MODULAR_BASE + zlib.crc32(pid.encode("utf-8")) % _MODULAR_SPAN
    while cid in taken:          # crc32 collision, or a numeric/hashed overlap
        cid += 1
    taken.add(cid)
    return cid


def parse_port_spec(spec):
    """Parse "1-24,49-54,1_MA-MOD-8X10G_1" into (ranges, literals).

    Ranges are inclusive and match ports whose id is a plain number. Any other
    token matches a port id exactly, so a modular port can still be picked.
    """
    ranges, literals = [], set()
    for tok in str(spec).split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", tok)
        if m and (m.group(1) or m.group(2)):
            lo = int(m.group(1)) if m.group(1) else 0
            hi = int(m.group(2)) if m.group(2) else 10 ** 9
            if lo > hi:
                fail("--ports range '{}' is backwards.".format(tok))
            ranges.append((lo, hi))
        elif tok.isdigit():
            ranges.append((int(tok), int(tok)))
        else:
            literals.add(tok)
    if not ranges and not literals:
        fail("--ports '{}' selected nothing parseable.".format(spec))
    return ranges, literals


def parse_split(spec):
    """Parse "1/2", "1of2" or "1:2" into (index, total), 1-based."""
    m = re.fullmatch(r"\s*(\d+)\s*(?:/|:|of)\s*(\d+)\s*", str(spec), re.I)
    if not m:
        fail("--split must look like 1/2 (got '{}').".format(spec))
    idx, total = int(m.group(1)), int(m.group(2))
    if total < 1 or not 1 <= idx <= total:
        fail("--split {}/{} is out of range.".format(idx, total))
    return idx, total


def apply_split(ordered, idx, total):
    """Cut `ordered` into `total` contiguous, near-equal slices; return slice idx."""
    base, extra = divmod(len(ordered), total)
    start = 0
    for k in range(1, total + 1):
        size = base + (1 if k <= extra else 0)
        if k == idx:
            return ordered[start:start + size]
        start += size
    return []


def suggest_split(n_ports, agg_count, limit=PRTG_CHANNEL_LIMIT):
    """Fewest --split sensors that fit n_ports, each carrying agg_count aggregates.

    The largest slice is ceil(n_ports / m) ports, and that plus the aggregates
    has to stay inside the limit.
    """
    room = limit - agg_count
    if room < 1:
        return n_ports
    return max(1, math.ceil(n_ports / room))


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
    p.add_argument("--ports", default="",
                   help="Ports to chart, e.g. '1-27' or '1-24,49-54'. Default: all.")
    p.add_argument("--split", default="",
                   help="Chart slice N of M, e.g. '1/2'. Applied after --ports.")
    p.add_argument("--aggregates", default="full", choices=sorted(AGGREGATE_COUNT),
                   help="Aggregate channels to emit: full (7), minimal (2) or none.")
    p.add_argument("--scope", default="range", choices=["range", "switch"],
                   help="Aggregates cover the charted ports (default) or the whole switch.")
    p.add_argument("--max-channels", type=int, default=PRTG_CHANNEL_LIMIT)
    p.add_argument("--list-ports", action="store_true",
                   help="Report the port inventory and the recommended split, then exit.")
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

    all_ports = sorted(stat, key=lambda x: port_sort_key(x.get("portId", "")))

    # Which ports get a channel on this sensor.
    charted = all_ports
    if clean(a.ports):
        ranges, literals = parse_port_spec(clean(a.ports))

        def wanted(pid):
            pid = str(pid).strip()
            return pid in literals or (
                pid.isdigit() and any(lo <= int(pid) <= hi for lo, hi in ranges))

        charted = [s for s in charted if wanted(s["portId"])]
    if clean(a.split):
        charted = apply_split(charted, *parse_split(clean(a.split)))
    charted_ids = {s["portId"] for s in charted}

    agg_count = AGGREGATE_COUNT[a.aggregates]
    limit = max(1, min(a.max_channels, PRTG_CHANNEL_LIMIT))
    need = suggest_split(len(all_ports), agg_count, limit)
    inventory = ",".join(str(s["portId"]) for s in all_ports)

    if a.list_ports:
        msg = "{} ports on {}: {}".format(len(all_ports), serial, inventory)
        if need > 1:
            msg += (" | {} ports + {} aggregates is over the {}-channel limit; use {} "
                    "sensors on this serial: --split 1/{} to --split {}/{}".format(
                        len(all_ports), agg_count, limit, need, need, need, need))
        else:
            msg += " | fits in one sensor"
        # Id 17 is used by nothing else here, so a sensor left on --list-ports
        # by mistake does not create a mislabelled aggregate channel.
        emit([chan(17, "Ports Total", len(all_ports), ctype="integer", kind="count")],
             msg, status="ok")
        return

    if not charted and (clean(a.ports) or clean(a.split)):
        fail("No ports selected by --ports '{}' / --split '{}' (the switch reports {} "
             "ports: {}).".format(a.ports, a.split, len(all_ports), inventory))
    if agg_count + len(charted) > limit:
        fail("Would emit {} channels ({} aggregates + {} ports) but PRTG supports {}. "
             "This switch reports {} ports. Split it across {} sensors on the same "
             "serial: '--split 1/{}' to '--split {}/{}'.".format(
                 agg_count + len(charted), agg_count, len(charted), limit,
                 len(all_ports), need, need, need, need))

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
    connected = errors = warnings = clients = alarm_count = counted = 0
    poe = 0.0
    taken = set()
    for s in all_ports:
        pid = s["portId"]
        is_charted = pid in charted_ids
        # With --scope range a sensor answers only for its own slice, so two
        # sensors on one switch divide the alarms instead of both raising them.
        if not (is_charted or a.scope == "switch"):
            continue
        c = conf.get(pid, {})
        has_label = bool(c.get("name"))
        label = c.get("name") or "(unused)"
        tags = c.get("tags") or []
        up = 1 if s.get("status") == "Connected" else 0
        counted += 1
        connected += up
        clients += s.get("clientCount") or 0
        poe += s.get("powerUsageInWh") or 0.0
        if [e for e in s.get("errors", []) if e != "Port disconnected"]:
            errors += 1
        if s.get("warnings"):
            warnings += 1
        was_active = (s.get("usageInKb") or {}).get("total", 0) > 0
        alarm = in_alarm(pid, label, tags, has_label, up, was_active)
        if alarm:
            alarm_count += 1
            if not up:
                down_alarm.append("{}: {}".format(pid, label))
        if not is_charted:
            continue
        ch = chan(port_channel_id(pid, taken), "{}: {}".format(port_label(pid), label),
                  up, ctype="integer", kind="custom", display_unit="up")
        if alarm:
            ch["limits"] = {"error": {"lower": 1}}
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
    if a.aggregates == "minimal":
        aggregates = [aggregates[0], aggregates[2]]
    elif a.aggregates == "none":
        aggregates = []

    text = "{} of {} ports connected; {} in alarm set".format(connected, counted, alarm_count)
    if len(charted) != len(all_ports):
        text += " [charting {} of {} ports: {} to {}]".format(
            len(charted), len(all_ports), charted[0]["portId"], charted[-1]["portId"])
    if down_alarm:
        text += " | DOWN: " + "; ".join(down_alarm)
    elif alarm_count == 0:
        text += " | no alarm ports (tag 'prtg-alarm' in Dashboard, or --alert-mode)"
    status = "error" if down_alarm else "ok"
    emit(aggregates + per_port, text, status=status)


if __name__ == "__main__":
    main()
