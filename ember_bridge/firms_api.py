"""Download FIRMS hotspots through the official FIRMS area API (free MAP_KEY needed).

The key is read from the FIRMS_MAP_KEY environment variable or from a local .env file
(one line: FIRMS_MAP_KEY=...). It is never printed, logged or written anywhere else. Never commit .env.

    python -m ember_bridge.firms_api check      # key, quota, data availability, one test request
    python -m ember_bridge.firms_api all        # every region and satellite since 2000, resumable
    python -m ember_bridge.firms_api status     # progress of the downloads (works offline)
    python -m ember_bridge.firms_api update   --region all                 # last 5 days, near real time
    python -m ember_bridge.firms_api backfill --region canada --family VIIRS_NOAA20 --start 2018-04-01

API rules (FIRMS area API page): /api/area/csv/[MAP_KEY]/[SOURCE]/[west,south,east,north]/[DAY_RANGE]/[DATE],
DAY_RANGE 1..5, 5000 transactions per 10 minutes per MAP_KEY.
One gzip CSV per window lands in data/raw/<region>/api/, where ember_bridge.ingest picks it up.
Standard library only, so downloads can start before the scientific packages are installed.
"""
from __future__ import annotations

import argparse
import csv
import errno
import gzip
import hashlib
import http.client
import io
import json
import logging
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = os.environ.get("FIRMS_BASE_URL", "https://firms.modaps.eosdis.nasa.gov").rstrip("/")
MAX_DAY_RANGE = 5
GRID_EPOCH = date(2000, 1, 1)   # windows sit on a fixed 5-day grid, so file names stay stable between runs
USER_AGENT = "ember-bridge/0.9 (NASA Space Apps 2026; python-urllib)"
# FIRMS allows 5000 transactions per 10 minutes and one area request costs several: about 20 for a
# 5-day window (measured on 15 Nov 2026), so the quota, not the network, sets the pace of a full download.
LIMIT_PAUSE_S = 600.0           # FIRMS said no: everyone waits 10 minutes (20, then 30 if it says no again)
MIN_INTERVAL_S = 0.5            # at most 2 requests started per second, all workers together
RETRY_WAIT_S = 20.0             # network or server hiccup: wait 20 s, 40 s, 60 s...
STALL_S = 6 * 3600.0            # six hours without a single successful request: give up and say why
QUOTA_SHARE = 0.85              # quota guard: hold every download while the key has used 85 % of its quota
STATUS_EVERY_S = 3.0            # the guard reads the key's transaction count every 3 s...
HOLD_S = 15.0                   # ...and every 15 s while it holds the downloads
FAIL_STREAK = 12                # that many windows failing in a row: FIRMS is probably down, pause
TRANSACTIONS_PER_DAY = 4.0      # measured: a 5-day window over a region costs about 20 transactions

# west, south, east, north
REGIONS = {
    "canada": (-141.0, 41.6, -52.6, 83.2),
    "amazon": (-74.0, -18.0, -44.0, 5.0),
    "southern_africa": (11.0, -18.0, 41.0, -4.0),
}
REGION_ORDER = ["canada", "amazon", "southern_africa"]   # the showcase region first
# Science-quality (SP) archive first, near-real-time (NRT) after the last SP day.
FAMILIES = {
    "MODIS": ("MODIS_SP", "MODIS_NRT"),
    "VIIRS_SNPP": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
    "VIIRS_NOAA20": ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"),
    "VIIRS_NOAA21": ("VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT"),
}
FIRST_DAY = {"MODIS": "2000-11-01", "VIIRS_SNPP": "2012-01-20", "VIIRS_NOAA20": "2018-04-01",
             "VIIRS_NOAA21": "2023-03-01"}
