"""Everything the web app shows for a user-selected area and period.

All functions take the harmonized hotspot table (output of harmonize.build_harmonized)
and work on the 'weight' column, so the sensor of origin no longer matters.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .harmonize import interval_sum


# ---------------------------------------------------------------- area selection
def in_polygon(lat: np.ndarray, lon: np.ndarray, polygon: list[tuple[float, float]]) -> np.ndarray:
    """Ray-casting point-in-polygon. polygon = [(lon, lat), ...] as drawn on the map."""
    x, y = np.asarray(lon), np.asarray(lat)
    inside = np.zeros(len(x), dtype=bool)
    pts = list(polygon)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        crosses = ((y1 > y) != (y2 > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
        inside ^= crosses & (x < xint)
    return inside


def circle(lat0: float, lon0: float, radius_km: float, n: int = 48) -> list[tuple[float, float]]:
    """Polygon around a lat/lon point, for users who type coordinates instead of drawing."""
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    dlat = radius_km / 111.0 * np.sin(a)
    dlon = radius_km / (111.0 * np.cos(np.radians(lat0))) * np.cos(a)
    return list(zip(lon0 + dlon, lat0 + dlat))


def select(h: pd.DataFrame, polygon, start=None, end=None) -> pd.DataFrame:
    d = h[in_polygon(h["lat"].to_numpy(), h["lon"].to_numpy(), polygon)]
    d = d.assign(date=pd.to_datetime(d["date"]))
    if start:
        d = d[d["date"] >= pd.Timestamp(start)]
    if end:
        d = d[d["date"] <= pd.Timestamp(end)]
    return d


# ---------------------------------------------------------------- calendar
def calendar(sel: pd.DataFrame, harmonized: bool = True) -> pd.DataFrame:
    """Year x month table of hotspot counts, with 95 % bounds.

    harmonized=False gives the naive raw count of all afternoon detections from whatever
    satellites flew (the 'before' view of the before/after toggle).
    """
    d = sel.assign(year=sel["date"].dt.year, month=sel["date"].dt.month)
    if harmonized:
        cal = interval_sum(d[d["in_reference"]], ["year", "month"])
        cal["low_confidence_period"] = d[d["in_reference"]].groupby(["year", "month"])["low_confidence_period"].any()
    else:
        raw = d[d["orbit_group"] == "afternoon"].groupby(["year", "month"]).size().rename("pred")
        cal = raw.to_frame().assign(pred_lo=raw, pred_hi=raw, low_confidence_period=False)
    years = range(int(d["year"].min()), int(d["year"].max()) + 1) if len(d) else []
    idx = pd.MultiIndex.from_product([years, range(1, 13)], names=["year", "month"])
    cal = cal.reindex(idx).fillna({"pred": 0, "pred_lo": 0, "pred_hi": 0, "low_confidence_period": False})
    width = cal["pred_hi"] - cal["pred_lo"]
    cal["uncertain"] = (cal["pred"] > 0) & (width > cal["pred"])    # interval wider than the value: hatch it
    return cal.reset_index().rename(columns={"pred": "hotspots", "pred_lo": "low", "pred_hi": "high"})


def contributing_satellites(sel: pd.DataFrame) -> pd.DataFrame:
    """For the satellite strip under the calendar: detections per platform and month."""
    d = sel.assign(ym=sel["date"].dt.to_period("M").astype(str))
    return d.groupby(["ym", "platform"]).size().unstack(fill_value=0)


# ---------------------------------------------------------------- insight
def anomaly(cal: pd.DataFrame, year: int, month: int) -> dict:
    """Compare one month with the same month in all previous years: median, mean, rank."""
    cur = cal[(cal["year"] == year) & (cal["month"] == month)]
    hist = cal[(cal["year"] < year) & (cal["month"] == month)]["hotspots"]
    value = float(cur["hotspots"].iloc[0]) if len(cur) else 0.0
    out = {"year": year, "month": month, "value": value,
           "low": float(cur["low"].iloc[0]) if len(cur) else 0.0,
           "high": float(cur["high"].iloc[0]) if len(cur) else 0.0, "median": None}
    if hist.empty:
        return out
    med, mean = float(hist.median()), float(hist.mean())
    out.update({
        "median": med, "mean": mean,
        "ratio_to_median": value / med if med > 0 else None,
        "anomaly_vs_mean": value - mean,
        "pct_vs_mean": (value / mean - 1) * 100 if mean > 0 else None,
        "percentile": float((hist < value).mean() * 100),
        "rank": int((hist >= value).sum()) + 1, "years_compared": int(len(hist)),
        # the whole 95 % range sits above (below) the median: the anomaly is not a sensor artefact
        "significant": bool(out["low"] > med or out["high"] < med),
    })
    return out


def anomaly_grid(cal: pd.DataFrame) -> pd.DataFrame:
    """Every cell of the calendar as % difference to the multi-year mean of its month
    (for the diverging-colour anomaly view)."""
    m = cal.groupby("month")["hotspots"].transform("mean")
    return cal.assign(pct_vs_month_mean=np.where(m > 0, (cal["hotspots"] / m - 1) * 100, np.nan))


def watchlist(h: pd.DataFrame, areas: dict, year: int, month: int) -> pd.DataFrame:
    """Several sensitive areas ranked by how unusual this month is (challenge: 'monitor sensitive regions').

    areas = {"Fort McMurray": [(lon, lat), ...], "Jasper": circle(52.87, -118.08, 50), ...}
    """
    rows = []
    for name, poly in areas.items():
        an = anomaly(calendar(select(h, poly)), year, month)
        rows.append({"area": name, **{k: an.get(k) for k in
                     ["value", "low", "high", "median", "ratio_to_median", "pct_vs_mean", "rank",
                      "years_compared", "significant"]}})
    out = pd.DataFrame(rows)
    return out.sort_values("ratio_to_median", ascending=False, na_position="last").reset_index(drop=True)


def top_periods(cal: pd.DataFrame, n: int = 3) -> dict:
    by_year = cal.groupby("year")["hotspots"].sum().sort_values(ascending=False)
    by_month = cal.groupby("month")["hotspots"].median().sort_values(ascending=False)
    return {"top_years": by_year.head(n).round(0).to_dict(), "peak_months": by_month.head(n).round(0).to_dict()}


def analog_years(cal: pd.DataFrame, year: int, upto_month: int, n: int = 3) -> list[dict]:
    """Past years whose Jan..upto_month profile looks most like this year's (shape and level)."""
    piv = cal.pivot(index="year", columns="month", values="hotspots").fillna(0)
    if year not in piv.index:
        return []
    cols = list(range(1, upto_month + 1))
    cur = piv.loc[year, cols].to_numpy()
    out = []
    for y in piv.index:
        if y >= year:
            continue
        past = piv.loc[y, cols].to_numpy()
        shape = np.corrcoef(cur, past)[0, 1] if cur.std() > 0 and past.std() > 0 else 0.0
        level = 1 - abs(np.log1p(cur.sum()) - np.log1p(past.sum())) / max(np.log1p(cur.sum()), 1)
        rest = piv.loc[y, [m for m in range(upto_month + 1, 13)]].sum()
        out.append({"year": int(y), "similarity": float(0.7 * shape + 0.3 * level),
                    "rest_of_year_hotspots": float(rest)})
    return sorted(out, key=lambda r: r["similarity"], reverse=True)[:n]


