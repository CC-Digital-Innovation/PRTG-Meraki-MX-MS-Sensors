# PRTG-Meraki-MX-MS-Sensors

Script v2 sensors for monitoring Cisco Meraki MX appliances, MS switches and MR access points in PRTG Network Monitor. Appliance utilization, WAN uplink health and traffic, switch port status, and access point radio, channel utilization and uplink health, read from the Meraki Dashboard API -- no SNMP, no third-party Python modules.

PRTG passes the Parameters field to the script on stdin; the script makes one or two API calls and returns a Script v2 JSON result with named channels and their limits. Thresholds ship as channel limits, so a sensor alerts from its first scan.

## Sensors

| Script | Reports | Channels |
|---|---|---|
| `meraki_device_utilization.py` | Appliance utilization (perfScore) for one MX or VMX | Device Utilization (%) |
| `meraki_wan_status.py` | Uplink health for one MX uplink | Loss Percent, Latency, Jitter, Uplink Failed |
| `meraki_wan_traffic.py` | Throughput for both uplinks of an MX | Traffic In/Out, Peak In/Out 60m |
| `meraki_port_status.py` | Per-port state for an MS switch | `NN: <label>` per port, plus aggregates |
| `meraki_mx_wan.py` | All uplinks of one MX, from the shared cache | Appliance Reporting, Uplinks In Service; per uplink: Uplink Failed, Loss, Latency, Traffic In/Out |
| `meraki_ap_health.py` | One MR access point, from the shared cache | Device Online; per band: Radio Broadcasting, Channel, Channel / Wi-Fi / Non-Wi-Fi Utilization, Transmit Power; Ethernet Speed, Full Duplex, Full Power, Packet Loss Down/Up |

`prtg_out.py` and `meraki_cache.py` are shared helpers, not sensors. Keep them beside the scripts; the sensors import them.

**The shared cache.** The Dashboard API allows 10 requests per second per organization, shared by every key and every application using it. A sensor per access point calling per-device endpoints does not fit in that. `meraki_mx_wan.py` and `meraki_ap_health.py` read organization-wide endpoints instead, through `meraki_cache.py`: the first sensor to need a dataset fetches it and writes it to a cache directory on the probe, and every other sensor reads that copy until it is `--cache-ttl` seconds old (default 240). A lock file stops two sensors fetching the same dataset at once, and a sensor that starts during a fetch waits for it. If the API fails or rate-limits, a copy up to `--max-stale` seconds old (default 900) is used and the sensor message gives its age. In testing, 20 AP sensors started together made 6 API calls between them, and one appliance sensor per MX across 106 appliances made 3.

The cache lives in `MERAKI_CACHE_DIR` if set, else `<temp>\prtg-meraki-cache` -- `C:\Windows\Temp\prtg-meraki-cache` for a probe running as LocalSystem. Set `MERAKI_CACHE_LOG` to a file path to log every real API request, which is how to check what the cache is saving.

**MX WAN** reports every uplink that is in service, or that you list in `--uplinks`. List the uplinks that should be up, so one that is unplugged alarms rather than dropping out of the sensor. Meraki keeps the last known uplink state for an appliance that stops checking in, so a dead appliance can still read "active"; **Appliance Reporting** catches that from the last check-in time (`--offline-after`, default 900 s). An appliance often monitors several loss/latency targets, and some never answer -- a provider gateway that drops ICMP, an IPv6 target on an IPv4 uplink -- so the sensor uses `--ip` (default 8.8.8.8) when it is monitored, otherwise the target with the lowest loss. Traffic is a 5-minute average.

**AP health** reports each band the AP is broadcasting on, or that you list in `--bands`; listing the expected bands makes a radio that stops broadcasting alarm instead of disappearing. Channel utilization carries warning/error limits (`--util-warn` 60, `--util-error` 80); Non-Wi-Fi utilization is interference. Ethernet Speed warns below `--min-speed` (1000 Mbit/s) -- an AP that has negotiated 100 Mbit/s is a cabling or switch-port fault. Full Power warns when the AP is in low-power mode, which limits its radios. An AP that is offline or dormant reports Device Online 0 and nothing else.

**Utilization** is the number the Dashboard shows under Organization > Summary report > (an `-appliance` network) > Utilization, which Meraki publishes as the signal to size an appliance up. Two states return no score and are reported rather than failed: HTTP 204 for a dormant appliance, and HTTP 400 `Feature not supported` for the passive unit of a warm-spare pair, which is normal. Neither emits a channel value -- reporting 0% would read as a healthy, idle appliance. Meraki serves the score only per device, so on an organization near its API rate limit a slow or rejected call is routine; the call goes through `meraki_cache.py` keyed per appliance, and a failed call reports the last good score (up to `--max-stale` seconds old, with its age in the message) instead of failing the scan. `ImplementationScript.py` creates these sensors at a 10-minute interval (`--util-interval`): the score moves slowly, and it halves their share of the organization's API budget.

