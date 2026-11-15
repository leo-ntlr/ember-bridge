"""Synthetic raw FIRMS files (area-API layout) for 3 regions, 2000-11 to 2026-11, all 5 satellites.

The truth is known: each day has fires with an intensity (FRP); Terra (10:30) and Aqua (13:30)
MODIS miss small fires and see one 1-4 km pixel per fire; Suomi NPP, NOAA-20 and NOAA-21 VIIRS see
several 375 m pixels. A static industrial source (type 2) sits in Canada and keeps showing up in the
near-real-time files, which carry no type: the ingest step must mask it.
"""
from __future__ import annotations

import gzip
from datetime import date, timedelta
from pathlib import Path

import numpy as np

START = {"Terra": "2000-11-01", "Aqua": "2002-07-04", "SNPP": "2012-01-20", "NOAA20": "2018-04-01",
         "NOAA21": "2023-03-01"}
END = {"SNPP": "2026-11-01"}
SOURCE = {"Terra": "MODIS", "Aqua": "MODIS", "SNPP": "VIIRS_SNPP", "NOAA20": "VIIRS_NOAA20", "NOAA21": "VIIRS_NOAA21"}
CODE = {"Terra": "Terra", "Aqua": "Aqua", "SNPP": "N", "NOAA20": "N20", "NOAA21": "N21"}
ZONES = {"canada": [(56.5, -112.0), (50.5, -120.0), (62.0, -116.0)],
         "amazon": [(-9.0, -63.0), (-6.0, -52.0), (-12.0, -55.0)],
         "southern_africa": [(-12.0, 24.0), (-10.0, 30.0), (-14.0, 34.0)]}
PEAK = {"canada": 200, "amazon": 245, "southern_africa": 220}          # day of year of the season peak
MODIS_COLS = "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight"
VIIRS_COLS = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight"


def write(raw: Path, regions=("canada", "amazon", "southern_africa"), first="2000-11-01", last="2026-11-13",
          fires_per_day=3.0, nrt_from="2026-06-01", seed=7, snpp_outages=()) -> int:
    """snpp_outages: (first day, last day) pairs when Suomi NPP sends nothing, like its real outages."""
    rng = np.random.default_rng(seed)
    outages = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in snpp_outages]
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    n_rows = 0
    for region in regions:
        lines = {}   # (platform, year, nrt) -> list of csv lines
        day = d0
        while day <= d1:
            doy = day.timetuple().tm_yday
            season = np.exp(-0.5 * ((doy - PEAK[region]) / 30.0) ** 2) * 2.5 + 0.05
            boost = 2.5 if (region == "canada" and day.year == 2023) else 1.0     # a record season
            n = rng.poisson(fires_per_day * season * boost * (1 + 0.01 * (day.year - 2000)))
            for _ in range(n):
                lat0, lon0 = ZONES[region][rng.integers(len(ZONES[region]))]
                lat, lon = lat0 + rng.normal(0, 0.8), lon0 + rng.normal(0, 0.8)
                frp = rng.lognormal(2.4, 1.0)
                for platform, start in START.items():
                    if not (date.fromisoformat(start) <= day <= date.fromisoformat(END.get(platform, last))):
                        continue
                    modis = platform in ("Terra", "Aqua")
                    local = 10.5 if platform == "Terra" else 13.5
                    utc = (local - lon / 15.0) % 24
                    if modis:
                        if rng.random() > 1 - np.exp(-frp / 12):
                            continue
                        k, size = 1, rng.choice([1.0, 1.5, 2.2], p=[0.5, 0.3, 0.2])
                    else:
                        k, size = max(1, rng.poisson(1.0 + frp / 15)), 0.38
                    for _ in range(k):
                        jit = 0.004 if not modis else 0.0
                        nrt = day >= date.fromisoformat(nrt_from)
                        conf = str(rng.integers(40, 100)) if modis else rng.choice(["n", "h"])
                        version = ("6.1" if modis else "2") + ("NRT" if nrt and modis else ".0NRT" if nrt else "")
                        row = [f"{lat + rng.normal(0, jit):.5f}", f"{lon + rng.normal(0, jit):.5f}", "330.0",
                               f"{size:.2f}", f"{size:.2f}", day.isoformat(), f"{int(utc):02d}{int(utc % 1 * 60):02d}",
                               CODE[platform], "MODIS" if modis else "VIIRS", conf, version, "300.0",
                               f"{frp / (1 if modis else 3):.2f}", "D"]
                        if platform == "SNPP" and any(a <= day <= b for a, b in outages):
                            continue
                        lines.setdefault((platform, day.year, nrt), []).append(",".join(row))
            if region == "canada" and rng.random() < 0.5:          # the static source, every other day
                for platform in ("Aqua", "SNPP"):
                    if date.fromisoformat(START[platform]) <= day <= date.fromisoformat(END.get(platform, last)):
                        nrt = day >= date.fromisoformat(nrt_from)
                        row = ["57.0101", "-111.6402", "340.0", "1.0", "1.0", day.isoformat(), "2030", CODE[platform],
                               "MODIS" if platform == "Aqua" else "VIIRS", "90" if platform == "Aqua" else "h",
                               ("6.1" if platform == "Aqua" else "2") + ("NRT" if nrt and platform == "Aqua" else ".0NRT" if nrt else ""),
                               "300.0", "30.0", "D"]
                        lines.setdefault((platform, day.year, nrt), []).append(",".join(row) + ("" if nrt else ",S"))
            day += timedelta(days=1)
        for (platform, year, nrt), rows in lines.items():
            modis = platform in ("Terra", "Aqua")
            header = (MODIS_COLS if modis else VIIRS_COLS) + ("" if nrt else ",type")
            body = [r.replace(",S", ",2") if r.endswith(",S") else (r if nrt else r + ",0") for r in rows]
            source = SOURCE[platform] + ("_NRT" if nrt else "_SP")
            first_day = date(year, 1, 1) if not nrt else date.fromisoformat(nrt_from)
            n_days = (date(year, 12, 31) - first_day).days + 1
            out = raw / region / "api" / f"{source}_{first_day.isoformat()}_{n_days}_{platform}.csv.gz"
            out = out.with_name(f"{source}_{first_day.isoformat()}_{n_days}.csv.gz") if platform not in ("Terra",) \
                else out.with_name(f"{source}_{first_day.isoformat()}_{n_days}.csv.gz".replace(".csv.gz", ".csv.gz"))
            out.parent.mkdir(parents=True, exist_ok=True)
            mode = "at" if out.exists() else "wt"
            with gzip.open(out, mode) as f:
                if mode == "wt":
                    f.write(header + "\n")
                f.write("\n".join(body) + "\n")
            n_rows += len(body)
    return n_rows
