"""Load FIRMS hotspot files (MODIS C6.1, VIIRS 375 m) into one clean, partitioned Parquet store.

Usage:
    python -m ember_bridge.ingest --raw data/raw --out data/store [--years 2026] [--workers 2]

Built for hundreds of millions of detections on a laptop. Work is split by region, year and
satellite family (one region-year-family is at most ~15 million rows, about 1 GB in memory), and
every piece is cleaned on its own: no step ever holds a whole region.

Expected layout (one folder per region, any FIRMS CSV, CSV.GZ or ZIP inside, from the Archive
Download page or from ember_bridge.firms_api):
    data/raw/canada/api/MODIS_SP_2016-05-04_5.csv.gz      (area API windows: year read from the name)
    data/raw/canada/nrt/VIIRS_NOAA20_NRT_latest.csv.gz    (last days)
    data/raw/canada/fire_archive_M-C61_123.csv            (any other FIRMS file: read once, staged)
Output:
    data/store/region=<region>/year=<year>/<platform>.parquet
    data/store/_ingest_report.csv
"""
from __future__ import annotations

import argparse
import re
import shutil
import zipfile
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# FIRMS writes the platform in several ways depending on product and vintage.
PLATFORM_MAP = {
    "terra": "Terra", "t": "Terra",
    "aqua": "Aqua", "a": "Aqua",
    "n": "SNPP", "npp": "SNPP", "suomi npp": "SNPP", "snpp": "SNPP", "suomi-npp": "SNPP",
    "n20": "NOAA20", "noaa-20": "NOAA20", "noaa20": "NOAA20", "noaa 20": "NOAA20", "1": "NOAA20", "j1": "NOAA20",
    "n21": "NOAA21", "noaa-21": "NOAA21", "noaa21": "NOAA21", "noaa 21": "NOAA21", "2": "NOAA21", "j2": "NOAA21",
}
SENSOR_OF = {"Terra": "MODIS", "Aqua": "MODIS", "SNPP": "VIIRS", "NOAA20": "VIIRS", "NOAA21": "VIIRS"}
FAMILY_OF = {"Terra": "MODIS", "Aqua": "MODIS", "SNPP": "VIIRS_SNPP", "NOAA20": "VIIRS_NOAA20",
             "NOAA21": "VIIRS_NOAA21"}

# FIRMS "type": 0 presumed vegetation fire, 1 active volcano, 2 other static land source, 3 offshore.
# Near-real-time files have no "type" column: see the static-source mask in ingest().
VEGETATION_TYPE = 0
NON_VEGETATION_TYPES = (1, 2, 3)
STATIC_CELL_DEG = 0.01   # ~1 km cells for the static-source mask (plus their 8 neighbours)

FIXED_CATEGORIES = {
    "sensor": ["MODIS", "VIIRS"],
    "platform": ["Terra", "Aqua", "SNPP", "NOAA20", "NOAA21"],
    "orbit_group": ["morning", "afternoon"],
    "daynight": ["D", "N"],
    "confidence": ["low", "nominal", "high"],
}
STORE_COLUMNS = ["time_utc", "date", "month", "lat", "lon", "sensor", "platform", "orbit_group", "solar_hour",
                 "daynight", "confidence", "frp", "pixel_area_km2", "is_nrt"]
OUT_COLUMNS = ["region", "year"] + STORE_COLUMNS     # region and year come from the folder names
READ_DTYPES = {"acq_date": str, "acq_time": str, "satellite": str, "confidence": str, "version": str,
               "daynight": str}
API_FILE = re.compile(r"^(?P<source>[A-Z0-9]+(?:_[A-Z0-9]+)*?)_(?P<tag>\d{4}-\d{2}-\d{2}_\d+|latest)\.csv(?:\.gz)?$")


# --------------------------------------------------------------------------- reading

