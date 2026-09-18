"""Shared output helpers for the Meraki PRTG Script v2 sensors.

Emits the PRTG Script v2 JSON result schema (version 3):

    {"version": 3, "status": "ok|warning|error", "message": "...",
     "channels": [{"id": >=10, "name": "...", "type": "float|integer",
                   "kind": "percent|time_milliseconds|count|custom",
                   "display_unit": "...",          # required for kind "custom"
                   "value": ...,
                   "limits": {"error": {"upper": X, "lower": Y},
                              "warning": {"upper": .., "lower": ..}}}]}

Two things the Script v2 runtime does that the classic EXE/Script sensor does
not:

- Parameters arrive on stdin, not as argv. Use read_args().
- Channel ids must be >= 10 (ids 0-9 are reserved).

This file is not a sensor. Keep it in the same directory as the sensor scripts;
they import it.
"""
import os
import sys
import json
import shlex

SCHEMA_VERSION = 3  # Script v2 result schema (PRTG 25.3.112+); use 2 on older


def read_args(parser):
    """Parse args from stdin when run by the probe (Script v2 delivers the
    Parameters field on stdin, not as argv), and from argv when run by hand.
    Build `parser` with exit_on_error=False so a bad parse raises instead of
    writing to stderr."""
    import argparse
    try:
        if sys.stdin.isatty():
            return parser.parse_args()
        return parser.parse_args(shlex.split(sys.stdin.read().rstrip()))
    except (argparse.ArgumentError, SystemExit):
        fail("Could not parse parameters (Script v2 delivers them on stdin; "
             "check the Parameters field).")


def emit(channels, message, status="ok", version=SCHEMA_VERSION):
    print(json.dumps({"version": version, "status": status,
                      "message": str(message)[:2000], "channels": channels}))


def fail(message, version=SCHEMA_VERSION):
    print(json.dumps({"version": version, "status": "error",
                      "message": str(message)[:2000]}))
    sys.exit(0)


def chan(cid, name, value, ctype="float", kind="custom", display_unit=None,
         err_upper=None, err_lower=None, warn_upper=None, warn_lower=None):
    """Build one Script v2 channel dict. cid must be >= 10."""
    c = {"id": cid, "name": name, "type": ctype, "kind": kind,
         "value": int(value) if ctype == "integer" else value}
    if display_unit is not None:
        c["display_unit"] = display_unit
    limits = {}
    if err_upper is not None or err_lower is not None:
        limits["error"] = {}
        if err_upper is not None:
            limits["error"]["upper"] = err_upper
        if err_lower is not None:
            limits["error"]["lower"] = err_lower
    if warn_upper is not None or warn_lower is not None:
        limits["warning"] = {}
        if warn_upper is not None:
            limits["warning"]["upper"] = warn_upper
        if warn_lower is not None:
            limits["warning"]["lower"] = warn_lower
    if limits:
        c["limits"] = limits
    return c


def clean(value):
    return (value or "").strip().strip('"').strip("'").strip()