**WAN traffic** carries a lower error limit on the WAN2 out-peak (`--floor-out-wan2`, default 3.0 Mbit/s) to flag an uplink that has stopped carrying traffic. Set `0` to disable.

**Port status** alarms only on ports in the alarm set, chosen by `--alert-mode`: `active` (up now, or passed traffic within `--lookback-hours`), `all-labeled`, or `selective` (`--alert-tag`, `--alert-labels` globs, `--alert-ports`).

## Requirements

- A PRTG probe offering Script v2. A standard Windows remote probe does; scripts live in `Custom Sensors\scripts`.
- Python 3 on the probe host, on the PATH of the probe service account.
- A Meraki Dashboard API key with read access, and the organization ID.

To confirm a core offers Script v2, ask it **about a specific device**:

```bash
curl -sk "https://<prtg>/api/sensortypes.json?id=<device-id>&username=<user>&passhash=<hash>" \
  | grep -o '"id": "paessler.exe.exe_sensor"'
```

The `id=` matters. Without it the endpoint returns a shorter legacy catalog that omits the newer sensor types, which reads like an unsupported core rather than a malformed question.

## Install

On the probe host, in an elevated PowerShell:

```powershell
irm https://raw.githubusercontent.com/CC-Digital-Innovation/PRTG-Meraki-MX-MS-Sensors/main/install.ps1 | iex
```

Set `$env:PRTG_CUSTOM_SENSORS` first to install into a different Custom Sensors location. By hand: copy `prtg_out.py` and the sensor scripts you need into `Custom Sensors\scripts`.

## API key

Put the key in **Credentials for Script Sensors -> Placeholder 1** on the device or a parent group, and reference it as `%scriptplaceholder1` in the Parameters field. There are five slots. The core substitutes the value at run time and stores it write-only -- the API reads it back as `***`.

Never put the key in the Parameters field directly; anyone who can read the sensor's settings or the sensor table over the API recovers it in clear text.

`MERAKI_API_KEY` in the probe process environment is a fallback, useful for testing a script by hand.

## Parameters

Common to every script: `--api-key` (or `MERAKI_API_KEY`) and `--splay N`, a random 0..N second start delay that de-aligns bursts when many sensors share a scan interval.

The splay is slept inside the script, so it is spent against the sensor's own timeout. `ImplementationScript.py` sets the timeout to `splay + 60` for this reason. Setting a large splay by hand on a sensor left at the 60 s default makes every scan a lottery -- the runs that draw a high delay are killed and report no data, which looks like a broken sensor rather than a short budget.

| Script | Parameters |
|---|---|
| `meraki_device_utilization.py` | `--serial`, `--util-warn` (75), `--util-error` (90), `--idle-status` (`ok`\|`warning`), `--max-stale` (1500) |
| `meraki_wan_status.py` | `--serial`, `--uplink` (`wan1`), `--ip` (8.8.8.8), `--org-id`, `--loss-warn` (2), `--loss-error` (3), `--lat-warn` (150), `--lat-error` (300) |
| `meraki_wan_traffic.py` | `--network-id`, `--floor-out-wan2` (3.0) |
| `meraki_port_status.py` | `--serial`, `--alert-mode` (`active`), `--lookback-hours` (24), `--alert-tag`, `--alert-labels`, `--alert-ports` |
| `meraki_mx_wan.py` | `--serial`, `--org-id`, `--uplinks` (e.g. `wan1,wan2`), `--ip` (8.8.8.8), `--loss-warn` (2), `--loss-error` (5), `--lat-warn` (150), `--lat-error` (300), `--offline-after` (900), `--cache-ttl` (240), `--max-stale` (900) |
| `meraki_ap_health.py` | `--serial`, `--org-id`, `--bands` (e.g. `2.4,5`), `--util-warn` (60), `--util-error` (80), `--min-speed` (1000), `--loss-warn`, `--loss-error`, `--offline-status` (`error`\|`warning`), `--cache-ttl` (240), `--max-stale` (900) |

`--org-id` on `meraki_wan_status.py` adds the Uplink Failed channel, a connection-loss record separate from packet loss.

In PRTG, a Parameters field looks like:

```
--serial Q2XX-XXXX-XXXX --api-key %scriptplaceholder1 --util-warn 75 --util-error 90 --splay 5
```

Run any script by hand to test -- it accepts the same parameters on the command line:

```bash
python3 meraki_device_utilization.py --serial Q2XX-XXXX-XXXX --api-key <key>
```

PRTG stores a script's channel limits once, when the channels are created on the first scan. Put the final thresholds in the parameters at creation time; changing them later updates the message but not the stored limit.

## Bulk deployment