NRT_UPDATE_SOURCES = ["MODIS_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]  # Suomi NPP stopped 2026-11-02
# Download order for `all`. Tier 1 is everything the harmonized calendar needs (MODIS, Suomi NPP,
# recent NOAA-20); tier 2 is the rest of NOAA-20 and NOAA-21 (extra calibration, satellite strip).
CORE_NOAA20_FROM = "2024-01-01"
TIERS = [
    [("MODIS", None, None), ("VIIRS_SNPP", None, None), ("VIIRS_NOAA20", CORE_NOAA20_FROM, None)],
    [("VIIRS_NOAA20", None, "2023-12-31"), ("VIIRS_NOAA21", None, None)],
]
# Test request that also tells whether DATE is the first or the last day of a window: the
# Fort McMurray fire burned hard on 3, 4 and 5 May 2016, so a 2-day window on 4 May separates the two.
PROBE = ("MODIS_SP", (-113.0, 55.5, -110.0, 57.5), "2016-05-04", 2)

log = logging.getLogger("ember_bridge.firms_api")


class FirmsKeyError(RuntimeError):
    """FIRMS refused the MAP_KEY: nothing else can work, so everything stops."""


class FirmsFatalError(RuntimeError):
    """A problem no retry can fix (full disk, quota that never comes back)."""


# --------------------------------------------------------------------------- key and HTTP

def load_key() -> str | None:
    """FIRMS_MAP_KEY from the environment, else from .env in the current or the project folder."""
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if key:
        return key
    for folder in (Path.cwd(), Path(__file__).resolve().parents[1]):
        env = folder / ".env"
        if not env.is_file():
            continue
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            name, sep, value = line.partition("=")
            name = name.strip()
            if name.startswith("export "):
                name = name[len("export "):].strip()
            value = value.strip().strip('"').strip("'").strip()
            if sep and name in ("FIRMS_MAP_KEY", "MAP_KEY") and value:
                return value
    return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # python.org builds on macOS have no certificates without it
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


class _Gate:
    """Shared by every download thread.

    It spaces the requests, and its quota guard reads the key's transaction count from FIRMS and holds
    every download while the key is near its quota, so FIRMS never has to say no. If FIRMS says no
    anyway (a quota message, or "Invalid MAP_KEY." for a key it accepted a minute before, which is how
    it blocked the key on 15 Nov 2026), every download pauses, a little longer each time in a row.
    It also keeps the time of the last success, to give up after hours without one.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_start = 0.0
        self.pause_until = 0.0      # FIRMS said no: everyone waits
        self.hold_until = 0.0       # quota guard: everyone waits for the count to go down
        self.last_success = time.time()
        self.requests = 0
        self.refusals = 0           # times in a row FIRMS said no
        self.key: str | None = None # key watched by the quota guard
        self.status_at = 0.0
        self.held = False
        self.holds = 0
        self.quota: tuple[int, int] | None = None   # last reading: (used, limit)
        self.worked: set[str] = set()               # fingerprints of the keys FIRMS accepted in this run

    def watch(self, key: str | None) -> None:
        """Turn the quota guard on for this key (None turns it off)."""
        with self.lock:
            self.key, self.status_at, self.held = key, 0.0, False

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                if (self.key and now >= self.pause_until and now >= self.hold_until
                        and now - self.status_at >= (HOLD_S if self.held else STATUS_EVERY_S)):
                    self._guard()
                    now = time.time()
                start = max(self.pause_until, self.hold_until, self.next_start)
                if now >= start:
                    self.next_start = now + MIN_INTERVAL_S
                    self.requests += 1
                    return
                delay = start - now
            time.sleep(min(max(delay, 0.01), 5.0))

    def _guard(self) -> None:
        """Read the key's transaction count (lock held) and hold every download near the quota."""
        st = key_status(self.key, timeout=20.0)
        now = self.status_at = time.time()
        used = _as_int((st or {}).get("current_transactions"))
        limit = _as_int((st or {}).get("transaction_limit"))
        if used is None or not limit:
            return                  # no reading: the pause when FIRMS says no still protects the key
        self.quota = (used, limit)
        if used >= QUOTA_SHARE * limit:
            if not self.held:
                self.holds += 1
                if self.holds == 1:
                    log.info("quota guard: %d of %d FIRMS transactions used in the last 10 minutes, downloads "
                             "wait for the count to go down (this keeps happening: it is the normal pace)",
                             used, limit)
            self.held = True
            self.hold_until = now + HOLD_S
        else:
            self.held = False

    def refused(self, why: str) -> None:
        """FIRMS said no: pause every download, 10 minutes, then 20, then 30 if it says no again in a row."""
        with self.lock:
            now = time.time()
            if now < self.pause_until:
                return              # another download already met the same refusal
            self.refusals += 1
            pause = min(LIMIT_PAUSE_S * self.refusals, 3 * LIMIT_PAUSE_S)
            self.pause_until = now + pause
        log.warning("%s: all downloads pause for %s", why, _fmt_duration(pause))

    def success(self, secret: str | None = None) -> None:
        with self.lock:
            self.last_success = time.time()
            self.refusals = 0
            if secret:
                self.worked.add(_fingerprint(secret))

    def accepted_before(self, secret: str | None) -> bool:
        with self.lock:
            return bool(secret) and _fingerprint(secret) in self.worked

    def stalled(self) -> bool:
        return time.time() - self.last_success > STALL_S


GATE = _Gate()


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "<MAP_KEY>") if secret else text


