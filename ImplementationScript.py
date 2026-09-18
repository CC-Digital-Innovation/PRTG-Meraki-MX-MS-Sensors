#!/usr/bin/env python3
"""Bulk-create the Meraki MX/MS PRTG sensors across a Meraki organization.

Walks a Meraki organization interactively (organization -> networks -> devices),
matches each Meraki device to an existing PRTG device by name or IP, and creates
the Script v2 sensors on the matching PRTG device:

  MX appliance -> Device Utilization, WAN 1 Status, WAN 2 Status, WAN Traffic
  MS switch    -> Port Status

Use --sensors to create one kind on its own, and --only-serials / --skip-serials
to scope a run to specific appliances -- for example when rolling Device
Utilization out to the MXs that do not have it yet, without touching the ones
that are already monitored.

Sensors are created with the Meraki API key referenced as a placeholder, not
embedded. Set the key once under the target device or a parent group, in
"Credentials for Script Sensors" -> Placeholder 1, so the created sensors
inherit it. Override the slot with --key-placeholder if 1 is already taken.

Prerequisites:
  - The sensor scripts are installed on each target probe (see install.ps1).
  - A Meraki Dashboard API key with read access.
  - A PRTG username and passhash (Setup > Account Settings, or the
    /api/getpasshash.htm endpoint).
  - A probe offering Script v2 (a standard Windows probe does), with Python 3
    on the PATH of the probe service account. The sensor-type token is read
    from the core, so no Script v2 sensor needs to exist yet; --sensor-type
    overrides it.

This talks to the PRTG and Meraki HTTP APIs directly; PrtgAPI is not required.

Usage:
  python3 ImplementationScript.py --prtg-server https://prtg.example.com \\
      --prtg-user admin --prtg-passhash 1234567890 --api-key <meraki-key>

  Just the utilization sensor, only on the appliances listed in a file:

  python3 ImplementationScript.py --prtg-server https://prtg.example.com \\
      --prtg-user admin --prtg-passhash 1234567890 --api-key <meraki-key> \\
      --sensors device_utilization --only-serials @serials.txt --dry-run

  --dry-run shows the plan and creates nothing.
"""
import os
import re
import ssl
import sys
import json
import time
import html
import argparse
import getpass
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from datetime import datetime

MERAKI_BASE = "https://api.meraki.com/api/v1"
SCRIPTS = {
    "wan_status": "meraki_wan_status.py",
    "wan_traffic": "meraki_wan_traffic.py",
    "port_status": "meraki_port_status.py",
    "device_utilization": "meraki_device_utilization.py",
}
ALL_SENSORS = list(SCRIPTS)

# Meraki security appliances are MX* hardware and VMX* virtual appliances; both
# serve the appliance sensors. A bare "MX" prefix test silently skips every VMX.
IS_APPLIANCE = re.compile(r'^V?MX', re.I).match

_logf = None


def log(msg):
    line = "{}: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    if _logf:
        _logf.write(line + "\n")
        _logf.flush()


# ---------------------------------------------------------------- Meraki
def meraki_get(key, path, tries=5):
    """GET a Meraki endpoint, following Link pagination. Returns a list or dict.

    Retries on 429 honouring Retry-After. A full walk of a large organization
    is one call per network, which runs into the org rate limit on its own.
    """
    url = MERAKI_BASE + path
    out = None
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + key, "Accept": "application/json"})
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                    link = r.headers.get("Link", "")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < tries - 1:
                    delay = min(float(e.headers.get("Retry-After") or 2), 15)
                    log("rate limited on {}, retrying in {:.0f}s".format(path, delay))
                    time.sleep(delay)
                    continue
                raise SystemExit("Meraki API HTTP {} on {}: {}".format(
                    e.code, path, e.read().decode("utf-8", "ignore")[:200]))
        if not isinstance(data, list):
            return data
        out = (out or []) + data
        m = re.search(r'<([^>]+)>;\s*rel=next', link)
        url = m.group(1) if m else None
    return out or []


