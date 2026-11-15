"""Export every hotspot of an area and period with its harmonization weight (full archive).

The web app exports the last 31 days; this command exports any period since 2000 from the local
store, after `python -m ember_bridge.build` has fitted the model.

    python -m ember_bridge.export --region canada --circle 56.73,-111.38,80 --start 2016-04-01 --end 2016-07-31 --out fort_mcmurray_2016.csv
    python -m ember_bridge.export --region canada --polygon "-118,50;-106,50;-106,62;-118,62" --start 2023-01-01 --out area.csv
    add --all-satellites to also list the detections outside the backbone (weight 0)

Columns: time, place, satellite, confidence, FRP, harmonization weight with its 95 % range, the
weight's fallback level, the Terra-only flag and a fire id (hotspots touching in space and time).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import events as E
from . import harmonize as H
from .analytics import EXPORT_COLUMNS, circle, in_polygon
from .build import HARM_COLS, NO_DAYS, SOURCES, _backbone_rows, gaps_from_report
from .ingest import partitions


def export(store: Path, model: Path, region: str, polygon, start: str, end: str,
           all_satellites: bool = False) -> pd.DataFrame:
    parts = partitions(store)
    cal_dir = model / "calibration"
    cals = {s: H.Calibration.load(cal_dir, s) for s in SOURCES if (cal_dir / f"{s}_to_SNPP_meta.json").exists()}
    report = json.loads((model / "validation.json").read_text())
    bb = H.backbone(report.get("snpp_last_day"))
    gaps = gaps_from_report(report)            # Suomi NPP outage days: NOAA-20 stands in, as in the calendar
    gap = gaps.get(region, NO_DAYS)
    frames = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        if (region, year) not in parts:
            continue
        used = []
        for platform, s, e in bb:
            d = _backbone_rows(store, parts, region, year, platform, max(s, start), min(e, end), cals, gaps=gaps)
            if d is not None:
                frames.append(d)
                used.append((platform, max(s, start), min(e, end)))
        if all_satellites:
            for platform, path in parts[(region, year)].items():
                d = pd.read_parquet(path, columns=HARM_COLS)
                d = d[(d["date"] >= start) & (d["date"] <= end)]
                for p, s, e in used:   # rows already exported as backbone
                    if p == platform:
                        d = d[~((d["date"] >= s) & (d["date"] <= e) & ((p == "Terra") | (d["orbit_group"] == "afternoon")))]
                    if p == H.REFERENCE and platform == "NOAA20":     # NOAA-20 on Suomi NPP outage days
                        d = d[~((d["date"] >= s) & (d["date"] <= e) & d["date"].isin(gap)
                                & (d["orbit_group"] == "afternoon"))]
                if len(d):
                    frames.append(d.assign(region=region, weight=0.0, weight_lo=0.0, weight_hi=0.0, weight_level=-1,
                                           in_reference=False, low_confidence_period=False))
    if not frames:
        return pd.DataFrame(columns=EXPORT_COLUMNS)
    d = pd.concat(frames, ignore_index=True)
    d = d[in_polygon(d["lat"].to_numpy(), d["lon"].to_numpy(), polygon)]
    if d.empty:
        return pd.DataFrame(columns=EXPORT_COLUMNS)
    with_ids, _ = E.detect_events(d)
    d = d.merge(with_ids[["time_utc", "lat", "lon", "platform", "event_id"]], on=["time_utc", "lat", "lon", "platform"],
                how="left").drop_duplicates(subset=["time_utc", "lat", "lon", "platform"])
    return d[[c for c in EXPORT_COLUMNS if c in d]].sort_values("time_utc")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", required=True)
    shape = ap.add_mutually_exclusive_group(required=True)
    shape.add_argument("--polygon", help='"lon,lat;lon,lat;..."')
    shape.add_argument("--circle", help="lat,lon,radius_km")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default="2100-01-01")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--all-satellites", action="store_true")
    ap.add_argument("--store", type=Path, default=Path("data/store"))
    ap.add_argument("--model", type=Path, default=Path("data/model"))
    args = ap.parse_args()
    if args.circle:
        lat, lon, km = (float(v) for v in args.circle.split(","))
        polygon = circle(lat, lon, km)
    else:
        polygon = [tuple(float(v) for v in pt.split(",")) for pt in args.polygon.split(";")]
    out = export(args.store, args.model, args.region, polygon, args.start, args.end, args.all_satellites)
    out.to_csv(args.out, index=False)
    print(f"{len(out):,} hotspots written to {args.out}")


if __name__ == "__main__":
    main()