def _iter_tables(path: Path, chunksize: int = 2_000_000):
    """Yield the rows of a FIRMS CSV (plain or .csv.gz) or of every CSV inside a ZIP, in chunks."""
    if path.stat().st_size == 0:
        return
    name = path.name.lower()
    sources = []
    if name.endswith((".csv", ".csv.gz")):
        sources = [path]
    elif name.endswith(".zip"):
        z = zipfile.ZipFile(path)
        sources = [z.open(m) for m in z.namelist() if m.lower().endswith(".csv")]
    for src in sources:
        try:
            with pd.read_csv(src, dtype=READ_DTYPES, chunksize=chunksize) as reader:
                for chunk in reader:
                    if len(chunk):
                        yield chunk
        except pd.errors.EmptyDataError:   # a window with no fire at all
            continue


def _read_any(path: Path) -> list[pd.DataFrame]:
    return list(_iter_tables(path))


def _map_unique(s: pd.Series, func) -> pd.Series:
    """Apply func once per distinct value (FIRMS columns have few), not once per row."""
    s = s.astype(str)
    return s.map({u: func(u) for u in pd.unique(s)})


def _minutes(hhmm: str) -> float:
    v = hhmm.strip().replace(":", "")
    try:
        v = int(float(v))
    except ValueError:
        return np.nan
    return (v // 100) * 60 + v % 100


def _confidence_modis(u: str) -> str:
    try:
        v = float(u)
    except ValueError:
        return "nominal"
    return "low" if v < 30 else "high" if v >= 80 else "nominal"   # MODIS active fire user guide


def _confidence_viirs(u: str) -> str:
    c = u.strip().lower()
    return "low" if c.startswith("l") else "high" if c.startswith("h") else "nominal"


def _normalize_confidence(conf: pd.Series, sensor: pd.Series) -> pd.Series:
    """Map MODIS 0-100 and VIIRS l/n/h to one scale: low / nominal / high."""
    modis = _map_unique(conf, _confidence_modis)
    viirs = _map_unique(conf, _confidence_viirs)
    return pd.Series(np.where(np.asarray(sensor) == "MODIS", modis, viirs), index=conf.index)


def solar_hour(time_utc: pd.Series, lon: pd.Series) -> pd.Series:
    """Local solar time of the overpass, night passes folded onto their day counterpart.

    Terra ~10:30 (night 22:30 -> 10:30), Aqua and VIIRS ~13:30 (night 01:30 -> 13:30).
    Using the real local time (not the nominal one) lets the model see Terra and Aqua orbit drift.
    """
    hours = time_utc.dt.hour + time_utc.dt.minute / 60.0
    solar = (hours + lon / 15.0) % 24
    folded = np.where(solar >= 18, solar - 12, np.where(solar < 6, solar + 12, solar))
    return pd.Series(folded, index=time_utc.index)


def orbit_group(time_utc: pd.Series, lon: pd.Series) -> pd.Series:
    """'morning' (Terra-like) or 'afternoon' (Aqua/VIIRS-like) from local solar time."""
    return pd.Series(np.where(solar_hour(time_utc, lon) < 12, "morning", "afternoon"), index=time_utc.index)


def _cat(values, column: str) -> pd.Categorical:
    return pd.Categorical(values, categories=FIXED_CATEGORIES[column])


def normalize(df: pd.DataFrame, region: str) -> pd.DataFrame:
    """Turn one raw FIRMS table (either sensor, archive or NRT) into the common compact schema."""
    df = df.rename(columns=str.lower)
    platform = _map_unique(df["satellite"], lambda u: PLATFORM_MAP.get(u.strip().lower()))
    if platform.isna().any():
        unknown = sorted(df["satellite"][platform.isna()].astype(str).unique())[:5]
        raise ValueError(f"Unknown satellite codes in {region}: {unknown}")
    day = pd.to_datetime(df["acq_date"].astype(str).str.slice(0, 10), format="%Y-%m-%d")
    minutes = _map_unique(df["acq_time"], _minutes).astype("float64")
    time_utc = (day + pd.to_timedelta(minutes, unit="m")).dt.tz_localize("UTC")
    sensor = platform.map(SENSOR_OF)
    lon = df["longitude"].astype("float64")
    version = df["version"] if "version" in df else pd.Series("", index=df.index)
    daynight = _map_unique(df["daynight"], lambda u: u.strip().upper()[:1]) if "daynight" in df else "D"
    scan = pd.to_numeric(df.get("scan"), errors="coerce")
    track = pd.to_numeric(df.get("track"), errors="coerce")
    sh = solar_hour(time_utc, lon)
    out = pd.DataFrame({
        "region": region,
        "year": time_utc.dt.year.astype("int16"),
        "time_utc": time_utc,
        "date": day,
        "month": time_utc.dt.month.astype("int8"),
        "lat": df["latitude"].astype("float32"),
        "lon": lon.astype("float32"),
        "sensor": _cat(sensor, "sensor"),
        "platform": _cat(platform, "platform"),
        "orbit_group": _cat(np.where(sh < 12, "morning", "afternoon"), "orbit_group"),
        "solar_hour": sh.astype("float32"),
        "daynight": _cat(daynight if isinstance(daynight, pd.Series) else [daynight] * len(df), "daynight"),
        "confidence": _cat(_normalize_confidence(df["confidence"], sensor), "confidence"),
        "frp": pd.to_numeric(df.get("frp"), errors="coerce").astype("float32"),
        "pixel_area_km2": (scan * track).astype("float32"),
        "is_nrt": _map_unique(version, lambda u: "NRT" in u.upper()).astype(bool),
        "type": pd.to_numeric(df["type"], errors="coerce").astype("float32") if "type" in df
        else np.float32(np.nan),
    })
    return out


def clean(df: pd.DataFrame, keep_low_confidence: bool = False) -> tuple[pd.DataFrame, dict]:
    """Drop non-vegetation sources, NRT rows superseded by science-quality data, duplicates and
    (optionally) low-confidence detections."""
    stats = {"input": len(df)}
    df = df[df["type"].fillna(VEGETATION_TYPE).eq(VEGETATION_TYPE)]
    stats["after_type_filter"] = len(df)
    # FIRMS replaces NRT data with standard (SP) data a few months later. If a platform has SP data for
    # a day, its NRT rows for that day are duplicates with slightly different coordinates: drop them.
    nrt = df["is_nrt"].to_numpy()
    if nrt.any() and (~nrt).any():
        sp = pd.MultiIndex.from_frame(df.loc[~nrt, ["platform", "date"]].drop_duplicates())
        df = df[~(nrt & pd.MultiIndex.from_frame(df[["platform", "date"]]).isin(sp))]
    stats["after_nrt_superseded"] = len(df)
    df = df.drop_duplicates(subset=["platform", "time_utc", "lat", "lon"])
    stats["after_dedup"] = len(df)
    if not keep_low_confidence:
        df = df[df["confidence"] != "low"]
    stats["output"] = len(df)
    return df, stats


# --------------------------------------------------------------------------- static sources

def cell_keys(lat, lon, deg: float = STATIC_CELL_DEG) -> np.ndarray:
    i = np.floor((np.asarray(lat, float) + 90) / deg).astype(np.int64)
    j = np.floor((np.asarray(lon, float) + 180) / deg).astype(np.int64)
    return i * 100_000 + j


def _with_neighbours(keys: np.ndarray) -> np.ndarray:
    i, j = keys // 100_000, keys % 100_000
    return np.unique(np.concatenate([(i + di) * 100_000 + (j + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)]))


# --------------------------------------------------------------------------- partitions

def partitions(store_dir: Path) -> dict[tuple[str, int], dict[str, Path]]:
    """{(region, year): {platform: parquet path}} for everything in the store."""
    out: dict[tuple[str, int], dict[str, Path]] = {}
    for path in Path(store_dir).glob("region=*/year=*/*.parquet"):
        region = path.parent.parent.name.split("=", 1)[1]
        year = int(path.parent.name.split("=", 1)[1])
        out.setdefault((region, year), {})[path.stem] = path
    return dict(sorted(out.items()))


def read_partition(store_dir: Path, region: str, year: int, platforms=None, columns=None,
                   parts: dict | None = None, filters=None) -> pd.DataFrame:
    """One region-year of the store (optionally some platforms, columns or rows), with region and year."""
    files = (parts if parts is not None else partitions(store_dir)).get((region, year), {})
    frames = [pd.read_parquet(p, columns=columns, filters=filters) for name, p in files.items()
              if platforms is None or name in platforms]
    frames = [f for f in frames if len(f)]
    if not frames:
        cols = columns or STORE_COLUMNS
        return pd.DataFrame({c: pd.Series(dtype="object") for c in ["region", "year"] + list(cols)})
    df = pd.concat(frames, ignore_index=True)
    for c, cats in FIXED_CATEGORIES.items():
        if c in df and not isinstance(df[c].dtype, pd.CategoricalDtype):
            df[c] = pd.Categorical(df[c], categories=cats)
        elif c in df and list(df[c].cat.categories) != cats:
            df[c] = pd.Categorical(df[c].astype(str), categories=cats)
    df.insert(0, "region", region)
    df.insert(1, "year", np.int16(year))
    return df


def _family_of_source(source: str) -> str:
    return source.rsplit("_", 1)[0] if source.endswith(("_SP", "_NRT")) else source


def _api_years(path: Path, tag: str) -> set[int]:
    if tag == "latest":
        end = datetime.fromtimestamp(path.stat().st_mtime).date()
        return {end.year, (end - timedelta(days=6)).year}
    day, n = tag.rsplit("_", 1)
    d0 = date.fromisoformat(day)
    return {d0.year, (d0 + timedelta(days=int(n) - 1)).year}


def _scan(region_dir: Path):
    """API files by (year, family), and the other FIRMS files (read in full and staged)."""
    jobs: dict[tuple[int, str], list[Path]] = {}
    other = []
    for path in sorted(region_dir.rglob("*")):
        if not path.is_file() or not path.name.lower().endswith((".csv", ".csv.gz", ".zip")):
            continue
        m = API_FILE.match(path.name)
        if not m:
            other.append(path)
            continue
        family = _family_of_source(m["source"])
        for year in _api_years(path, m["tag"]):
            jobs.setdefault((year, family), []).append(path)
    return jobs, other


def _stage_other(region: str, files: list[Path], stage_dir: Path, years) -> dict[tuple[int, str], list[Path]]:
    staged: dict[tuple[int, str], list[Path]] = {}
    k = 0
    for path in files:
        for raw in _iter_tables(path):
            d = normalize(raw, region)
            fam = d["platform"].astype(str).map(FAMILY_OF)
            for (year, family), g in d.groupby([d["year"], fam], observed=True):
                if years and int(year) not in years:
                    continue
                out = stage_dir / f"{int(year)}_{family}_{k:05d}.parquet"
                out.parent.mkdir(parents=True, exist_ok=True)
                g.to_parquet(out, index=False)
                staged.setdefault((int(year), family), []).append(out)
                k += 1
    return staged


def _process_job(region: str, year: int, family: str, api_files: list[Path], staged: list[Path],
                 out_dir: Path, keep_low_confidence: bool) -> dict:
    """Read, clean and write one region-year-family. Returns stats, static cells and written files."""
    frames = []
    for path in api_files:
        for raw in _iter_tables(path):
            d = normalize(raw, region)
            d = d[d["year"] == year]
            if len(d):
                frames.append(d)
    frames += [pd.read_parquet(p) for p in staged]
    frames = [f for f in frames if len(f)]
    empty = {"stats": {"input": 0, "after_type_filter": 0, "after_nrt_superseded": 0, "after_dedup": 0,
                       "output": 0}, "static": np.array([], dtype=np.int64), "written": [], "by_platform": {}}
    if not frames:
        return empty
    df = pd.concat(frames, ignore_index=True)
    del frames
    bad = df["type"].isin(NON_VEGETATION_TYPES).to_numpy()
    static = np.unique(cell_keys(df["lat"].to_numpy()[bad], df["lon"].to_numpy()[bad]))
    df, stats = clean(df, keep_low_confidence)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for platform, g in df.groupby("platform", observed=True):
        path = out_dir / f"{platform}.parquet"
        g[STORE_COLUMNS].sort_values("time_utc").to_parquet(path, index=False, compression="zstd")
        written.append((str(platform), str(path), bool(g["is_nrt"].any())))
    by_platform = {str(k): int(v) for k, v in df["platform"].value_counts().items() if v}
    return {"stats": stats, "static": static, "written": written, "by_platform": by_platform}


def _apply_static_mask(nrt_files: list[str], static: np.ndarray) -> int:
    """NRT rows carry no 'type': drop those sitting on a static source known from the SP archive
    (gas flares, mines, industrial sites, volcanoes), so the current month is compared fairly."""
    if not len(static) or not nrt_files:
        return 0
    mask = _with_neighbours(static)
    removed = 0
    for path in nrt_files:
        d = pd.read_parquet(path)
        drop = d["is_nrt"].to_numpy() & np.isin(cell_keys(d["lat"], d["lon"]), mask)
        if drop.any():
            d[~drop].to_parquet(path, index=False, compression="zstd")
            removed += int(drop.sum())
    return removed


def ingest(raw_dir: Path, out_dir: Path, keep_low_confidence: bool = False, years=None,
           workers: int = 1, regions=None) -> pd.DataFrame:
    """Ingest every region folder under raw_dir into out_dir/region=<r>/year=<y>/<platform>.parquet.

    years: only rebuild these years (e.g. the current one after a near-real-time update).
    """
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    years = {int(y) for y in years} if years else None
    report = []
    for region_dir in sorted(p for p in raw_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        region = region_dir.name
        if regions and region not in regions:
            continue
        jobs, other = _scan(region_dir)
        if years:
            jobs = {k: v for k, v in jobs.items() if k[0] in years}
        stage_dir = out_dir / "_staging" / region
        shutil.rmtree(stage_dir, ignore_errors=True)
        staged = _stage_other(region, other, stage_dir, years) if other else {}
        keys = sorted(set(jobs) | set(staged))
        if not keys:
            continue
        region_store = out_dir / f"region={region}"
        if years:
            for y in years:
                shutil.rmtree(region_store / f"year={y}", ignore_errors=True)
        else:
            shutil.rmtree(region_store, ignore_errors=True)   # safe re-runs: never duplicate rows
        args = [(region, y, fam, jobs.get((y, fam), []), staged.get((y, fam), []),
                 region_store / f"year={y}", keep_low_confidence) for y, fam in keys]
        if workers > 1:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_process_job, *zip(*args)))
        else:
            results = [_process_job(*a) for a in args]
        shutil.rmtree(stage_dir, ignore_errors=True)
        total = {k: sum(r["stats"][k] for r in results) for k in results[0]["stats"]}
        static = np.unique(np.concatenate([r["static"] for r in results])) if results else np.array([])
        nrt_files = [p for r in results for _, p, has_nrt in r["written"] if has_nrt]
        removed = _apply_static_mask(nrt_files, static)
        total["static_mask_removed"] = removed
        total["output"] -= removed
        by_platform: dict[str, int] = {}
        for r in results:
            for k, v in r["by_platform"].items():
                by_platform[k] = by_platform.get(k, 0) + v
        report.append({"region": region, **total, **by_platform})
        print(f"[{region}] {total} per platform: {by_platform}", flush=True)
    return pd.DataFrame(report)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/store"))
    ap.add_argument("--keep-low-confidence", action="store_true")
    ap.add_argument("--years", type=int, nargs="+", help="only rebuild these years")
    ap.add_argument("--regions", nargs="+")
    ap.add_argument("--workers", type=int, default=2, help="parallel processes (each needs ~1-2 GB)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = ingest(args.raw, args.out, args.keep_low_confidence, args.years, args.workers, args.regions)
    report.to_csv(args.out / "_ingest_report.csv", index=False)


if __name__ == "__main__":
    main()