def _key_problem(text: str) -> bool:
    t = text.lower()
    return "invalid" in t and ("map_key" in t or "mapkey" in t or "map key" in t or " key" in t)


def _quota_problem(text: str) -> bool:
    t = text.lower()
    return ("limit" in t and ("transaction" in t or "exceed" in t)) or "too many requests" in t


def _http_get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch(url: str, secret: str | None = None, retries: int = 6, timeout: float = 300.0) -> str:
    """GET one FIRMS answer.

    Waits out quota pauses and server or network hiccups (a laptop can lose Wi-Fi for a minute).
    A key FIRMS never accepted stops everything at once; a key it accepted earlier in this run and now
    refuses is a temporary block, waited out like the quota. Anything unexpected fails loudly.
    The key never appears in an error message.
    """
    errors = 0
    while True:
        GATE.wait()
        problem = None
        refusal = None
        text = None
        try:
            text = _http_get(url, timeout)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            if _key_problem(body):
                refusal = body
            elif e.code == 429 or _quota_problem(body):
                problem = "quota"
            elif e.code >= 500:
                problem = f"HTTP {e.code}"
            else:
                raise RuntimeError(f"FIRMS refused the request (HTTP {e.code}): "
                                   f"{_redact(body.strip()[:200], secret)}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise FirmsFatalError(
                    "HTTPS certificate check failed. Run: .venv/bin/python -m pip install certifi "
                    "(or open 'Install Certificates.command' in your Python folder), then run again.") from None
            problem = f"network ({type(reason).__name__})"
        if text is not None:
            head = text.lstrip()[:300].lower()
            if head == "" or head.startswith(("latitude", "country_id", "data_id")):
                GATE.success(secret)
                return text
            if _key_problem(head):
                refusal = text
            elif _quota_problem(head):
                problem = "quota"
            else:
                raise RuntimeError(f"FIRMS answered: {_redact(text.strip()[:200], secret)!r}")
        if refusal is not None:
            if not GATE.accepted_before(secret):
                raise FirmsKeyError(_redact(refusal.strip()[:200], secret))
            problem = "refused"
        if problem in ("quota", "refused"):
            if GATE.stalled():
                raise FirmsFatalError(
                    f"FIRMS has refused every request for {STALL_S / 3600:.0f} hours (transaction quota or a "
                    "block of the key). Run the same command again later: downloads resume where they stopped.")
            GATE.refused("FIRMS transaction quota reached" if problem == "quota" else
                         "FIRMS refused the key it accepted before (a temporary block after the quota)")
            continue
        errors += 1
        if errors >= retries:
            raise RuntimeError(f"FIRMS did not answer after {retries} tries ({problem})")
        time.sleep(RETRY_WAIT_S * errors)