`ImplementationScript.py` creates sensors across a whole organization. Run it from any machine with Python 3 that can reach both PRTG and the Meraki Dashboard:

```bash
python3 ImplementationScript.py --prtg-server https://prtg.example.com \
    --prtg-user admin --prtg-passhash <passhash> --api-key <meraki-key> --dry-run
```

It walks organization -> networks -> devices, matches each Meraki device to an existing PRTG device by IP or exact name, and plans Device Utilization + WAN 1/2 Status + WAN Traffic on each MX or VMX, and Port Status on each MS. With `--sensors mx_wan,ap_health` it plans MX WAN on each appliance and AP Health on each MR, and passes each one the uplinks / bands that are in service at creation time. Devices with no PRTG match are reported, not created. Sensors whose name already exists on the device are skipped.

```text
2026-01-15 09:14:03: Script v2 sensor type = paessler.exe.exe_sensor
2026-01-15 09:14:31: API key referenced as %scriptplaceholder1 (Credentials for Script Sensors)

Planned sensors:
  HQ - Device Utilization - fw-hq -> PRTG device 'fw-hq' (id 2041)
      meraki_device_utilization.py  --serial Q2XX-XXXX-XXXX --api-key %scriptplaceholder1 --util-warn 75 --util-error 90 --splay 5

Meraki devices with no matching PRTG device (1):
  Store 100 / sw-s100-1 (MS120-8 Q2XX-YYYY-ZZZZ)
2026-01-15 09:14:37: dry run: 9 would be created, 1 unmatched, 0 filtered by serial
```

Drop `--dry-run` and answer `y` to create. Each run writes a timestamped `ImplementationScript_*.log` beside the script.

| Flag | Effect |
|---|---|
| `--sensors` | Comma-separated subset of `device_utilization,wan_status,wan_traffic,port_status,mx_wan,ap_health` (default `all` = the first four; the cache-backed `mx_wan` and `ap_health` are opt-in) |
| `--probe` | Only match PRTG devices on probes whose name contains this text. On a core shared by several customers, private addresses overlap, so matching by IP across the whole core can pick another customer's device |
| `--ap-util-warn` / `--ap-util-error` | AP channel utilization limits at creation time (60 / 80) |
| `--no-notify` | Create sensors with notification-trigger inheritance off, so a rollout can be reviewed before it alerts; turn it back on with `setobjectproperty.htm?name=inherittriggers&value=1` |
| `--pause N` | Seconds between creations (default 0). A core near its limit can hang under back-to-back creation; 30-60 s spreads the load |
| `--only-serials` / `--skip-serials` | Scope to specific appliances: comma-separated, or `@path` to a file of serials |
| `--key-placeholder N` | Which Script Sensors slot holds the key (default 1) |
| `--interval` | Scan interval, set after creation (default `300\|5 minutes`) |
| `--util-interval` | Scan interval for Device Utilization (default `600\|10 minutes`) |
| `--util-warn` / `--util-error` | Utilization limits at creation time |
| `--sensor-type` | Override the sensor-type token, normally read from the core |

Creation is paced by the core itself: before each sensor the script times a one-row read and, above 4 s, backs off in 30 s steps until it is under 2 s; if the core stays slow for 15 minutes it stops cleanly, and a re-run resumes (sensors that exist are skipped by name). A create that errors or times out is logged and the run continues -- a timeout does not mean nothing was created, so at the end the script looks for a sensor that appeared on that device anyway and gives it its proper name.

`--only-serials` matters when part of a fleet is already monitored: name-based deduplication will not catch a sensor created under a different naming convention.

## Notes

- The Meraki API allows 10 requests per second per organization. Every script retries HTTP 429 honoring `Retry-After`; `--splay` keeps a large fleet from bursting.
- The API key needs read access only.
- Channel ids start at 10; 0-9 are reserved by the Script v2 runtime.
- Each channel id comes from the port id, not its position in the response:
  `99 + N` for a numeric port, a hashed id above 1000 for a modular one.
  PRTG keys history to the channel id, so an id that shifts when a module is added
  or removed hands one port's history to another. Ports 1..48 land on 100..147,
  which is what the earlier positional numbering produced, so sensors already
  deployed keep their history and their channel names.
- Meraki says a `portId` is "commonly just the port number" but "may contain
  additional identifying information such as the slot and module-type if the port
  is located on a port module". Modular hardware (MS390, C9300, and MS425 flexible
  stacking) returns ids like `1_MA-MOD-8X10G_1`, which are sorted naturally and
  shown verbatim.

## Related projects

- [PRTG-Meraki-MT-Sensors](https://github.com/CC-Digital-Innovation/PRTG-Meraki-MT-Sensors) -- companion sensors for Meraki MT environmental devices.

## License

MIT

## Credits

Original Meraki sensor by Onecimo Arce. Maintained and extended by Richard Travellin.