# ---------------------------------------------------------------- PRTG
class Prtg:
    def __init__(self, server, user, passhash):
        self.base = server.rstrip("/")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ctx))
        self._auth = "username={}&passhash={}".format(
            urllib.parse.quote(user), urllib.parse.quote(passhash))
        self._open("{}/public/checklogin.htm?{}&loginurl=%2Fwelcome.htm".format(
            self.base, self._auth))

    def _open(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        with self.op.open(req, timeout=40) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    def table(self, content, columns, extra="", count=50000):
        _, body = self._open("{}/api/table.json?content={}&columns={}&count={}&{}{}".format(
            self.base, content, columns, count, self._auth, extra))
        try:
            return json.loads(body).get(content, [])
        except ValueError:
            return []

    def setprop(self, objid, name, value):
        self._open("{}/api/setobjectproperty.htm?id={}&name={}&value={}&{}".format(
            self.base, objid, urllib.parse.quote(name),
            urllib.parse.quote(str(value)), self._auth))

    def sensortypes(self, device_id):
        """Sensor types the core offers on one device.

        sensortypes.json must be asked about a specific device. Called without
        an id it answers with a shorter legacy catalogue that leaves out the
        newer sensors, Script v2 among them -- so a bare call looks exactly
        like a core that cannot run these sensors at all.
        """
        _, body = self._open("{}/api/sensortypes.json?id={}&{}".format(
            self.base, device_id, self._auth))
        try:
            return json.loads(body).get("sensortypes", [])
        except ValueError:
            return []

    def discover_sensor_type(self, device_id, override=None):
        """Resolve the Script v2 sensor-type token.

        Asks the core what it offers on device_id, so this works on a core
        where no Script v2 sensor has ever been created. Falls back to reading
        the token off an existing sensor.
        """
        if override:
            return override
        for t in self.sensortypes(device_id):
            if (t.get("id") or "").startswith("paessler.exe.exe_sensor"):
                return t["id"]
        for r in self.table("sensors", "objid,type", extra="&filter_type=@sub(exe_sensor)", count=50):
            t = r.get("type_raw") or ""
            if t.startswith("paessler.exe.exe_sensor"):
                return t
        return None

    def sensor_ids(self, device_id):
        """Set of sensor ids currently on a device."""
        return {str(s.get("objid")) for s in self.table(
            "sensors", "objid", extra="&id={}".format(device_id), count=5000)}

    def create_scriptv2(self, device_id, name, script, params, stype, tag):
        before = self.sensor_ids(device_id)
        u, _ = self._open("{}/controls/addsensor3.htm?id={}&sensortype={}".format(
            self.base, device_id, stype))
        m = re.search(r'tmpid=(\d+)', u)
        if not m:
            return None, "no tmpid from addsensor3"
        tmpid = m.group(1)
        body = ""
        for _ in range(15):
            time.sleep(2)
            _, body = self._open("{}/addsensor4.htm?id={}&tmpid={}".format(
                self.base, device_id, tmpid))
            if script in body:
                break
        if script not in body:
            return None, "metascan did not list {} (scripts installed on this probe?)".format(script)
        f = parse_form_fields(body)
        mval = None
        for mm in re.finditer(
                r'name="metascan__check"[^>]*value="([^"]*' + re.escape(script) + r'[^"]*)"', body):
            mval = html.unescape(mm.group(1))
        if not mval:
            return None, "metascan__check value not found for {}".format(script)
        f["metascan__check"] = mval
        f["paessler-exe-exe_metascan_section-exe_metascan_group-exe_name"] = mval
        f["paessler-exe-exe_metascan_section-exe_metascan_group-exe_type"] = "Python"
        f["name_"] = name
        f["paessler-exe-exe_section-exe_group-parameters_"] = params
        f["paessler-exe-exe_section-exe_group-timeout_"] = "60"
        f["id"] = str(device_id)
        f["tmpid"] = str(tmpid)
        tags = f.get("tags_", "exesensor")
        if tag and tag not in tags:
            f["tags_"] = (tags + " " + tag).strip()
        f["sensortype"] = stype
        u2, body2 = self._post_multipart("{}/addsensor5.htm".format(self.base), f)
        if "Step 2 of 2" in body2 or u2.rsplit("/", 1)[-1].startswith("addsensor"):
            errs = re.findall(
                r'(Please select a value|This field is required|'
                r'no sensors could be created|session expired)', body2)
            return None, "not created: {}".format(sorted(set(errs)) or u2)
        # Never scrape the id out of the redirect: on success it can land on the
        # device page, and naming that id renames the DEVICE. Diff the device's
        # sensors instead.
        #
        # The diff has to be retried. table.json can serve a cached tree that
        # does not yet list a sensor the core has just created, so a single read
        # reports "gained no sensors" for a sensor that exists and is already
        # scanning. Believing that leaves the sensor behind under the wizard's
        # default name, which later re-runs do not match and so duplicate.
        added = set()
        for attempt in range(6):
            added = self.sensor_ids(device_id) - before
            if added:
                break
            time.sleep(3)
        if len(added) != 1:
            return None, ("created, but could not identify the new sensor after "
                          "{} reads (device {} gained {}); it is probably there "
                          "under the default name 'Script v2: <script>' -- find it "
                          "and rename it rather than re-running, which would "
                          "duplicate it".format(attempt + 1, device_id,
                                                "{} sensors".format(len(added)) if added else "none"))
        nid = added.pop()
        self.setprop(nid, "name", name)  # name_ in the wizard does not stick
        return nid, u2

    def _post_multipart(self, url, fields):
        boundary = "----prtgform7c9a2b"
        lines = []
        for k, v in fields.items():
            lines += ["--" + boundary,
                      'Content-Disposition: form-data; name="{}"'.format(k), "", str(v)]
        lines += ["--" + boundary + "--", ""]
        return self._open(url, data="\r\n".join(lines).encode("utf-8"),
                          headers={"Content-Type": "multipart/form-data; boundary=" + boundary})


def parse_form_fields(htmltext):
    """Return {name: value} for every input/select/textarea in the add-sensor form."""
    fields = {}
    for m in re.finditer(r'<input\b[^>]*>', htmltext):
        tag = m.group(0)
        n = re.search(r'name="([^"]+)"', tag)
        if not n:
            continue
        typ = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        val = (re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
        if typ in ("checkbox", "radio"):
            if "checked" in tag.lower():
                fields[n.group(1)] = html.unescape(val)
        else:
            fields[n.group(1)] = html.unescape(val)
    for m in re.finditer(r'<select\b[^>]*name="([^"]+)"[^>]*>(.*?)</select>', htmltext, re.S):
        sel = re.search(r'<option[^>]*\bselected\b[^>]*value="([^"]*)"', m.group(2)) or \
              re.search(r'<option[^>]*value="([^"]*)"[^>]*\bselected\b', m.group(2))
        if sel:
            fields[m.group(1)] = html.unescape(sel.group(1))
    for m in re.finditer(r'<textarea\b[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', htmltext, re.S):
        fields[m.group(1)] = html.unescape(m.group(2))
    return fields


# ---------------------------------------------------------------- helpers
def choose(items, label, prompt, multi=False):
    print("\n" + prompt)
    for i, it in enumerate(items):
        print("  [{}] {}".format(i, label(it)))
    if multi:
        print("  [A] all")
    while True:
        raw = input("Selection: ").strip()
        if multi and raw.lower() == "a":
            return list(items)
        try:
            idx = [int(x) for x in raw.replace(",", " ").split()]
            if multi and idx and all(0 <= i < len(items) for i in idx):
                return [items[i] for i in idx]
            if not multi and len(idx) == 1 and 0 <= idx[0] < len(items):
                return items[idx[0]]
        except ValueError:
            pass
        print("  invalid, try again")


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())


def serial_set(spec):
    """Parse a --only-serials / --skip-serials value into a set of serials.

    Accepts a comma-separated list, or @path to a file with one serial per
    line (blank lines and # comments ignored). Returns None when spec is
    empty, which the callers read as "no restriction".
    """
    if not spec:
        return None
    if spec.startswith("@"):
        path = spec[1:]
        if not os.path.exists(path):
            raise SystemExit("serial list not found: {}".format(path))
        with open(path) as fh:
            raw = [ln.split("#", 1)[0] for ln in fh]
    else:
        raw = spec.split(",")
    out = {s.strip().upper() for s in raw if s.strip()}
    if not out:
        raise SystemExit("serial list {!r} resolved to no serials".format(spec))
    return out


def match_prtg_device(mdev, by_host, by_name):
    """Match a Meraki device to an existing PRTG device by IP (the PRTG device
    host), falling back to an exact device-name match."""
    for f in ("lanIp", "wan1Ip", "wan2Ip", "mgmtIp"):
        ip = mdev.get(f)
        if ip and ip in by_host:
            return by_host[ip]
    n = norm(mdev.get("name"))
    if n and n in by_name:
        return by_name[n]
    return None


def main():
    p = argparse.ArgumentParser(description="Bulk-create Meraki MX/MS sensors in PRTG.")
    p.add_argument("--prtg-server", required=True, help="e.g. https://prtg.example.com")
    p.add_argument("--prtg-user", required=True)
    p.add_argument("--prtg-passhash", help="PRTG passhash (prompted if omitted)")
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY"),
                   help="Meraki API key (or MERAKI_API_KEY env; prompted if omitted)")
    p.add_argument("--org-id", help="Meraki organization id (prompted if omitted)")
    p.add_argument("--uplink-ip", default="1.1.1.1",
                   help="MX uplink monitoring target for the WAN status sensors (default 1.1.1.1)")
    p.add_argument("--floor-out-wan2", default="3.0",
                   help="WAN2 out-peak floor in Mbit/s for the traffic sensor (default 3.0)")
    p.add_argument("--splay", default="5")
    p.add_argument("--interval", default="300|5 minutes",
                   help="scan interval, set after creation (default '300|5 minutes'). "
                        "Utilization moves slowly; the 60 s wizard default is five times "
                        "the Meraki API calls for no extra signal. Empty to leave as created.")
    p.add_argument("--key-placeholder", default="1", choices=["1", "2", "3", "4", "5"],
                   help="which 'Credentials for Script Sensors' placeholder slot holds the "
                        "Meraki API key (default 1, referenced as %%scriptplaceholder1)")
    p.add_argument("--util-warn", default="75",
                   help="MX utilization warning limit in percent (default 75)")
    p.add_argument("--util-error", default="90",
                   help="MX utilization error limit in percent (default 90)")
    p.add_argument("--sensors", default="all",
                   help="comma-separated subset of {} (default all). Use this to roll "
                        "one sensor kind out on its own.".format(",".join(ALL_SENSORS)))
    p.add_argument("--only-serials",
                   help="restrict to these Meraki serials: comma-separated, or @path "
                        "to a file with one serial per line. Everything else is skipped.")
    p.add_argument("--skip-serials",
                   help="exclude these Meraki serials (same forms as --only-serials). "
                        "Use it to leave appliances that are already monitored alone.")
    p.add_argument("--tag", default="meraki-api", help="tag applied to every created sensor")
    p.add_argument("--sensor-type", help="override the Script v2 sensor-type token")
    p.add_argument("--dry-run", action="store_true", help="show the plan, create nothing")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a = p.parse_args()

    global _logf
    _logf = open("ImplementationScript_{}.log".format(
        datetime.now().strftime("%Y%m%d_%H%M%S")), "a")

    key = a.api_key or getpass.getpass("Meraki API key: ")
    passhash = a.prtg_passhash or getpass.getpass("PRTG passhash: ")

    log("connecting to PRTG at {}".format(a.prtg_server))
    prtg = Prtg(a.prtg_server, a.prtg_user, passhash)
    prtg_devices = prtg.table("devices", "objid,device,host")
    by_host = {d.get("host"): d for d in prtg_devices if d.get("host")}
    by_name = {norm(d.get("device")): d for d in prtg_devices}
    log("PRTG has {} devices".format(len(prtg_devices)))

    if not prtg_devices:
        raise SystemExit("PRTG returned no devices; check the account's permissions.")
    probe_device = prtg_devices[0]["objid"]
    stype = prtg.discover_sensor_type(probe_device, a.sensor_type)
    if not stype:
        raise SystemExit(
            "This core does not offer Script v2 on device {}. Check that the probe "
            "supports it and that Python 3 is installed for the probe service account, "
            "or pass --sensor-type.".format(probe_device))
    log("Script v2 sensor type = {}".format(stype))

    if a.org_id:
        org = {"id": a.org_id, "name": a.org_id}
    else:
        org = choose(meraki_get(key, "/organizations"),
                     lambda o: "{} (id {})".format(o["name"], o["id"]),
                     "Meraki organizations:")
    log("organization {}".format(org.get("name")))

    nets = meraki_get(key, "/organizations/{}/networks?perPage=1000".format(
        urllib.parse.quote(str(org["id"]))))
    nets = [n for n in nets
            if {"appliance", "switch"} & set(n.get("productTypes", []))]
    if not nets:
        raise SystemExit("No appliance/switch networks found in this organization.")
    chosen = choose(nets, lambda n: n["name"],
                    "Networks (comma-separated, or A for all):", multi=True)

    want = set(ALL_SENSORS) if a.sensors.strip().lower() == "all" else \
        {s.strip() for s in a.sensors.split(",") if s.strip()}
    unknown = want - set(ALL_SENSORS)
    if unknown:
        raise SystemExit("unknown --sensors value(s): {} (choose from {})".format(
            ", ".join(sorted(unknown)), ", ".join(ALL_SENSORS)))
    only = serial_set(a.only_serials)
    skip = serial_set(a.skip_serials) or set()
    keyref = "%scriptplaceholder{}".format(a.key_placeholder)
    log("sensor kinds: {}".format(", ".join(sorted(want))))
    log("API key referenced as {} (Credentials for Script Sensors)".format(keyref))
    if only:
        log("restricted to {} serial(s) from --only-serials".format(len(only)))
    if skip:
        log("excluding {} serial(s) from --skip-serials".format(len(skip)))

    # /networks/{id}/devices omits some address fields the org-wide list carries
    # (a VMX reports only lanIp, and that key is absent from the network call),
    # so merge the org view in by serial before matching on IP.
    org_addr = {}
    for d in meraki_get(key, "/organizations/{}/devices?perPage=1000".format(
            urllib.parse.quote(str(org["id"])))):
        addr = {f: d.get(f) for f in ("lanIp", "wan1Ip", "wan2Ip", "mgmtIp") if d.get(f)}
        if addr:
            org_addr[d.get("serial")] = addr
    log("org device list: {} appliances/switches carry an address".format(len(org_addr)))

    plan, unmatched, filtered = [], [], 0
    for net in chosen:
        for d in meraki_get(key, "/networks/{}/devices".format(urllib.parse.quote(net["id"]))):
            for f, v in org_addr.get(d.get("serial"), {}).items():
                if not d.get(f):
                    d[f] = v
            model = (d.get("model") or "").upper()
            serial, dname = d.get("serial"), (d.get("name") or d.get("serial"))
            su = (serial or "").upper()
            if (only is not None and su not in only) or su in skip:
                filtered += 1
                continue
            pd = match_prtg_device(d, by_host, by_name)
            if IS_APPLIANCE(model):
                if not want & {"device_utilization", "wan_status", "wan_traffic"}:
                    continue
                if not pd:
                    unmatched.append((net["name"], d))
                    continue
                if "device_utilization" in want:
                    plan.append((pd, "{} - Device Utilization - {}".format(net["name"], dname),
                                 SCRIPTS["device_utilization"],
                                 "--serial {} --api-key {} --util-warn {} "
                                 "--util-error {} --splay {}".format(
                                     serial, keyref, a.util_warn, a.util_error, a.splay)))
                if "wan_status" in want:
                    for uplink, label in (("wan1", "WAN 1 Status"), ("wan2", "WAN 2 Status")):
                        plan.append((pd, "{} - {} - {}".format(net["name"], label, dname),
                                     SCRIPTS["wan_status"],
                                     "--serial {} --uplink {} --ip {} --api-key {} "
                                     "--org-id {} --splay {}".format(
                                         serial, uplink, a.uplink_ip, keyref, org["id"], a.splay)))
                if "wan_traffic" in want:
                    plan.append((pd, "{} - WAN Traffic - {}".format(net["name"], dname),
                                 SCRIPTS["wan_traffic"],
                                 "--network-id {} --api-key {} --floor-out-wan2 {} "
                                 "--splay {}".format(net["id"], keyref, a.floor_out_wan2, a.splay)))
            elif model.startswith("MS"):
                if "port_status" not in want:
                    continue
                if not pd:
                    unmatched.append((net["name"], d))
                    continue
                plan.append((pd, "{} - Port Status - {}".format(net["name"], dname),
                             SCRIPTS["port_status"],
                             "--serial {} --api-key {} --alert-mode active "
                             "--splay {}".format(serial, keyref, a.splay)))

    existing = {(str(s.get("parentid")), s.get("sensor"))
                for s in prtg.table("sensors", "objid,sensor,parentid")}

    todo = []
    print("\nPlanned sensors:")
    for pd, name, script, params in plan:
        if (str(pd["objid"]), name) in existing:
            log("skip (already exists): {}".format(name))
            continue
        todo.append((pd, name, script, params))
        print("  {} -> PRTG device '{}' (id {})".format(name, pd.get("device"), pd["objid"]))
        if a.dry_run:
            print("      {}  {}".format(script, params))

    if unmatched:
        print("\nMeraki devices with no matching PRTG device ({}):".format(len(unmatched)))
        for netname, d in unmatched:
            print("  {} / {} ({} {})".format(netname, d.get("name"), d.get("model"), d.get("serial")))

    if a.dry_run:
        log("dry run: {} would be created, {} unmatched, {} filtered by serial".format(
            len(todo), len(unmatched), filtered))
        return
    if not todo:
        log("nothing to create")
        return
    if not a.yes and input("\nCreate {} sensors? [y/N] ".format(len(todo))).strip().lower() != "y":
        log("aborted by user")
        return

    created = failed = 0
    for pd, name, script, params in todo:
        sid, info = prtg.create_scriptv2(pd["objid"], name, script, params, stype, a.tag)
        if sid:
            created += 1
            if a.interval:
                prtg.setprop(sid, "interval", a.interval)
            log("created id={} {}".format(sid, name))
        else:
            failed += 1
            log("FAILED {} : {}".format(name, info))
    log("done: {} created, {} failed, {} already existed, {} filtered by serial".format(
        created, failed, len(plan) - len(todo), filtered))


if __name__ == "__main__":
    main()