def key_status(key: str, timeout: float = 60.0) -> dict | None:
    """Quota of the key ({'transaction_limit', 'current_transactions', ...}), or None if unavailable."""
    try:
        data = json.loads(_http_get(f"{BASE}/mapserver/mapkey_status/?MAP_KEY={key}", timeout))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def availability(key: str) -> dict[str, tuple[str, str]]:
    """First and last available day of every FIRMS source: {data_id: (min_date, max_date)}."""
    text = fetch(f"{BASE}/api/data_availability/csv/{key}/ALL", secret=key)
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        sid = (row.get("data_id") or "").strip()
        if sid:
            out[sid] = ((row.get("min_date") or "")[:10], (row.get("max_date") or "")[:10])
    return out


def area_url(key: str, source: str, bbox, day_range: int, day: str | None = None) -> str:
    if not 1 <= day_range <= MAX_DAY_RANGE:
        raise ValueError("DAY_RANGE must be 1..5")
    area = ",".join(f"{v:g}" for v in bbox)
    url = f"{BASE}/api/area/csv/{key}/{source}/{area}/{day_range}"
    return f"{url}/{day}" if day else url


def probe_date_semantics(key: str) -> tuple[str, list[str]]:
    """'start' if DATE is the first day of a window, 'end' if it is the last, else 'unknown'."""
    source, bbox, day, n = PROBE
    text = fetch(area_url(key, source, bbox, n, day), secret=key)
    days = sorted({(r.get("acq_date") or "")[:10] for r in csv.DictReader(io.StringIO(text))} - {""})
    d = date.fromisoformat(day)
    first = {(d + timedelta(days=k)).isoformat() for k in range(n)}
    last = {(d - timedelta(days=k)).isoformat() for k in range(n)}
    seen = set(days)
    if seen and seen <= first and not seen <= last:
        return "start", days
    if seen and seen <= last and not seen <= first:
        return "end", days
    return "unknown", days


# --------------------------------------------------------------------------- planning

def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def windows(start: str, end: str, step: int = MAX_DAY_RANGE, align: bool = True) -> list[tuple[str, int]]:
    """Split [start, end] into (first_day, n_days) windows of at most `step` days.

    With align=True the windows follow a fixed grid (every 5 days from 2000-01-01), so a window
    keeps its file name even when the start of the range moves between two runs.
    """
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d0 <= d1:
        n = step - (d0 - GRID_EPOCH).days % step if align else step
        n = min(n, (d1 - d0).days + 1)
        out.append((d0.isoformat(), n))
        d0 += timedelta(days=n)
    return out


def plan(family: str, start: str, end: str, avail: dict | None) -> list[tuple[str, str, str]]:
    """Which source covers which dates: SP archive as far as it goes, then NRT."""
    sp, nrt = FAMILIES[family]
    if avail is None:
        return [(sp, start, end)]
    jobs = []
    sp_end = None
    if sp in avail:
        lo, hi = avail[sp]
        sp_end = hi
        s0, s1 = max(start, lo), min(end, hi)
        if s0 <= s1:
            jobs.append((sp, s0, s1))
    if nrt in avail:
        lo, hi = avail[nrt]
        n0 = start if sp_end is None else max(start, _next_day(sp_end))
        n0, n1 = max(n0, lo), min(end, hi)
        if n0 <= n1:
            jobs.append((nrt, n0, n1))
    return jobs


