import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_bridge import analytics as A  # noqa: E402
from ember_bridge.harmonize import build_harmonized, fit  # noqa: E402
from tests.synthetic import make  # noqa: E402


def test_pipeline(tmp_path=Path("/tmp")):
    df = make(regions=("canada",), start="2003-01-01", end="2025-12-31", fires_per_day=3)
    cal = fit(df, "Aqua", start="2012-01-20", end="2025-12-31", n_boot=20)
    h = build_harmonized(df, {"Aqua": cal})
    poly = [(-118, 50), (-106, 50), (-106, 62), (-118, 62)]
    sel = A.select(h, poly)
    harm = A.calendar(sel)
    raw = A.calendar(sel, harmonized=False)
    # the fake 2012 jump: raw counts explode when VIIRS appears, harmonized ones do not
    def jump(c):
        y = c.groupby("year")["hotspots"].sum()
        return y.loc[2012:2014].mean() / y.loc[2009:2011].mean()
    print("raw jump", round(jump(raw), 2), "harmonized jump", round(jump(harm), 2))
    assert jump(raw) > 2.5 and jump(harm) < 1.4
    an = A.anomaly(harm, 2025, 7)
    analogs = A.analog_years(harm, 2025, 7)
    trend = A.season_trend(A.season_timing(sel))
    print(A.brief("Northern Alberta", an, analogs, trend))
    assert len(analogs) == 3 and an["median"] > 0
    assert len(A.select(h, A.circle(56, -112, 100))) > 0
    wl = A.watchlist(h, {"North": [(-118, 57), (-106, 57), (-106, 62), (-118, 62)],
                         "South": [(-118, 50), (-106, 50), (-106, 55), (-118, 55)]}, 2025, 7)
    print(wl[["area", "value", "median", "ratio_to_median", "significant"]])
    assert list(wl.columns[:2]) == ["area", "value"] and len(wl) == 2
    A.export_csv(sel, tmp_path / "export.csv")


if __name__ == "__main__":
    test_pipeline()
    print("ok")
