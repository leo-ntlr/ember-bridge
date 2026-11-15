import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_bridge import analytics as A  # noqa: E402
from ember_bridge import events as E  # noqa: E402
from ember_bridge.harmonize import build_harmonized, fit  # noqa: E402
from tests.synthetic import make  # noqa: E402


def test_cluster_rules():
    lat = [50.000, 50.010, 50.060, 50.000, 50.000, 51.0]
    lon = [-110.0, -110.0, -110.0, -110.0, -110.0, -110.0]
    days = ["2020-07-01", "2020-07-01", "2020-07-01", "2020-07-03", "2020-07-07", "2020-07-01"]
    lab = E.cluster_labels(lat, lon, days)
    assert lab[0] == lab[1] == lab[2] == lab[3]   # touching cells, same day or 2 days later: one fire
    assert lab[4] != lab[0]                        # 6 days later: a new fire
    assert lab[5] != lab[0]                        # 100 km away: another fire


def test_fire_counts_and_comparison():
    df = make(regions=("canada",), start="2003-01-01", end="2025-12-31", fires_per_day=3)
    h = build_harmonized(df, {"Aqua": fit(df, "Aqua", start="2012-01-20", end="2025-12-31", n_boot=20)})
    sel = A.select(h, [(-118, 50), (-106, 50), (-106, 62), (-118, 62)])
    _, ev = E.detect_events(sel)
    factors = E.event_count_factor(df, years=(2013, 2016, 2019))
    raw = E.fire_calendar(ev)
    harm = E.fire_calendar(ev, factors)

    def jump(c, col):
        y = c.groupby("year")[col].sum()
        return y.loc[2012:2014].mean() / y.loc[2009:2011].mean()
    print("fire-count jump raw", round(jump(raw, "fires"), 2), "harmonized", round(jump(harm, "fires"), 2))
    assert jump(raw, "fires") > 1.3 and 0.85 < jump(harm, "fires") < 1.2
    cmp_ = E.compare_event(ev)
    print(cmp_)
    assert cmp_["past_fires"] > 1000 and 0 <= cmp_["size_percentile"] <= 100
    an = A.anomaly(A.calendar(sel), 2025, 12)
    print(A.brief("Test area", an, [], {}, cmp_))


if __name__ == "__main__":
    test_cluster_rules()
    test_fire_counts_and_comparison()
    print("ok")
