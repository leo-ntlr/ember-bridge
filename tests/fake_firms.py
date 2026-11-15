"""A small local stand-in for the FIRMS API, to test the downloader end to end without network.

It answers the three endpoints the downloader uses, with a few hotspots per day inside the
requested box, and can inject the troubles of a real night: a quota message, a 503, a window
that always fails with 5 days but works day by day, and either reading of the DATE parameter.
It can also count transactions like FIRMS (several per area request, over a rolling interval),
answer a quota message when a request would go over the limit, and then block the key the way
FIRMS did on 15 Nov 2026: "Invalid MAP_KEY." for a while after a second overrun.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD_KEY = "0123456789abcdef0123456789abcdef"
AVAIL = {
    "MODIS_SP": ("2000-11-01", "2026-06-30"), "MODIS_NRT": ("2026-07-01", "2026-11-14"),
    "VIIRS_SNPP_SP": ("2012-01-20", "2026-05-31"), "VIIRS_SNPP_NRT": ("2026-06-01", "2026-11-01"),
    "VIIRS_NOAA20_SP": ("2018-04-01", "2026-05-31"), "VIIRS_NOAA20_NRT": ("2026-06-01", "2026-11-14"),
    "VIIRS_NOAA21_NRT": ("2024-01-17", "2026-11-14"),
}
SAT = {"MODIS": "Aqua", "VIIRS_SNPP": "N", "VIIRS_NOAA20": "N20", "VIIRS_NOAA21": "N21"}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.transactions = 0
        self.area_requests = []          # (source, date_arg, n)
        self.semantics = "start"
        self.quota_at = None             # area request number answered with a quota message, once
        self.fail_503_at = None          # area request number answered with HTTP 503, once
        self.always_fail = set()         # (source, first_day) failing whenever n > 1
        # transaction quota, like FIRMS: limit per rolling interval, cost per day of an area request
        self.limit, self.interval_s, self.cost_per_day = 5000, 600.0, 1
        self.spent = deque()             # (time, transactions)
        self.lockout_after = None        # overrun number from which the key is blocked
        self.lockout_s = 0.0
        self.locked_until = 0.0
        self.overruns = 0                # requests refused because they would go over the limit
        self.lockouts = 0                # requests answered "Invalid MAP_KEY." because of a block
        self.status_broken = False       # mapkey_status answers HTTP 500

    def used(self, now: float) -> int:   # lock held
        while self.spent and now - self.spent[0][0] > self.interval_s:
            self.spent.popleft()
        return sum(c for _, c in self.spent)


STATE = State()


def _rows(source: str, bbox, days: list[str]) -> str:
    family = source.rsplit("_", 1)[0]
    modis = family == "MODIS"
    sp = source.endswith("_SP")
    cols = ["latitude", "longitude", "bright_t" if False else ("brightness" if modis else "bright_ti4"),
            "scan", "track", "acq_date", "acq_time", "satellite", "instrument", "confidence", "version",
            "bright_t31" if modis else "bright_ti5", "frp", "daynight"] + (["type"] if sp else [])
    w, s, e, n = bbox
    lines = [",".join(cols)]
    for k, d in enumerate(days):
        for j in range(3):
            lat, lon = s + (n - s) * (0.3 + 0.1 * j), w + (e - w) * (0.4 + 0.01 * k)
            vals = [f"{lat:.4f}", f"{lon:.4f}", "330.1", "1.2" if modis else "0.4", "1.1" if modis else "0.37",
                    d, "1950" if j else "0640", SAT[family], "MODIS" if modis else "VIIRS",
                    "77" if modis else "n", ("6.1" if modis else "2") + ("" if sp else "NRT"),
                    "290.5", f"{10 + j:.1f}", "D" if j else "N"] + (["0"] if sp else [])
            lines.append(",".join(vals))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, code: int, body: str, ctype: str = "text/plain"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if url.path.startswith("/mapserver/mapkey_status"):
            key = urllib.parse.parse_qs(url.query).get("MAP_KEY", [""])[0]
            if key != GOOD_KEY:
                return self._send(400, json.dumps({"error": "Invalid MAP_KEY."}), "application/json")
            if STATE.status_broken:
                return self._send(500, "Internal Server Error")
            with STATE.lock:
                used = STATE.used(time.time())
            return self._send(200, json.dumps({"transaction_limit": STATE.limit, "current_transactions": used,
                                               "transaction_interval": "10 minutes"}), "application/json")
        if parts[:3] == ["api", "data_availability", "csv"]:
            if parts[3] != GOOD_KEY:
                return self._send(400, "Invalid MAP_KEY.")
            with STATE.lock:
                STATE.transactions += 1
                STATE.spent.append((time.time(), 1))
            body = "data_id,min_date,max_date\n" + "".join(f"{k},{a},{b}\n" for k, (a, b) in AVAIL.items())
            return self._send(200, body)
        if parts[:3] == ["api", "area", "csv"]:
            key, source, area, n = parts[3], parts[4], parts[5], int(parts[6])
            if key != GOOD_KEY:
                return self._send(400, "Invalid MAP_KEY.")
            arg = parts[7] if len(parts) > 7 else None
            with STATE.lock:
                now = time.time()
                if now < STATE.locked_until:
                    STATE.lockouts += 1
                    refusal = "blocked"
                elif STATE.used(now) + STATE.cost_per_day * n > STATE.limit:
                    STATE.overruns += 1
                    refusal = "quota"
                    if STATE.lockout_after and STATE.overruns >= STATE.lockout_after:
                        STATE.locked_until = now + STATE.lockout_s
                        STATE.lockouts += 1
                        refusal = "blocked"
                else:
                    refusal = None
                    STATE.spent.append((now, STATE.cost_per_day * n))
                    STATE.area_requests.append((source, arg, n))
                    number = len(STATE.area_requests)
                    STATE.transactions += STATE.cost_per_day * n
            if refusal == "blocked":
                return self._send(400, "Invalid MAP_KEY.")
            if refusal == "quota":
                return self._send(200, "Exceeding allowed transaction limit. Please try later.")
            if number == STATE.quota_at:
                return self._send(200, "Exceeding allowed transaction limit. Please try later.")
            if number == STATE.fail_503_at:
                return self._send(503, "Service Unavailable")
            if arg is None:
                d0 = date(2026, 11, 14) - timedelta(days=n - 1)
            else:
                d = date.fromisoformat(arg)
                d0 = d if STATE.semantics == "start" else d - timedelta(days=n - 1)
            if n > 1 and (source, d0.isoformat()) in STATE.always_fail:
                return self._send(500, "Internal Server Error")
            days = [(d0 + timedelta(days=k)).isoformat() for k in range(n)]
            bbox = [float(v) for v in area.split(",")]
            return self._send(200, _rows(source, bbox, days))
        return self._send(404, "Not found")


def start() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