@dataclass(frozen=True)
class Window:
    region: str
    source: str
    day: str   # first day of the window
    n: int     # number of days

    @property
    def name(self) -> str:
        return f"{self.source}_{self.day}_{self.n}.csv.gz"

    def path(self, raw_dir: Path) -> Path:
        return Path(raw_dir) / self.region / "api" / self.name

    def split(self) -> list["Window"]:
        d0 = date.fromisoformat(self.day)
        return [Window(self.region, self.source, (d0 + timedelta(days=k)).isoformat(), 1) for k in range(self.n)]

    def done(self, raw_dir: Path) -> bool:
        p = self.path(raw_dir)
        if p.exists() or p.with_name(p.name[:-3]).exists():          # also accept an older plain .csv
            return True
        return self.n > 1 and all(w.path(raw_dir).exists() for w in self.split())


def build_windows(avail: dict | None, regions=None, end: str | None = None, tiers=None) -> list[Window]:
    """Every window to download, in priority order: tier, then region, then satellite, then date."""
    end = end or (date.today() - timedelta(days=1)).isoformat()
    out = []
    for tier in (tiers or TIERS):
        for region in (regions or REGION_ORDER):
            for family, start, stop in tier:
                s0, s1 = start or FIRST_DAY[family], min(stop or end, end)
                if s0 > s1:
                    continue
                for source, a, b in plan(family, s0, s1, avail):
                    out.extend(Window(region, source, d, n) for d, n in windows(a, b))
    return out


# --------------------------------------------------------------------------- downloading

