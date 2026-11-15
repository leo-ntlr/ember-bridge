"""From raw FIRMS downloads to the harmonized calendar, its validation and the web app data.

    python -m ember_bridge.build                  # everything, from data/raw
    python -m ember_bridge.build --skip-ingest    # reuse data/store (e.g. after changing the model)
    python -m ember_bridge.build --years 2026     # after `firms_api update`: refresh 2026 only,
                                                  # keep the fitted model, rewrite the web data

Every step reads one region-year at a time, so hundreds of millions of detections fit on a laptop:

 1. ingest     raw CSV / ZIP -> data/store (clean, compact Parquet)
 2. tables     cell-day pairs of Aqua, Terra and NOAA-20 with Suomi NPP -> small additive fit tables
 3. fit        time-split and leave-one-region-out models first, then the final models on all data
 4. validate   monthly predictions on held-out years and on a never-seen region -> validation.json
 5. harmonize  weights for every backbone hotspot -> monthly grid cube, fire events, recent hotspots
 6. web        compact JSON files for the web app -> web/data/
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import events as E
from . import harmonize as H
from .firms_api import REGIONS
from .ingest import ingest, partitions, read_partition

PLATFORMS = ["Terra", "Aqua", "SNPP", "NOAA20", "NOAA21"]
SOURCES = {"Aqua": "2012-01-20", "Terra": "2012-01-20", "NOAA20": "2018-04-01"}   # overlap with Suomi NPP
FACTOR_YEARS = (2013, 2016, 2019, 2022)   # fire-count factor: a few full overlap years are plenty
TRAIN_END_YEAR = 2022                     # time-split test: fit up to this year, test on the later years
EARLY_TEST_END_YEAR = 2015                # early-years test: fit on the later years, test 2012 to this year
GRID_DEG = 0.25                           # web cube cells (~25 km)
LOCAL_DEG = 1.0                           # local validation cells (~100 km, the size of a drawn area)
NOTABLE_EVENTS = 50_000                   # per region, largest fires kept with full details for the web
CUBE_ROWS_PER_FILE = 120_000              # web cube split in files of ~5 MB (hosts advise < 10 MB per file)
RECENT_DAYS = 31                          # hotspot-level CSV export window in the web app
CURRENT_FIRE_DAYS = 14
EPOCH = pd.Timestamp("2000-01-01")
REGION_LABELS = {"canada": "Canada", "amazon": "Brazilian Amazon", "southern_africa": "Southern Africa savanna"}
REF_COLS = ["lat", "lon", "date", "daynight", "orbit_group"]
SRC_COLS = ["lat", "lon", "date", "daynight", "frp", "pixel_area_km2", "solar_hour", "orbit_group", "platform"]
HARM_COLS = ["time_utc", "date", "month", "lat", "lon", "sensor", "platform", "orbit_group", "solar_hour",
             "daynight", "confidence", "frp", "pixel_area_km2"]
CELL_KEYS = ["region", "cell", "date", "daynight"]
GAP_SHARE = 0.5                           # Suomi NPP saw less than half of what NOAA-20 saw that day...
GAP_MIN = 100                             # ...while NOAA-20 saw at least this many detections: an outage
NO_DAYS = pd.DatetimeIndex([])


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def _ym(dates: pd.Series) -> pd.Series:
    """Months since January 2000 (the web app's time index)."""
    return ((dates.dt.year - 2000) * 12 + dates.dt.month - 1).astype("int32")


def _local_cell(d: pd.DataFrame) -> pd.Series:
    """1-degree cell id, for the local-scale validation."""
    i = np.floor(d["lat"].astype(float).to_numpy() / LOCAL_DEG).astype(np.int64) + 1000
    j = np.floor(d["lon"].astype(float).to_numpy() / LOCAL_DEG).astype(np.int64) + 1000
    return pd.Series(i * 10_000 + j, index=d.index, name="lc")


def region_grid(region: str, deg: float = GRID_DEG) -> dict:
    west, south, east, north = REGIONS.get(region, (-180.0, -90.0, 180.0, 90.0))
    return {"west": west, "south": south, "east": east, "north": north, "deg": deg,
            "ncols": int(np.ceil((east - west) / deg - 1e-9)), "nrows": int(np.ceil((north - south) / deg - 1e-9))}


def grid_cell(lat, lon, g: dict) -> np.ndarray:
    """Cell index on the region grid (row-major from the south-west corner); -1 outside the grid."""
    i = np.floor((np.asarray(lat, float) - g["south"]) / g["deg"]).astype(np.int64)
    j = np.floor((np.asarray(lon, float) - g["west"]) / g["deg"]).astype(np.int64)
    ok = (i >= 0) & (i < g["nrows"]) & (j >= 0) & (j < g["ncols"])
    return np.where(ok, i * g["ncols"] + j, -1)


# --------------------------------------------------------------------------- 2. fit tables

def _cell_day_src(src: pd.DataFrame) -> pd.DataFrame:
    d = src.assign(cell=H.cell_ids(src["lat"].astype(float), src["lon"].astype(float)))
    return d.groupby(CELL_KEYS, observed=True).agg(n_src=("lat", "size"), frp_src=("frp", "mean"),
                                                    area_src=("pixel_area_km2", "mean"),
                                                    hour_src=("solar_hour", "mean"))


def _cell_day_ref(ref: pd.DataFrame) -> pd.DataFrame:
    d = ref.assign(cell=H.cell_ids(ref["lat"].astype(float), ref["lon"].astype(float)))
    return d.groupby(CELL_KEYS, observed=True).agg(n_ref=("lat", "size"))


def _join(src_cd: pd.DataFrame, ref_cd: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Same table as harmonize.cell_day_table, from pre-aggregated pieces.

    Only days with Suomi NPP data in the region are kept: a day missing from the download must not
    read as "VIIRS saw nothing" next to the source detections of that day.
    """
    ref_days = ref_cd.index.get_level_values("date").unique()
    t = src_cd.join(ref_cd, how="outer").fillna({"n_src": 0, "n_ref": 0}).reset_index()
    t = t[(t["date"] >= start) & (t["date"] <= end) & t["date"].isin(ref_days)].copy()
    t["region"] = t["region"].astype(str)
    t["daynight"] = t["daynight"].astype(str)
    t["month"] = t["date"].dt.month
    t["block"] = t["cell"] * 1000 + (t["date"].dt.year - 2000) * 12 + t["month"] - 1
    return t


def _fire_starts(d: pd.DataFrame, region: str, platform: str) -> list[dict]:
    """Monthly counts of fire starts of one platform (for the fire-count factor)."""
    if d.empty:
        return []
    lab = E.cluster_labels(d["lat"].to_numpy(), d["lon"].to_numpy(), d["date"].to_numpy())
    starts = pd.Series(pd.to_datetime(d["date"]).to_numpy()).groupby(lab).min()
    return [{"region": region, "platform": platform, "month": int(m), "fires": int(n)}
            for m, n in starts.dt.month.value_counts().items()]


def snpp_gap_days(parts: dict, snpp_last: str) -> dict[str, pd.DatetimeIndex]:
    """Suomi NPP outage days per region, found against NOAA-20 (same 13:30 orbit, 50 minutes ahead).

    A day is an outage day when, over all regions together, Suomi NPP saw less than half of what NOAA-20
    saw: the satellite itself was down (NASA reported outages from 26 July to 10 August 2022, from 24 July
    to 2 August 2024 and from 10 to 17 July 2026). Pooling the regions keeps a fire seen at the edge of
    one swath and the centre of the other from passing for an outage. A day when Suomi NPP has no hotspot
    at all in a region where NOAA-20 saw many is one too (data missing for that region).
    On these days the calendar takes NOAA-20 hotspots, harmonized to Suomi NPP, and the fit and the
    validation leave the days out: their Suomi NPP "truth" is incomplete.
    """
    rows = []
    for (region, year), files in parts.items():
        if "SNPP" not in files or "NOAA20" not in files or str(year) > snpp_last[:4]:
            continue
        n = {}
        for p in ("SNPP", "NOAA20"):
            d = pd.read_parquet(files[p], columns=["date", "orbit_group"])
            n[p] = d.loc[d["orbit_group"].astype(str) == "afternoon", "date"].value_counts()
        rows.append(pd.concat(n, axis=1).fillna(0).rename_axis("date").reset_index().assign(region=region))
    if not rows:
        return {}
    c = pd.concat(rows, ignore_index=True)
    c = c[c["date"] <= pd.Timestamp(snpp_last)]
    pooled = c.groupby("date")[["SNPP", "NOAA20"]].sum()
    down = pooled.index[(pooled["NOAA20"] >= GAP_MIN) & (pooled["SNPP"] < GAP_SHARE * pooled["NOAA20"])]
    out = {}
    for region, g in c.groupby("region"):
        hit = g["date"].isin(down) | ((g["NOAA20"] >= GAP_MIN) & (g["SNPP"] == 0))
        if hit.any():
            out[region] = pd.DatetimeIndex(g.loc[hit, "date"]).unique().sort_values()
    return out


def day_ranges(days: pd.DatetimeIndex) -> list[str]:
    """Consecutive days written as ranges, e.g. ['2024-07-24..2024-08-01']."""
    out, run = [], []
    for d in pd.DatetimeIndex(days).sort_values():
        if run and (d - run[-1]).days > 1:
            out.append(run)
            run = []
        run.append(d)
    if run:
        out.append(run)
    return [f"{r[0]:%Y-%m-%d}" if len(r) == 1 else f"{r[0]:%Y-%m-%d}..{r[-1]:%Y-%m-%d}" for r in out]


def outage_report(gaps: dict) -> dict:
    """Outage days for validation.json and the web app: per region, and all regions together."""
    every = NO_DAYS.append(list(gaps.values())).unique() if gaps else NO_DAYS
    return {"snpp_outage_days": {r: day_ranges(d) for r, d in gaps.items()},
            "snpp_outage_ranges": day_ranges(every)}


def gaps_from_report(report: dict) -> dict[str, pd.DatetimeIndex]:
    """The outage days written in validation.json by day_ranges, back as dates."""
    out = {}
    for region, ranges in (report.get("snpp_outage_days") or {}).items():
        days = [pd.date_range(*(r.split("..") if ".." in r else (r, r))) for r in ranges]
        out[region] = days[0].append(days[1:]) if days else NO_DAYS
    return out


def build_tables(store: Path, parts: dict, snpp_last: str, out: Path, gaps: dict | None = None) -> dict:
    """Per region-year: fit tables for each source, monthly Suomi NPP truth, fire-start counts.

    Read a month at a time (the tables are additive and a bootstrap block never spans two months),
    so the memory stays small even for the densest savanna years. Suomi NPP outage days are left out.
    """
    tables = {s: [] for s in SOURCES}
    truth, truth_local, fire_counts = [], [], []
    gaps = gaps or {}
    for (region, year), files in parts.items():
        if "SNPP" not in files or year < 2012 or str(year) > snpp_last[:4]:
            continue
        t0 = time.time()
        n_ref = 0
        pieces = {s: ([], []) for s in SOURCES}
        gap = gaps.get(region, NO_DAYS)
        for month in range(1, 13):
            flt = [("month", "==", month)]
            ref = read_partition(store, region, year, ["SNPP"], REF_COLS, parts, filters=flt)
            ref = ref[(ref["date"] >= "2012-01-20") & (ref["date"] <= snpp_last) & (ref["orbit_group"] == "afternoon")
                      & ~ref["date"].isin(gap)]
            if ref.empty:            # no Suomi NPP data this month: nothing to learn from
                continue
            n_ref += len(ref)
            ref_cd = _cell_day_ref(ref)
            tr = ref.groupby([ref["region"], _ym(ref["date"])]).size().rename("truth").reset_index()
            truth.append(tr.rename(columns={"date": "ym"}))
            tl = ref.groupby([ref["region"], _ym(ref["date"]), _local_cell(ref)]).size().rename("truth").reset_index()
            truth_local.append(tl.rename(columns={"date": "ym"}))
            for source, start in SOURCES.items():
                if source not in files:
                    continue
                src = read_partition(store, region, year, [source], SRC_COLS, parts, filters=flt)
                src = src[~src["date"].isin(gap)]
                t = _join(_cell_day_src(src), ref_cd, max(start, f"{year}-01-01"), snpp_last)
                if len(t) and t["n_src"].sum() > 0 and t["n_ref"].sum() > 0:
                    ratio, miss = H.fit_tables(t)
                    pieces[source][0].append(ratio)
                    pieces[source][1].append(miss)
        for source, (rs, ms) in pieces.items():
            if rs:
                tables[source].append((region, year, pd.concat(rs, ignore_index=True), pd.concat(ms, ignore_index=True)))
        if year in FACTOR_YEARS and "Aqua" in files:
            cols = ["lat", "lon", "date", "orbit_group"]
            ref = read_partition(store, region, year, ["SNPP"], cols, parts)
            ref = ref[(ref["date"] >= "2012-01-20") & (ref["date"] <= snpp_last) & (ref["orbit_group"] == "afternoon")
                      & ~ref["date"].isin(gap)]
            aqua = read_partition(store, region, year, ["Aqua"], cols, parts)
            aqua = aqua[~aqua["date"].isin(gap)]
            fire_counts += _fire_starts(aqua[aqua["orbit_group"] == "afternoon"], region, "Aqua")
            fire_counts += _fire_starts(ref, region, "SNPP")
            del ref, aqua
        log(f"tables {region} {year}: {n_ref:,} Suomi NPP afternoon detections ({time.time() - t0:.0f} s)")
    folder = out / "tables"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for source, items in tables.items():
        for region, year, ratio, miss in items:
            ratio.assign(region_=region, year_=year).to_parquet(folder / f"{source}_{region}_{year}_ratio.parquet")
            miss.assign(region_=region, year_=year).to_parquet(folder / f"{source}_{region}_{year}_miss.parquet")
    truth_df = pd.concat(truth, ignore_index=True) if truth else pd.DataFrame(columns=["region", "ym", "truth"])
    truth_df.to_parquet(folder / "truth.parquet")
    truth_local_df = (pd.concat(truth_local, ignore_index=True) if truth_local
                      else pd.DataFrame(columns=["region", "ym", "lc", "truth"]))
    truth_local_df.to_parquet(folder / "truth_local.parquet")
    fc = pd.DataFrame(fire_counts, columns=["region", "platform", "month", "fires"])
    fc.to_parquet(folder / "fire_counts.parquet")
    return {"tables": tables, "truth": truth_df, "truth_local": truth_local_df, "fire_counts": fc}


def _concat(items, keep=lambda region, year: True):
    sel = [(r, m) for region, year, r, m in items if keep(region, year)]
    if not sel:
        return None
    return pd.concat([r for r, _ in sel], ignore_index=True), pd.concat([m for _, m in sel], ignore_index=True)


def count_factors(fc: pd.DataFrame, min_fires: int = 20) -> pd.DataFrame:
    """How many fires Suomi NPP sees per fire Aqua sees, per region and month (cf. events.event_count_factor)."""
    if fc.empty:
        return pd.DataFrame(columns=["region", "month", "factor", "global_factor"])
    p = fc.pivot_table(index=["region", "month"], columns="platform", values="fires", aggfunc="sum", fill_value=0)
    for col in ("Aqua", "SNPP"):
        if col not in p:
            p[col] = 0
    reg = p.groupby(level="region")[["Aqua", "SNPP"]].sum()
    reg_factor = (reg["SNPP"] / reg["Aqua"].where(reg["Aqua"] >= min_fires)).rename("region_factor")
    glob = float(p["SNPP"].sum() / max(p["Aqua"].sum(), 1))
    p["factor"] = p["SNPP"] / p["Aqua"].where(p["Aqua"] >= min_fires)
    p = p.reset_index().merge(reg_factor.reset_index(), on="region", how="left")
    p["factor"] = p["factor"].fillna(p["region_factor"]).fillna(glob)
    return p[["region", "month", "factor"]].assign(global_factor=glob)


# --------------------------------------------------------------------------- 3-4. fit and validate

def _predict_monthly(cal: H.Calibration, src: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Predicted reference counts per region-month and per 1-degree cell-month."""
    cols = ["region", "ym", "pred", "pred_lo", "pred_hi", "raw_source"]
    w = H.apply_weights(src, cal)
    if w.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols + ["lc"])
    w["ym"] = _ym(w["date"])
    w["region"] = w["region"].astype(str)
    w["lc"] = _local_cell(w)
    out = []
    for keys in (["region", "ym"], ["region", "ym", "lc"]):
        p = H.interval_sum(w, keys)
        raw = w.groupby(keys).size().rename("raw_source")
        out.append(pd.concat([p, raw], axis=1).reset_index())
    return out[0], out[1]


def _validate(store, parts, cals: dict, source, regions, start, end, truth, truth_local=None,
              gaps: dict | None = None) -> dict:
    """Monthly error on [start, end]: whole regions, and 1-degree cells (~100 km, like a drawn area).

    `cals` maps a test name to a calibration: the source data is read once for all of them.
    Suomi NPP outage days are left out (the truth was built without them too).
    Returns {name: (metrics, monthly table)}.
    """
    gaps = gaps or {}
    preds = {name: ([], []) for name in cals}
    for (region, year), files in parts.items():
        if region not in regions or source not in files or not (start[:4] <= str(year) <= end[:4]):
            continue
        gap = gaps.get(region, NO_DAYS)
        for month in range(1, 13):          # a month at a time: same result, a twelfth of the memory
            src = read_partition(store, region, year, [source], SRC_COLS, parts, filters=[("month", "==", month)])
            src = src[(src["date"] >= start) & (src["date"] <= end) & ~src["date"].isin(gap)]
            if src.empty:
                continue
            for name, cal in cals.items():
                p, pl = _predict_monthly(cal, src)
                preds[name][0].append(p)
                preds[name][1].append(pl)
    lo_ym, hi_ym = _ym(pd.Series([pd.Timestamp(start)]))[0], _ym(pd.Series([pd.Timestamp(end)]))[0]

    def joined(pred, tr, keys):
        m = pred.merge(tr, on=keys, how="outer").fillna(0)
        return m[(m["ym"] >= lo_ym) & (m["ym"] <= hi_ym) & m["region"].isin(regions)]

    out = {}
    for name, (p, pl) in preds.items():
        if not p:
            out[name] = ({"months": 0}, pd.DataFrame())
            continue
        m = joined(pd.concat(p, ignore_index=True), truth, ["region", "ym"])
        res = H.metrics(m)
        if truth_local is not None and len(truth_local):
            ml = joined(pd.concat(pl, ignore_index=True), truth_local, ["region", "ym", "lc"])
            local = H.metrics(ml)
            res["local_1deg_cells"] = {"cell_months": local.pop("months", 0), **local}
        out[name] = (res, m)
    return out


def _split_test(store, parts, aqua, name, keep, train, test, regions, truth, truth_local, gaps, n_boot, out,
                csv_name=None) -> dict:
    """Fit Aqua -> Suomi NPP on the years `keep` selects, with and without the pass-hour bins (ablation),
    and measure both on the `test` dates."""
    tables = _concat(aqua, keep)
    if not tables:
        return {}
    cals = {name: H.fit_from_tables(*tables, "Aqua", n_boot=n_boot),
            f"{name}_no_hour_bin": H.fit_from_tables(*tables, "Aqua", n_boot=n_boot, no_hour=True)}
    report = {}
    for k, (res, m) in _validate(store, parts, cals, "Aqua", regions, *test, truth, truth_local, gaps).items():
        report[k] = {"train": train, "test": f"{test[0]}..{test[1]}", **res}
        for r in regions:
            if len(m):
                report[k][f"region_{r}"] = H.metrics(m[m["region"] == r])
        if csv_name and k == name and len(m):
            m.to_csv(out / csv_name, index=False)
    return report


def fit_and_validate(store, parts, t: dict, snpp_last: str, out: Path, n_boot: int,
                     gaps: dict | None = None) -> tuple[dict, dict]:
    tables, truth, truth_local = t["tables"], t["truth"], t.get("truth_local")
    report: dict = {"snpp_last_day": snpp_last}
    aqua = tables["Aqua"]
    if not aqua:
        raise SystemExit("No Aqua / Suomi NPP overlap in the store: download MODIS and VIIRS_SNPP first.")
    regions = sorted({r for r, _, _, _ in aqua})
    # Time split: fit up to 2022, test on 2023 onwards, when Aqua's pass had drifted later in the day.
    log(f"fit: Aqua -> Suomi NPP on 2012-{TRAIN_END_YEAR} (time-split test), with and without pass-hour bins")
    report.update(_split_test(store, parts, aqua, "Aqua_time_split_test", lambda r, y: y <= TRAIN_END_YEAR,
                              f"2012-01-20..{TRAIN_END_YEAR}-12-31", (f"{TRAIN_END_YEAR + 1}-01-01", snpp_last),
                              regions, truth, truth_local, gaps, n_boot, out, "validation_time_split_monthly.csv"))
    # Early years: fit on 2016 onwards (drifted years included), test on 2012-2015, when Aqua still flew
    # at 13:30 as it did in 2002-2012, the years the calendar takes from Aqua.
    log(f"fit: Aqua -> Suomi NPP on {EARLY_TEST_END_YEAR + 1}-{snpp_last[:4]} (early-years test)")
    report.update(_split_test(store, parts, aqua, "Aqua_early_years_test", lambda r, y: y > EARLY_TEST_END_YEAR,
                              f"{EARLY_TEST_END_YEAR + 1}-01-01..{snpp_last}",
                              ("2012-01-20", f"{EARLY_TEST_END_YEAR}-12-31"),
                              regions, truth, truth_local, gaps, n_boot, out, "validation_early_years_monthly.csv"))
    if len(regions) > 1:
        for r in regions:
            log(f"fit: leave-one-region-out, {r} never seen")
            rest = _concat(aqua, lambda region, y, r=r: region != r)
            if not rest:
                continue
            cal = H.fit_from_tables(*rest, "Aqua", n_boot=max(n_boot // 4, 20))
            name = f"Aqua_unseen_region_{r}"
            report[name] = _validate(store, parts, {name: cal}, "Aqua", [r], "2012-01-20", snpp_last, truth,
                                     truth_local, gaps)[name][0]
    cals = {}
    for source in SOURCES:
        if tables[source]:
            log(f"fit: final {source} -> Suomi NPP on all overlap data")
            cals[source] = H.fit_from_tables(*_concat(tables[source]), source, n_boot=n_boot,
                                             meta={"start": SOURCES[source], "end": snpp_last})
    for source in ("NOAA20", "Terra"):
        if source in cals:
            name = f"{source}_in_sample_check"
            report[name] = _validate(store, parts, {name: cals[source]}, source, regions, SOURCES[source],
                                     snpp_last, truth, truth_local, gaps)[name][0]
    model = out / "calibration"
    shutil.rmtree(model, ignore_errors=True)
    for c in cals.values():
        c.save(model)
    report["global_ratios"] = {k: round(c.global_ratio, 3) for k, c in cals.items()}
    report["global_missed_share"] = {k: round(c.global_missed, 3) for k, c in cals.items()}
    return report, cals


# --------------------------------------------------------------------------- 5. harmonize

def _backbone_rows(store, parts, region, year, platform, start, end, cals, columns=HARM_COLS, month=None,
                   gaps: dict | None = None):
    """Backbone hotspots of one platform in one region-year, with their weights.

    On Suomi NPP outage days (`gaps`), NOAA-20 hotspots stand in for the missing Suomi NPP ones,
    weighted with the NOAA-20 -> Suomi NPP calibration (same orbit, 50 minutes ahead).
    """
    s, e = max(start, f"{year}-01-01"), min(end, f"{year}-12-31")
    files = parts.get((region, year), {})
    if s > e or platform not in files:
        return None
    filters = [("month", "==", month)] if month else None

    def read(p):
        d = pd.read_parquet(files[p], columns=columns, filters=filters)
        d.insert(0, "region", region)
        d = d[(d["date"] >= s) & (d["date"] <= e)]
        return d if p == "Terra" else d[d["orbit_group"] == "afternoon"]

    d = read(platform)
    fill = None
    if platform == H.REFERENCE:
        gap = (gaps or {}).get(region, NO_DAYS)
        gap = gap[(gap >= s) & (gap <= e)]
        if len(gap) and "NOAA20" in files and "NOAA20" in cals:
            d = d[~d["date"].isin(gap)]
            n20 = read("NOAA20")
            n20 = n20[n20["date"].isin(gap)]
            if len(n20):
                fill = H.apply_weights(n20, cals["NOAA20"]).assign(in_reference=True, low_confidence_period=False)
        d = d.assign(weight=1.0, weight_lo=1.0, weight_hi=1.0, weight_level=0)
    elif len(d):
        if platform not in cals:
            raise SystemExit(f"No calibration for {platform}: is the {platform} overlap with Suomi NPP downloaded?")
        d = H.apply_weights(d, cals[platform])
    d = d.assign(in_reference=True, low_confidence_period=(platform == "Terra"))
    if fill is not None:
        d = pd.concat([x for x in (d, fill) if len(x)], ignore_index=True)
    return d if len(d) else None


def harmonize_partition(store, parts, region, year, cals, bb, factors, grid, recent_from, gaps=None):
    """Weights, monthly cube, events and recent hotspots of one region-year."""
    files = parts[(region, year)]
    cube = []
    for platform, path in files.items():
        d = pd.read_parquet(path, columns=["lat", "lon", "month"])
        c = pd.DataFrame({"cell": grid_cell(d["lat"], d["lon"], grid), "month": d["month"].astype(int)})
        cube.append(c[c["cell"] >= 0].value_counts().rename(f"raw_{platform}"))
    hs = [_backbone_rows(store, parts, region, year, p, s, e, cals, gaps=gaps) for p, s, e in bb]
    hs = [h for h in hs if h is not None]
    ev = pd.DataFrame()
    recent = pd.DataFrame()
    if hs:
        h = pd.concat(hs, ignore_index=True)
        del hs
        h["cell"] = grid_cell(h["lat"], h["lon"], grid)
        g = pd.DataFrame({"cell": h["cell"], "month": h["month"].astype(int), "pred": h["weight"],
                          "lo": h["weight_lo"], "hi": h["weight_hi"], "w2": h["weight"] ** 2})
        cube.append(g[g["cell"] >= 0].groupby(["cell", "month"]).sum())
        h_ev, ev = E.detect_events(h)
        if len(ev):
            ev["uid"] = np.int64(year) * 10_000_000 + ev["event_id"].astype("int64")
            ev["cell"] = grid_cell(ev["lat"], ev["lon"], grid)
            ev["month"] = ev["start"].dt.month
            f = ev[["month"]].assign(region=region).merge(factors, on=["region", "month"], how="left")["factor"]
            glob = float(factors["global_factor"].iloc[0]) if len(factors) else 1.0
            ev["count_weight"] = np.where(ev["main_sensor"].astype(str).eq("MODIS"), f.fillna(glob).to_numpy(), 1.0)
            fires = (ev[ev["cell"] >= 0].groupby(["cell", "month"])
                     .agg(fires=("count_weight", "sum"), fires_raw=("count_weight", "size")))
            cube.append(fires)
        if recent_from is not None and h["date"].max() >= recent_from:
            r = h_ev[h_ev["date"] >= recent_from]
            recent = pd.DataFrame({"time_utc": r["time_utc"], "lat": r["lat"], "lon": r["lon"],
                                   "platform": r["platform"].astype(str), "daynight": r["daynight"].astype(str),
                                   "confidence": r["confidence"].astype(str), "frp": r["frp"],
                                   "weight": r["weight"], "weight_lo": r["weight_lo"], "weight_hi": r["weight_hi"],
                                   "event_uid": np.int64(year) * 10_000_000 + r["event_id"].astype("int64")})
    out = pd.concat(cube, axis=1).fillna(0).reset_index() if cube else pd.DataFrame()
    if len(out):
        out.insert(0, "year", year)
    return out, ev, recent


def month_to_date(store, parts, region, cals, bb, month: int, day: int, grid, gaps=None) -> pd.DataFrame:
    """Harmonized hotspots from the 1st to `day` of `month`, every year, per grid cell."""
    rows = []
    for (r, year) in parts:
        if r != region:
            continue
        hs = [_backbone_rows(store, parts, region, year, p, s, e, cals, month=month, gaps=gaps) for p, s, e in bb]
        hs = [h for h in hs if h is not None]
        if not hs:
            continue
        h = pd.concat(hs, ignore_index=True)
        h = h[h["date"].dt.day <= day]
        g = pd.DataFrame({"cell": grid_cell(h["lat"], h["lon"], grid), "pred": h["weight"], "lo": h["weight_lo"],
                          "hi": h["weight_hi"], "w2": h["weight"] ** 2})
        g = g[g["cell"] >= 0].groupby("cell").sum().reset_index()
        rows.append(g.assign(year=year))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["cell", "pred", "lo", "hi", "w2", "year"])


# --------------------------------------------------------------------------- 6. web export

def _r(a, nd: int = 2) -> list:
    """Rounded numbers for JSON; whole numbers are written without '.0' (smaller files)."""
    v = np.round(np.nan_to_num(np.asarray(a, dtype=float)), nd).tolist()
    return [int(x) if x.is_integer() else x for x in v]


def _i(a) -> list:
    return [int(v) for v in a]


def _read_dir(folder: Path) -> pd.DataFrame:
    """Concatenate the yearly files of a folder (their columns can differ: new satellites appear)."""
    files = sorted(Path(folder).glob("*.parquet")) if Path(folder).exists() else []
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True).fillna(0) if files else pd.DataFrame()


def write_web(web: Path, model: Path, regions: list[str], grids: dict, last_day: pd.Timestamp, report: dict,
              mtd: dict, bb) -> None:
    data = web / "data"
    data.mkdir(parents=True, exist_ok=True)
    meta = {"generated": datetime.now().isoformat(timespec="minutes"), "last_day": str(last_day.date()),
            "month_to_date": {"month": int(last_day.month), "day": int(last_day.day)},
            "backbone": [{"platform": p, "start": s, "end": min(e, str(last_day.date()))} for p, s, e in bb],
            "platforms": PLATFORMS, "regions": {}, "validation": report}
    for region in regions:
        cube = _read_dir(model / "cube" / region)
        cube = cube[cube["year"].astype(int) * 100 + cube["month"].astype(int) <= last_day.year * 100 + last_day.month]
        cube = cube.assign(t=(cube["year"].astype(int) - 2000) * 12 + cube["month"].astype(int) - 1)
        cube = cube.sort_values(["t", "cell"]).reset_index(drop=True)
        raw_cols = [f"raw_{p}" for p in PLATFORMS if f"raw_{p}" in cube]
        for old_file in data.glob(f"{region}_cube*.json"):
            old_file.unlink()
        n_parts = max(1, int(np.ceil(len(cube) / CUBE_ROWS_PER_FILE)))
        for k in range(n_parts):
            c = cube.iloc[k * CUBE_ROWS_PER_FILE:(k + 1) * CUBE_ROWS_PER_FILE]
            zero = pd.Series(0, index=c.index)
            payload = {"cell": _i(c["cell"]), "t": _i(c["t"]), "pred": _r(c.get("pred", zero), 1),
                       "lo": _r(c.get("lo", zero), 1), "hi": _r(c.get("hi", zero), 1), "w2": _r(c.get("w2", zero), 2),
                       "fires": _r(c.get("fires", zero), 2), "fires_raw": _i(c.get("fires_raw", zero)),
                       "raw": {col[4:]: _i(c[col]) for col in raw_cols}}
            (data / f"{region}_cube_{k}.json").write_text(json.dumps(payload, separators=(",", ":")))
        m = mtd.get(region)
        if m is not None and len(m):
            (data / f"{region}_mtd.json").write_text(json.dumps(
                {"cell": _i(m["cell"]), "year": _i(m["year"]), "pred": _r(m["pred"]), "lo": _r(m["lo"]),
                 "hi": _r(m["hi"]), "w2": _r(m["w2"], 3)}, separators=(",", ":")))
        ev = _read_dir(model / "events" / region)
        if len(ev):
            ev = ev.sort_values("hotspots", ascending=False)
            current = ev[ev["end"] >= last_day - pd.Timedelta(days=CURRENT_FIRE_DAYS)]
            notable = pd.concat([ev.head(NOTABLE_EVENTS), current]).drop_duplicates("uid").sort_values("start")
            threshold = float(ev["hotspots"].iloc[min(NOTABLE_EVENTS, len(ev)) - 1])
            (data / f"{region}_events.json").write_text(json.dumps(
                {"threshold": round(threshold, 2), "total": int(len(ev)),
                 "uid": _i(notable["uid"]), "lat": _r(notable["lat"], 3), "lon": _r(notable["lon"], 3),
                 "start": _i((notable["start"] - EPOCH).dt.days), "end": _i((notable["end"] - EPOCH).dt.days),
                 "size": _r(notable["hotspots"], 1), "lo": _r(notable["hotspots_low"], 1),
                 "hi": _r(notable["hotspots_high"], 1), "km2": _r(notable["footprint_km2_approx"], 0),
                 "peak": _r(notable["peak_day_hotspots"], 1),
                 "sensor": [str(s) for s in notable["main_sensor"]]}, separators=(",", ":")))
        rec_path = model / "recent" / f"{region}.parquet"
        if rec_path.exists():
            rec = pd.read_parquet(rec_path)
            rec = rec[rec["time_utc"].dt.tz_convert(None) >= last_day - pd.Timedelta(days=RECENT_DAYS - 1)]
            (data / f"{region}_recent.json").write_text(json.dumps(
                {"t": _i(rec["time_utc"].astype("int64") // 60_000_000_000), "lat": _r(rec["lat"], 4),
                 "lon": _r(rec["lon"], 4), "platform": list(rec["platform"]), "daynight": list(rec["daynight"]),
                 "confidence": list(rec["confidence"]), "frp": _r(rec["frp"], 1), "w": _r(rec["weight"], 3),
                 "wlo": _r(rec["weight_lo"], 3), "whi": _r(rec["weight_hi"], 3), "fire": _i(rec["event_uid"])},
                separators=(",", ":")))
        years = sorted(cube["year"].astype(int).unique())
        meta["regions"][region] = {"label": REGION_LABELS.get(region, region), **grids[region], "cube_parts": n_parts,
                                   "first_year": int(years[0]) if years else None,
                                   "last_year": int(years[-1]) if years else None}
    (data / "meta.json").write_text(json.dumps(meta, indent=1, default=str))


# --------------------------------------------------------------------------- orchestration

def snpp_last_day(parts: dict) -> str | None:
    years = sorted({y for (r, y), files in parts.items() if "SNPP" in files})
    if not years:
        return None
    days = [pd.read_parquet(files["SNPP"], columns=["date"])["date"].max()
            for (r, y), files in parts.items() if y == years[-1] and "SNPP" in files]
    return str(min(max(days), pd.Timestamp("2026-11-01")).date())


def last_data_day(parts: dict) -> pd.Timestamp:
    last_year = max(y for _, y in parts)
    return max(pd.read_parquet(p, columns=["date"])["date"].max()
               for (r, y), files in parts.items() if y == last_year for p in files.values())


def build(raw: Path, store: Path, model: Path, web: Path, skip_ingest=False, years=None, workers=2,
          n_boot=200, grid_deg=GRID_DEG) -> dict:
    t_start = time.time()
    model.mkdir(parents=True, exist_ok=True)
    if not skip_ingest:
        log("1/6 ingest" + (f" (years {sorted(years)})" if years else ""))
        rep = ingest(raw, store, years=years, workers=workers)
        rep.to_csv(store / "_ingest_report.csv", index=False)
    parts = partitions(store)
    if not parts:
        raise SystemExit(f"Nothing in {store}: download data first (bash run_overnight.sh).")
    regions = sorted({r for r, _ in parts})
    snpp_last = snpp_last_day(parts)
    if not snpp_last:
        raise SystemExit("No Suomi NPP data in the store: it is the reference of the harmonized series.")
    bb = H.backbone(snpp_last)
    gaps = snpp_gap_days(parts, snpp_last)
    for r in regions:
        days = gaps.get(r, NO_DAYS)
        log(f"Suomi NPP outage days in {r}: {len(days)}" + (f", NOAA-20 fills them: {', '.join(day_ranges(days))}"
                                                             if len(days) else ""))
    cal_dir = model / "calibration"
    if years and (cal_dir / "Aqua_to_SNPP_meta.json").exists():
        log("2-4/6 reusing the fitted model (incremental run)")
        cals = {s: H.Calibration.load(cal_dir, s) for s in SOURCES if (cal_dir / f"{s}_to_SNPP_meta.json").exists()}
        report = json.loads((model / "validation.json").read_text())
        report.update(outage_report(gaps))
        (model / "validation.json").write_text(json.dumps(report, indent=2, default=str))
        factors = pd.read_parquet(model / "count_factors.parquet")
    else:
        log("2/6 fit tables, one region-year at a time")
        t = build_tables(store, parts, snpp_last, model, gaps)
        log("3-4/6 fit and validate")
        report, cals = fit_and_validate(store, parts, t, snpp_last, model, n_boot, gaps)
        factors = count_factors(t["fire_counts"])
        factors.to_parquet(model / "count_factors.parquet")
        report["fire_count_factor_global"] = round(float(factors["global_factor"].iloc[0]), 3) if len(factors) else None
        report.update(outage_report(gaps))
        (model / "validation.json").write_text(json.dumps(report, indent=2, default=str))
        for k in ("Aqua_time_split_test", "Aqua_time_split_test_no_hour_bin", "Aqua_early_years_test",
                  "Aqua_early_years_test_no_hour_bin"):
            v = report.get(k, {})
            if v.get("months"):
                log(f"validation {k}: {v['median_abs_pct_error_harmonized']}% typical monthly error, "
                    f"total {v['total_bias_pct']:+}%, 95% range holds the truth in {v['interval_coverage_pct']}% "
                    f"of {v['months']} months")
    last_day = last_data_day(parts)
    recent_from = last_day - pd.Timedelta(days=RECENT_DAYS - 1)
    grids = {r: region_grid(r, grid_deg) for r in regions}
    log("5/6 harmonize every region-year")
    for region in regions:
        for sub in ("cube", "events"):
            if not years:
                shutil.rmtree(model / sub / region, ignore_errors=True)
            (model / sub / region).mkdir(parents=True, exist_ok=True)
        recent_parts = []
        for (r, year) in parts:
            if r != region or (years and year not in years and year < recent_from.year):
                continue
            t0 = time.time()
            cube, ev, recent = harmonize_partition(store, parts, region, year, cals, bb, factors, grids[region],
                                                   recent_from, gaps)
            if len(cube):
                cube.to_parquet(model / "cube" / region / f"{year}.parquet", index=False)
            if len(ev):
                ev.drop(columns=["region"]).to_parquet(model / "events" / region / f"{year}.parquet", index=False)
            if len(recent):
                recent_parts.append(recent)
            log(f"harmonized {region} {year}: {len(cube):,} cube cells, {len(ev):,} fires ({time.time() - t0:.0f} s)")
        if recent_parts:
            (model / "recent").mkdir(parents=True, exist_ok=True)
            pd.concat(recent_parts, ignore_index=True).to_parquet(model / "recent" / f"{region}.parquet", index=False)
    log(f"6/6 web data (month to date: 1-{last_day.day} {last_day:%B})")
    mtd = {r: month_to_date(store, parts, r, cals, bb, last_day.month, last_day.day, grids[r], gaps) for r in regions}
    write_web(web, model, regions, grids, last_day, report, mtd, bb)
    log(f"done in {(time.time() - t_start) / 60:.0f} min: web data in {web / 'data'}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--store", type=Path, default=Path("data/store"))
    ap.add_argument("--model", type=Path, default=Path("data/model"))
    ap.add_argument("--web", type=Path, default=Path("web"))
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--years", type=int, nargs="+", help="incremental run: re-ingest and re-harmonize these years")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--n-boot", type=int, default=200)
    ap.add_argument("--grid", type=float, default=GRID_DEG, help="web cube cell size in degrees")
    ap.add_argument("--pid-file", type=Path, help="write this process id there while the build runs")
    args = ap.parse_args()
    if args.pid_file:
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text(str(os.getpid()))
        atexit.register(lambda: args.pid_file.unlink(missing_ok=True))
    build(args.raw, args.store, args.model, args.web, args.skip_ingest, set(args.years) if args.years else None,
          args.workers, args.n_boot, args.grid)


if __name__ == "__main__":
    main()
