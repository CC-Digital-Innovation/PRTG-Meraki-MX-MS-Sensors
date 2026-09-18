# PRTG-Meraki-MX-MS-Sensors

Script v2 sensors for monitoring Cisco Meraki MX appliances and MS switches in PRTG Network Monitor. Appliance utilization, WAN uplink health, WAN traffic and switch port status, read from the Meraki Dashboard API -- no SNMP, no third-party Python modules.

PRTG passes the Parameters field to the script on stdin; the script makes one or two API calls and returns a Script v2 JSON result with named channels and their limits. Thresholds ship as channel limits, so a sensor alerts from its first scan.

## Sensors

| Script | Reports | Channels |
|---|---|---|
| `meraki_device_utilization.py` | Appliance utilization (perfScore) for one MX or VMX | Device Utilization (%) |
| `meraki_wan_status.py` | Uplink health for one MX uplink | Loss Percent, Latency, Jitter, Uplink Failed |
| `meraki_wan_traffic.py` | Throughput for both uplinks of an MX | Traffic In/Out, Peak In/Out 60m |
| `meraki_port_status.py` | Per-port state for an MS switch | `NN: <label>` per port, plus aggregates |

`prtg_out.py` is a shared helper, not a sensor. Keep it beside the scripts; they import it.

**Utilization** is the number the Dashboard shows under Organization > Summary report > (an `-appliance` network) > Utilization, which Meraki publishes as the signal to size an appliance up. Two states return no score and are reported rather than failed: HTTP 204 for a dormant appliance, and HTTP 400 `Feature not supported` for the passive unit of a warm-spare pair, which is normal. Neither emits a channel value -- reporting 0% would read as a healthy, idle appliance.

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

The `id=` matters. Without it the endpoint returns a shorter legacy catalogue that omits the newer sensor types, which reads like an unsupported core rather than a malformed question.

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

| Script | Parameters |
|---|---|
| `meraki_device_utilization.py` | `--serial`, `--util-warn` (75), `--util-error` (90), `--idle-status` (`ok`\|`warning`) |
| `meraki_wan_status.py` | `--serial`, `--uplink` (`wan1`), `--ip` (8.8.8.8), `--org-id`, `--loss-warn` (2), `--loss-error` (3), `--lat-warn` (150), `--lat-error` (300) |
| `meraki_wan_traffic.py` | `--network-id`, `--floor-out-wan2` (3.0) |
| `meraki_port_status.py` | `--serial`, `--alert-mode` (`active`), `--lookback-hours` (24), `--alert-tag`, `--alert-labels`, `--alert-ports` |

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

It walks organization -> networks -> devices, matches each Meraki device to an existing PRTG device by IP or exact name, and plans Device Utilization + WAN 1/2 Status + WAN Traffic on each MX or VMX, and Port Status on each MS. Devices with no PRTG match are reported, not created. Sensors whose name already exists on the device are skipped.

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
| `--sensors` | Comma-separated subset of `device_utilization,wan_status,wan_traffic,port_status` (default all) |
| `--only-serials` / `--skip-serials` | Scope to specific appliances: comma-separated, or `@path` to a file of serials |
| `--key-placeholder N` | Which Script Sensors slot holds the key (default 1) |
| `--interval` | Scan interval, set after creation (default `300\|5 minutes`) |
| `--util-warn` / `--util-error` | Utilization limits at creation time |
| `--sensor-type` | Override the sensor-type token, normally read from the core |

`--only-serials` matters when part of a fleet is already monitored: name-based deduplication will not catch a sensor created under a different naming convention.

## Notes

- The Meraki API allows 10 requests per second per organization. Every script retries HTTP 429 honouring `Retry-After`; `--splay` keeps a large fleet from bursting.
- The API key needs read access only.
- Channel ids start at 10; 0-9 are reserved by the Script v2 runtime.

## Related projects

- [PRTG-Meraki-MT-Sensors](https://github.com/CC-Digital-Innovation/PRTG-Meraki-MT-Sensors) -- companion sensors for Meraki MT environmental devices.

## License

MIT

## Credits

Original Meraki sensor by Onecimo Arce. Maintained and extended by Richard Travellin.