def _write_gz(path: Path, text: str) -> int:
    """Write text as gzip through a .part file renamed at the end (a crash never leaves half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise FirmsFatalError("The disk is full. Free some space, then run the same command again "
                                  "(downloads resume where they stopped).") from None
        raise
    return path.stat().st_size


def download_window(key: str, w: Window, raw_dir: Path, semantics: str = "start") -> tuple[int, int]:
    """Fetch one window and store it. Returns (hotspot rows, bytes on disk)."""
    day = w.day if semantics != "end" else (date.fromisoformat(w.day) + timedelta(days=w.n - 1)).isoformat()
    text = fetch(area_url(key, w.source, REGIONS[w.region], w.n, day), secret=key)
    size = _write_gz(w.path(raw_dir), text)
    return max(text.count("\n") - 1, 0), size


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}" if seconds >= 3600 else f"{seconds // 60} min"


def run_windows(key: str, todo: list[Window], raw_dir: Path, semantics: str = "start",
                workers: int = 4, label: str = "") -> list[tuple[Window, str]]:
    """Download windows in parallel, in the given order. Returns the windows that failed, with why."""
    if not todo:
        return []
    GATE.watch(key)
    failed: list[tuple[Window, str]] = []
    stop = threading.Event()
    lock = threading.Lock()
    tally = {"done": 0, "rows": 0, "bytes": 0, "streak": 0}
    t0, last_log = time.time(), 0.0
    marks = deque([(t0, 0)])        # (time, windows done): the time left is read on the last 30 minutes

    def job(w: Window):
        if stop.is_set():
            return w, None, "stopped"
        try:
            return w, download_window(key, w, raw_dir, semantics), None
        except (FirmsKeyError, FirmsFatalError):
            stop.set()
            raise
        except Exception as e:  # one bad window must not stop the night: it is retried afterwards
            return w, None, f"{type(e).__name__}: {e}"[:300]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(job, w) for w in todo]
        try:
            for fut in as_completed(futures):
                w, res, err = fut.result()
                with lock:
                    tally["done"] += 1
                    if err:
                        if err != "stopped":
                            failed.append((w, err))
                            log.warning("failed %s/%s %s (%d days): %s", w.region, w.source, w.day, w.n, err)
                            tally["streak"] += 1
                            if tally["streak"] >= FAIL_STREAK:   # FIRMS down: do not burn the plan, wait
                                GATE.refused(f"{tally['streak']} windows failed in a row (FIRMS may be down)")
                                tally["streak"] = 0
                    else:
                        tally["rows"] += res[0]
                        tally["bytes"] += res[1]
                        tally["streak"] = 0
                    now = time.time()
                    if now - last_log > 60 or tally["done"] == len(todo):
                        last_log = now
                        marks.append((now, tally["done"]))
                        while len(marks) > 2 and now - marks[1][0] > 1800:
                            marks.popleft()
                        t_then, done_then = marks[0]
                        rate = (tally["done"] - done_then) / max(now - t_then, 1e-9)
                        quota = f", quota {GATE.quota[0]}/{GATE.quota[1]}" if GATE.quota else ""
                        log.info("%s%d/%d windows (%.0f%%), %s hotspots, %.2f GB, now %s %s %s%s, about %s left",
                                 label, tally["done"], len(todo), 100 * tally["done"] / len(todo),
                                 f"{tally['rows']:,}", tally["bytes"] / 1e9, w.region, w.source, w.day, quota,
                                 _fmt_duration((len(todo) - tally["done"]) / max(rate, 1e-9)))
        except BaseException:
            stop.set()
            for f in futures:
                f.cancel()
            raise
    return failed


def _plan_path(raw_dir: Path) -> Path:
    return Path(raw_dir) / "download_plan.json"


def _save_plan(raw_dir: Path, wins: list[Window], semantics: str) -> None:
    Path(raw_dir).mkdir(parents=True, exist_ok=True)
    data = {"created": datetime.now().isoformat(timespec="seconds"), "date_semantics": semantics,
            "windows": [[w.region, w.source, w.day, w.n] for w in wins]}
    _plan_path(raw_dir).write_text(json.dumps(data))


def update(key: str, region: str, raw_dir: Path, days: int = MAX_DAY_RANGE, bbox=None) -> list[Path]:
    """Fetch the most recent days for the satellites still flying. Overwrites the previous update."""
    bbox = bbox or REGIONS[region]
    paths = []
    for source in NRT_UPDATE_SOURCES:
        path = Path(raw_dir) / region / "nrt" / f"{source}_latest.csv.gz"
        _write_gz(path, fetch(area_url(key, source, bbox, days), secret=key))
        paths.append(path)
    return paths


def run_all(key: str, raw_dir: Path, regions=None, workers: int = 4, end: str | None = None,
            tiers=None) -> dict:
    """The overnight job: every window of every region and satellite, resumable, then the last days."""
    raw_dir = Path(raw_dir)
    regions = regions or REGION_ORDER
    avail = availability(key)
    try:
        semantics, days = probe_date_semantics(key)
    except (FirmsKeyError, FirmsFatalError):
        raise
    except Exception as e:
        semantics, days = "unknown", [str(e)[:100]]
    if semantics == "unknown":
        log.warning("could not confirm how FIRMS reads DATE (test returned %s): assuming first day", days)
        semantics = "start"
    wins = build_windows(avail, regions, end, tiers)
    _save_plan(raw_dir, wins, semantics)
    todo = [w for w in wins if not w.done(raw_dir)]
    log.info("plan: %d windows for %s, %d already on disk, %d to download with %d workers",
             len(wins), ", ".join(regions), len(wins) - len(todo), len(todo), workers)
    failed = run_windows(key, todo, raw_dir, semantics, workers)
    if failed:  # second chance, one day at a time (smaller answers, fewer timeouts)
        retry = [d for w, _ in failed for d in (w.split() if w.n > 1 else [w]) if not d.done(raw_dir)]
        log.info("retrying %d failed windows as %d single days", len(failed), len(retry))
        failed = run_windows(key, retry, raw_dir, semantics, workers, label="retry: ")
    fail_path = raw_dir / "download_failures.json"
    if failed:
        fail_path.write_text(json.dumps([[w.region, w.source, w.day, w.n, err] for w, err in failed], indent=1))
        log.warning("%d windows still missing (listed in %s): run the same command again to retry them",
                    len(failed), fail_path)
    elif fail_path.exists():
        fail_path.unlink()
    for region in regions:
        try:
            update(key, region, raw_dir)
        except (FirmsKeyError, FirmsFatalError):
            raise
        except Exception as e:
            log.warning("near-real-time update failed for %s: %s", region, e)
    summary = {"finished": datetime.now().isoformat(timespec="seconds"), "windows": len(wins),
               "missing": len(failed), "regions": regions}
    (raw_dir / "download_done.json").write_text(json.dumps(summary, indent=1))
    log.info("downloads finished: %d windows, %d missing", len(wins), len(failed))
    return summary


# --------------------------------------------------------------------------- reporting

def _pid_alive(pid_file: Path | None) -> int | None:
    if not pid_file or not Path(pid_file).exists():
        return None
    try:
        pid = int(Path(pid_file).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return None


def status(raw_dir: Path, pid_file: Path | None = None) -> str:
    """Progress of the downloads, read from disk only (no key, no network)."""
    raw_dir = Path(raw_dir)
    if not _plan_path(raw_dir).exists():
        return "No download started yet. Run: bash ember.sh"
    data = json.loads(_plan_path(raw_dir).read_text())
    wins = [Window(*w) for w in data["windows"]]
    rows: dict[tuple[str, str], list[int]] = {}
    for w in wins:
        r = rows.setdefault((w.region, w.source), [0, 0])
        r[1] += 1
        r[0] += w.done(raw_dir)
    done, total = sum(r[0] for r in rows.values()), len(wins)
    files = [p for p in raw_dir.rglob("*.csv.gz")]
    size = sum(p.stat().st_size for p in files)
    newest = max((p.stat().st_mtime for p in files), default=None)
    pid = _pid_alive(pid_file)
    state = (f"running (pid {pid})" if pid else
             "finished" if (raw_dir / "download_done.json").exists() and done == total else
             "stopped (bash ember.sh resumes it)")
    lines = [f"Downloads: {done:,} / {total:,} windows ({100 * done / max(total, 1):.0f}%), "
             f"{size / 1e9:.2f} GB on disk, {state}"
             + (f", last file {int(time.time() - newest)} s ago" if newest else "")]
    lines.append(f"{'region':<16} {'source':<18} {'done':>6} / planned")
    for (region, source), (d, t) in rows.items():
        lines.append(f"{region:<16} {source:<18} {d:>6} / {t:<6} {100 * d / t:>4.0f}%")
    fail_path = raw_dir / "download_failures.json"
    if fail_path.exists():
        n_fail = len(json.loads(fail_path.read_text()))
        lines.append(f"{n_fail} windows failed last time: run the same command again to retry them.")
    return "\n".join(lines)


def check(key: str, raw_dir: Path = Path("data/raw")) -> bool:
    """Is the key accepted, what is available, and how long will the full download take?"""
    print(f"MAP_KEY found ({len(key)} characters, not shown).")
    before = key_status(key)
    if before:
        print(f"Quota: {before.get('current_transactions')} of {before.get('transaction_limit')} transactions "
              f"used in the current {before.get('transaction_interval', '10 minutes')}.")
    avail = availability(key)
    print("FIRMS sources (first day -> last day):")
    for sp, nrt in FAMILIES.values():
        for sid in (sp, nrt):
            first, last = avail.get(sid, ("", ""))
            print(f"  {sid:<18} {first + ' -> ' + last if sid in avail else 'not offered'}")
    try:
        semantics, days = probe_date_semantics(key)
        meaning = {"start": "DATE is the first day of a window (as the code assumes)",
                   "end": "DATE is the last day of a window (the downloader adapts)",
                   "unknown": "could not tell, the downloader will assume the first day"}[semantics]
        print(f"Test request (Fort McMurray fire, May 2016): days {days or 'none'} -> {meaning}.")
    except (FirmsKeyError, FirmsFatalError):
        raise
    except Exception as e:
        print(f"Test request failed ({e}); the downloader will still try.")
    after = key_status(key)
    wins = build_windows(avail)
    todo = [w for w in wins if not w.done(raw_dir)]
    days = sum(w.n for w in todo)
    msg = f"Full download: {len(wins):,} requests ({len(todo):,} still to do, {days:,} days of data)."
    used_before = _as_int((before or {}).get("current_transactions"))
    used_after = _as_int((after or {}).get("current_transactions"))
    if used_before is not None and used_after is not None:
        msg += f" This check used {max(used_after - used_before, 0)} transactions."
    limit = _as_int((after or before or {}).get("transaction_limit")) or 5000
    minutes = days * TRANSACTIONS_PER_DAY / (QUOTA_SHARE * limit) * 10
    msg += (f" At the pace the quota allows (about {TRANSACTIONS_PER_DAY:g} transactions per day of data),"
            f" that takes about {_fmt_duration(minutes * 60)}.")
    print(msg)
    print("OK: the key works.")
    return True


def _setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers, force=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    parsers = {name: sub.add_parser(name) for name in ("check", "all", "status", "update", "backfill")}
    for p in parsers.values():
        p.add_argument("--raw", type=Path, default=Path("data/raw"))
    parsers["all"].add_argument("--regions", nargs="+", choices=REGION_ORDER, default=REGION_ORDER)
    parsers["all"].add_argument("--workers", type=int, default=4)
    parsers["all"].add_argument("--end", help="last day (default: yesterday)")
    for name in ("all", "status"):
        parsers[name].add_argument("--pid-file", type=Path)
    for name in ("all", "update", "backfill"):
        parsers[name].add_argument("--log-file", type=Path)
    parsers["update"].add_argument("--region", choices=sorted(REGIONS) + ["all"], required=True)
    parsers["backfill"].add_argument("--region", choices=sorted(REGIONS), required=True)
    parsers["backfill"].add_argument("--family", choices=sorted(FAMILIES), required=True)
    parsers["backfill"].add_argument("--start")
    parsers["backfill"].add_argument("--end")
    parsers["backfill"].add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    _setup_logging(getattr(args, "log_file", None))

    if args.cmd == "status":
        print(status(args.raw, args.pid_file))
        return
    key = load_key()
    if not key:
        sys.exit("No FIRMS MAP_KEY found. Put the line FIRMS_MAP_KEY=<your key> in a file named .env in this "
                 "folder (free key: https://firms.modaps.eosdis.nasa.gov/api/map_key/).")
    try:
        if args.cmd == "check":
            check(key, args.raw)
        elif args.cmd == "all":
            if args.pid_file:
                args.pid_file.parent.mkdir(parents=True, exist_ok=True)
                args.pid_file.write_text(str(os.getpid()))
            try:
                run_all(key, args.raw, args.regions, args.workers, args.end)
            finally:
                if args.pid_file and args.pid_file.exists() and args.pid_file.read_text().strip() == str(os.getpid()):
                    args.pid_file.unlink()
        elif args.cmd == "update":
            for region in (REGION_ORDER if args.region == "all" else [args.region]):
                for p in update(key, region, args.raw):
                    print("wrote", p)
        else:
            end = args.end or (date.today() - timedelta(days=1)).isoformat()
            tiers = [[(args.family, args.start, end)]]
            avail = availability(key)
            semantics = probe_date_semantics(key)[0]
            todo = [w for w in build_windows(avail, [args.region], end, tiers) if not w.done(args.raw)]
            failed = run_windows(key, todo, args.raw, "end" if semantics == "end" else "start", args.workers)
            print(f"downloaded {len(todo) - len(failed)} windows, {len(failed)} failed")
    except FirmsKeyError as e:
        sys.exit(f"FIRMS refused the MAP_KEY ({e}). If this key worked before, FIRMS is most likely blocking "
                 "it for a while after too many requests: keep .env, wait 30 to 60 minutes, then run the same "
                 "command again (downloads resume where they stopped). Only if it never worked: delete the .env "
                 "file and run the script again to enter the key once more.")
    except FirmsFatalError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
