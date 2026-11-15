"""From hotspots to fires: group detections into fire events, count fires, and rank the current
fire against every past fire of the area.

The challenge asks for "the number of fires detected" and "how a current fire compares with
previous ones". A hotspot is one hot pixel; one fire produces many of them, and VIIRS's finer
pixels produce more than MODIS's. So we:

1. link hotspots that touch in space (neighbouring ~5 km cells) and time (up to 2 days apart,
   so a fire hidden by clouds for a day stays the same fire) into one event;
2. measure each event in harmonized hotspots (sum of weights), so a MODIS-era fire and a
   VIIRS-era fire are measured with the same ruler;
3. harmonize fire *counts* too: during 2012-2026 both sensors flew, so we count how many fires
   VIIRS sees for each fire MODIS sees, per region and month.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .harmonize import interval_sum

EVENT_CELL_DEG = 0.05   # ~5 km: MODIS pixels are 1-5 km, VIIRS 375 m
MAX_GAP_DAYS = 2


def cluster_labels(lat, lon, dates, cell_deg: float = EVENT_CELL_DEG, max_gap_days: int = MAX_GAP_DAYS) -> np.ndarray:
    """Connected components of hotspots in a (cell, cell, day) grid. Returns one label per hotspot."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    if len(lat) == 0:
        return np.array([], dtype=int)
    days = pd.to_datetime(pd.Series(dates)).to_numpy().astype("datetime64[D]").astype("int64")
    pts = pd.DataFrame({"i": np.floor((lat + 90) / cell_deg).astype("int64"),
                        "j": np.floor((lon + 180) / cell_deg).astype("int64"), "t": days})
    nodes = pts.drop_duplicates().reset_index(drop=True)
    nodes["nid"] = np.arange(len(nodes))
    node_of_pt = pts.merge(nodes, on=["i", "j", "t"], how="left")["nid"].to_numpy()

    src, dst = [], []
    for dt in range(0, max_gap_days + 1):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if dt == 0 and not (di > 0 or (di == 0 and dj > 0)):
                    continue                      # same day: each undirected pair once, no self loops
                shifted = nodes.assign(i=nodes["i"] + di, j=nodes["j"] + dj, t=nodes["t"] + dt)
                pairs = shifted.merge(nodes, on=["i", "j", "t"], suffixes=("_a", "_b"))
                src.append(pairs["nid_a"].to_numpy())
                dst.append(pairs["nid_b"].to_numpy())
    a = np.concatenate(src) if src else np.array([], dtype=int)
    b = np.concatenate(dst) if dst else np.array([], dtype=int)
    n = len(nodes)
    graph = coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n))
    _, lab = connected_components(graph, directed=False)
    return lab[node_of_pt]


