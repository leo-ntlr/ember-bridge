"""Synthetic FIRMS-like data where we know the truth: VIIRS sees ~2.5x more pixels than MODIS
and MODIS misses ~30 % of small fires. Used to test the harmonization end to end."""
import numpy as np
import pandas as pd


def make(regions=("canada", "amazon", "savanna"), start="2010-01-01", end="2025-12-31", seed=1,
         fires_per_day=6):
    rng = np.random.default_rng(seed)
    rows = []
    days = pd.date_range(start, end, freq="D")
    centers = {"canada": (56, -112), "amazon": (-8, -60), "savanna": (-12, 25)}
    for region in regions:
        lat0, lon0 = centers[region]
        season = 1 + np.sin((days.dayofyear / 365.25) * 2 * np.pi) * 0.9
        trend = 1 + (days.year - 2010) * 0.02
        n = rng.poisson(fires_per_day * season * trend)
        for day, k in zip(days, n):
            for _ in range(k):
                lat = lat0 + rng.normal(0, 1.5)
                lon = lon0 + rng.normal(0, 1.5)
                frp = rng.lognormal(2.5, 1.0)
                utc_hour = (13.5 - lon / 15) % 24                 # ~13:30 local
                t = day + pd.Timedelta(hours=float(utc_hour))
                # VIIRS: 375 m pixels -> several pixels per fire
                if day >= pd.Timestamp("2012-01-20"):
                    for _ in range(max(1, rng.poisson(1.0 + frp / 15))):
                        rows.append((region, t, lat + rng.normal(0, .003), lon + rng.normal(0, .003),
                                     "VIIRS", "SNPP", "n", frp / 3, .4, .4, 0))
                # MODIS Aqua: 1 km pixels, misses small fires
                if day >= pd.Timestamp("2002-07-04") and rng.random() < 1 - np.exp(-frp / 12):
                    area = rng.choice([1.0, 2.0, 4.0], p=[.5, .3, .2])
                    rows.append((region, t, lat, lon, "MODIS", "Aqua", "70", frp, area ** .5, area ** .5, 0))
    df = pd.DataFrame(rows, columns=["region", "time_utc", "lat", "lon", "sensor", "platform",
                                     "confidence_raw", "frp", "scan", "track", "type"])
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    df["date"] = df["time_utc"].dt.date
    df["year"] = df["time_utc"].dt.year
    df["month"] = df["time_utc"].dt.month
    df["daynight"] = "D"
    df["confidence"] = "nominal"
    df["pixel_area_km2"] = df["scan"] * df["track"]
    df["orbit_group"] = "afternoon"
    return df
