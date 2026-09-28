"""Shared, file-backed cache of organization-wide Meraki API calls.

The Dashboard API allows 10 requests per second per organization, shared by
every key and every application that talks to it. One sensor per access point
calling a per-device endpoint every scan does not fit in that. The
organization-wide endpoints return every device in one call, so the sensors in
this repo that use them read a cached copy instead:

- The first sensor to need a dataset fetches it (following pagination) and
  writes it to the cache directory.
- Sensors that start while that fetch is running wait for it rather than
  issuing the same call again. A lock file stops two processes fetching the
  same dataset at once.
- If the API fails or rate-limits, a cached copy up to --max-stale seconds
  old is returned instead, and the sensor says how old its data is.

Default cache directory: MERAKI_CACHE_DIR if set, else <temp>/prtg-meraki-cache.
The temp directory of the PRTG probe service account (LocalSystem) is
C:\\Windows\\Temp. The cache key includes a hash of the API key, never the key.

This file is not a sensor. Keep it beside the sensor scripts; they import it.
"""
import os
import re
import json
import time
import hashlib
import tempfile
import urllib.request
import urllib.parse
import urllib.error

API_BASE = "https://api.meraki.com/api/v1"
LOCK_STALE = 120  # seconds after which an abandoned lock file is ignored


class MerakiError(Exception):
    pass


def cache_dir():
    d = os.environ.get("MERAKI_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "prtg-meraki-cache")
    os.makedirs(d, exist_ok=True)
    return d


def _get_pages(api_key, path, params, deadline):
    """GET path, following Link rel=next. Returns the combined list; responses
    wrapped as {"items": [...]} are unwrapped. Retries 429 per Retry-After
    until deadline."""
    url = "{}{}?{}".format(API_BASE, path, urllib.parse.urlencode(params, doseq=True))
    out, wrapped = [], False
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer {}".format(api_key), "Accept": "application/json"})
        if os.environ.get("MERAKI_CACHE_LOG"):
            # optional request log, for checking how many calls the cache saves
            with open(os.environ["MERAKI_CACHE_LOG"], "a") as f:
                f.write("{:.0f} {} {}\n".format(time.time(), os.getpid(), path))
        while True:
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8") or "null")
                    link = r.headers.get("Link", "")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    delay = min(float(e.headers.get("Retry-After") or 2), 15)
                    if time.time() + delay < deadline:
                        time.sleep(delay)
                        continue
                raise MerakiError("Meraki API HTTP {} on {}: {}".format(
                    e.code, path, e.read().decode("utf-8", "ignore")[:200]))
            except MerakiError:
                raise
            except Exception as e:
                raise MerakiError("Request failed on {}: {}".format(path, e))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            wrapped, data = True, data["items"]
        if not isinstance(data, list):
            return data
        out += data
        m = re.search(r'<([^>]+)>;\s*rel=next', link)
        url = m.group(1) if m else None
    return out


def org_get(api_key, path, params=None, ttl=240, max_stale=900, wait=40):
    """Return (data, age_seconds) for an organization-wide GET.

    ttl: a cached copy younger than this is used without calling the API.
    max_stale: on API failure, a cached copy up to this old is still returned.
    wait: how long to wait for another process that is already fetching.
    """
    params = dict(params or {})
    key = hashlib.sha256(json.dumps(
        [hashlib.sha256(api_key.encode()).hexdigest(), path, sorted(params.items())],
        default=str).encode()).hexdigest()[:24]
    base = os.path.join(cache_dir(), key)
    data_file, lock_file = base + ".json", base + ".lock"

    def read():
        try:
            with open(data_file, encoding="utf-8") as f:
                c = json.load(f)
            return c["data"], time.time() - c["fetched"]
        except Exception:
            return None, None

    data, age = read()
    if data is not None and age < ttl:
        return data, age

    start = time.time()
    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_file) > LOCK_STALE:
                    os.remove(lock_file)
                    continue
            except OSError:
                continue
            # someone else is fetching: wait for their result
            data, age = read()
            if data is not None and age < ttl:
                return data, age
            if time.time() - start > wait:
                if data is not None and age < max_stale:
                    return data, age
                raise MerakiError("timed out waiting for another sensor to fetch {}".format(path))
            time.sleep(1)

    try:
        os.close(fd)
        # the process that held the lock before us may have just refreshed it
        data, age = read()
        if data is not None and age < ttl:
            return data, age
        try:
            fresh = _get_pages(api_key, path, params, deadline=start + wait)
        except MerakiError:
            data, age = read()
            if data is not None and age < max_stale:
                return data, age
            raise
        tmp = "{}.{}.tmp".format(data_file, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched": time.time(), "path": path, "data": fresh}, f)
        os.replace(tmp, data_file)
        return fresh, 0.0
    finally:
        try:
            os.remove(lock_file)
        except OSError:
            pass


def check_key(api_key):
    """Return an error message for an empty or unsubstituted key, else None."""
    if not api_key:
        return "No API key (--api-key / placeholder / MERAKI_API_KEY came through empty)."
    if api_key.startswith("%") or " " in api_key:
        return "API key looks unresolved/malformed (starts with '{}').".format(api_key[:1])
    return None