def season_timing(sel: pd.DataFrame) -> pd.DataFrame:
    """Per year: day of year when 10 %, 50 % and 90 % of the year's activity had happened, and the peak.

    A shrinking 10 % date or a growing 10-90 % span = an earlier or longer fire season.
    Southern-hemisphere seasons straddle New Year; for those regions use a July-June 'fire year'.
    """
    d = sel[sel["in_reference"]].assign(year=sel["date"].dt.year, doy=sel["date"].dt.dayofyear)
    rows = []
    for y, g in d.groupby("year"):
        daily = g.groupby("doy")["weight"].sum().reindex(range(1, 367), fill_value=0)
        total = daily.sum()
        if total < 20:
            continue
        c = daily.cumsum() / total
        peak = daily.rolling(15, center=True, min_periods=1).sum().idxmax()
        rows.append({"year": int(y), "start_doy": int((c >= .1).idxmax()), "middle_doy": int((c >= .5).idxmax()),
                     "end_doy": int((c >= .9).idxmax()), "peak_doy": int(peak)})
    t = pd.DataFrame(rows)
    if not t.empty:
        t["length_days"] = t["end_doy"] - t["start_doy"]
    return t


def season_trend(timing: pd.DataFrame) -> dict:
    """Days per decade the season start moved and the season length changed (least squares)."""
    if len(timing) < 5:
        return {}
    yrs = timing["year"].to_numpy()
    return {"start_shift_days_per_decade": float(np.polyfit(yrs, timing["start_doy"], 1)[0] * 10),
            "length_change_days_per_decade": float(np.polyfit(yrs, timing["length_days"], 1)[0] * 10)}


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def brief(area_name: str, an: dict, analogs: list[dict], trend: dict, fire: dict | None = None) -> str:
    """Plain-language paragraph for a situation report (English, as required by Space Apps)."""
    m = MONTHS[an["month"] - 1]
    parts = []
    if an.get("median"):
        r = an["ratio_to_median"]
        word = "above" if r >= 1 else "below"
        parts.append(f"{area_name}, {m} {an['year']}: {an['value']:.0f} harmonized hotspots "
                     f"(95% range {an['low']:.0f}-{an['high']:.0f}), {abs(r - 1) * 100:.0f}% {word} the "
                     f"{an['years_compared']}-year median for {m} ({an['median']:.0f}); "
                     f"rank {an['rank']} of {an['years_compared'] + 1}"
                     f"{' - a significant anomaly' if an.get('significant') else ''}.")
    else:
        parts.append(f"{area_name}, {m} {an['year']}: {an['value']:.0f} harmonized hotspots.")
    if fire and fire.get("past_fires"):
        pct = fire["size_percentile"]
        nd = fire["duration_days"]
        head = (f"The current fire ({fire['start']} to {fire['end']}, {nd} day{'s' if nd != 1 else ''}, "
                f"{fire['hotspots']:.0f} harmonized hotspots)")
        if pct >= 90:
            since = (f"the largest since {fire['largest_since']}" if fire.get("largest_since")
                     else f"the largest on record (since {fire['record_start_year']})")
            parts.append(f"{head} is larger than {pct:.0f}% of the {fire['past_fires']} earlier fires "
                         f"in this area: {since}.")
        else:
            parts.append(f"{head} is a typical fire for this area (larger than {pct:.0f}% of "
                         f"{fire['past_fires']} earlier fires).")
    if analogs:
        ys = ", ".join(str(a["year"]) for a in analogs[:2])
        parts.append(f"So far this year looks most like {ys}.")
    if trend:
        def days(v):
            return f"{abs(v):.0f} day{'s' if round(abs(v)) != 1 else ''}"
        s_, L = trend["start_shift_days_per_decade"], trend["length_change_days_per_decade"]
        parts.append(f"Since the early 2000s the fire season starts {days(s_)} per decade "
                     f"{'earlier' if s_ < 0 else 'later'} and lasts {days(L)} per decade "
                     f"{'longer' if L > 0 else 'shorter'}.")
    return " ".join(parts)


EXPORT_COLUMNS = ["date", "time_utc", "lat", "lon", "sensor", "platform", "daynight", "confidence",
                  "frp", "in_reference", "weight", "weight_lo", "weight_hi", "weight_level",
                  "low_confidence_period", "event_id"]


def export_csv(sel: pd.DataFrame, path) -> None:
    """Every hotspot in the search, with its sensor and its harmonization weight."""
    sel[[c for c in EXPORT_COLUMNS if c in sel]].sort_values("time_utc").to_csv(path, index=False)