def detect_events(sel: pd.DataFrame, cell_deg: float = EVENT_CELL_DEG,
                  max_gap_days: int = MAX_GAP_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group the harmonized hotspots of a selection into fire events.

    Returns (hotspots with an event_id column, one row per event).
    """
    d = sel[sel["in_reference"] & (sel["weight"] > 0)].copy()
    d["date"] = pd.to_datetime(d["date"])
    d["event_id"] = cluster_labels(d["lat"], d["lon"], d["date"], cell_deg, max_gap_days)
    if d.empty:
        return d, pd.DataFrame(columns=["event_id", "start", "end", "duration_days", "hotspots"])
    d["_cell"] = (np.floor((d["lat"] + 90) / cell_deg) * 100_000 + np.floor((d["lon"] + 180) / cell_deg)).astype("int64")
    g = d.groupby("event_id")
    ev = g.agg(start=("date", "min"), end=("date", "max"), n_cells=("_cell", "nunique"),
               frp_max=("frp", "max"), low_confidence_period=("low_confidence_period", "any"),
               region=("region", "first"))
    ev = ev.join(interval_sum(d, ["event_id"]).rename(
        columns={"pred": "hotspots", "pred_lo": "hotspots_low", "pred_hi": "hotspots_high"}))
    w = d["weight"]
    ev["lat"] = (d["lat"] * w).groupby(d["event_id"]).sum() / w.groupby(d["event_id"]).sum()
    ev["lon"] = (d["lon"] * w).groupby(d["event_id"]).sum() / w.groupby(d["event_id"]).sum()
    ev["peak_day_hotspots"] = d.groupby(["event_id", "date"])["weight"].sum().groupby("event_id").max()
    ev["main_sensor"] = d.groupby(["event_id", "sensor"], observed=True)["weight"].sum().unstack(fill_value=0).idxmax(axis=1)
    ev["duration_days"] = (ev["end"] - ev["start"]).dt.days + 1
    cell_km2 = (cell_deg * 111.0) ** 2 * np.cos(np.radians(ev["lat"]))
    ev["footprint_km2_approx"] = ev["n_cells"] * cell_km2
    ev = ev.reset_index()
    return d.drop(columns="_cell"), ev


def current_fire(events: pd.DataFrame, active_days: int = 7) -> int | None:
    """The 'current fire': the biggest fire still burning in the last `active_days` of data."""
    if events.empty:
        return None
    active = events[events["end"] >= events["end"].max() - pd.Timedelta(days=active_days)]
    return int(active.loc[active["hotspots"].idxmax(), "event_id"])


def compare_event(events: pd.DataFrame, event_id: int | None = None) -> dict:
    """Rank one fire (default: the current fire) against every earlier fire of the area."""
    if events.empty:
        return {}
    event_id = current_fire(events) if event_id is None else event_id
    cur = events.set_index("event_id").loc[event_id].copy()
    cur["event_id"] = event_id
    past = events[events["start"] < cur["start"]]
    out = {"event_id": int(cur["event_id"]), "start": str(cur["start"].date()), "end": str(cur["end"].date()),
           "duration_days": int(cur["duration_days"]), "hotspots": float(cur["hotspots"]),
           "hotspots_low": float(cur["hotspots_low"]), "hotspots_high": float(cur["hotspots_high"]),
           "footprint_km2_approx": float(cur["footprint_km2_approx"]), "past_fires": int(len(past))}
    if past.empty:
        return out
    bigger = past[past["hotspots"] >= cur["hotspots"]]
    out["size_percentile"] = round(float((past["hotspots"] < cur["hotspots"]).mean() * 100), 1)
    out["size_rank"] = int(len(bigger) + 1)
    out["duration_percentile"] = round(float((past["duration_days"] < cur["duration_days"]).mean() * 100), 1)
    # the most recent earlier fire at least as big: "the largest since <that date>"
    if len(bigger):
        last = bigger["start"].max()
        recent = (cur["start"] - last).days < 365   # a bigger fire less than a year ago: give its exact day
        out["largest_since"] = (f"the fire of {last.day} {last:%B %Y}" if recent else f"{last:%B %Y}")
    else:
        out["largest_since"] = None
    out["record_start_year"] = int(events["start"].min().year)
    return out


def event_count_factor(df: pd.DataFrame, years=(2013, 2016, 2019, 2022), source: str = "Aqua",
                       reference: str = "SNPP", min_fires: int = 20) -> pd.DataFrame:
    """How many fires VIIRS sees for each fire MODIS sees, per region and month (overlap years).

    Clustering is done separately on each sensor's own detections. A sample of years keeps it fast
    on large regions; the factor is a ratio of totals, so a few full years are plenty.
    """
    d = df[df["orbit_group"] == "afternoon"].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"].dt.year.isin(years)]
    rows = []
    for (region, platform), g in d[d["platform"].isin([source, reference])].groupby(["region", "platform"], observed=True):
        lab = cluster_labels(g["lat"], g["lon"], g["date"])
        starts = pd.Series(g["date"].to_numpy()).groupby(lab).min()
        counts = starts.dt.month.value_counts()
        for month, n in counts.items():
            rows.append({"region": region, "platform": platform, "month": int(month), "fires": int(n)})
    c = pd.DataFrame(rows, columns=["region", "platform", "month", "fires"])
    if c.empty:
        return pd.DataFrame(columns=["region", "month", "factor"])
    p = c.pivot_table(index=["region", "month"], columns="platform", values="fires", aggfunc="sum", fill_value=0,
                     observed=True)
    for col in (source, reference):
        if col not in p:
            p[col] = 0
    reg = p.groupby(level="region")[[source, reference]].sum()
    reg_factor = (reg[reference] / reg[source].where(reg[source] >= min_fires)).rename("region_factor")
    glob = p[reference].sum() / max(p[source].sum(), 1)
    p["factor"] = p[reference] / p[source].where(p[source] >= min_fires)
    p = p.reset_index().merge(reg_factor.reset_index(), on="region", how="left")
    p["factor"] = p["factor"].fillna(p["region_factor"]).fillna(glob)
    return p[["region", "month", "factor"]].assign(global_factor=glob)


def fire_calendar(events: pd.DataFrame, factors: pd.DataFrame | None = None) -> pd.DataFrame:
    """Year x month count of fires (by start month). MODIS-era fires are scaled to VIIRS-equivalent
    counts with event_count_factor, so the count does not jump in 2012 either."""
    if events.empty:
        return pd.DataFrame(columns=["year", "month", "fires", "fires_raw"])
    e = events.assign(year=events["start"].dt.year, month=events["start"].dt.month, w=1.0)
    if factors is not None and len(factors):
        f = e[["region", "month"]].merge(factors[["region", "month", "factor"]], on=["region", "month"], how="left")["factor"]
        f = f.fillna(float(factors["global_factor"].iloc[0])).to_numpy()
        e["w"] = np.where(e["main_sensor"].eq("MODIS"), f, 1.0)
    cal = e.groupby(["year", "month"]).agg(fires=("w", "sum"), fires_raw=("w", "size"))
    idx = pd.MultiIndex.from_product([range(int(e["year"].min()), int(e["year"].max()) + 1), range(1, 13)],
                                     names=["year", "month"])
    return cal.reindex(idx, fill_value=0).reset_index()
