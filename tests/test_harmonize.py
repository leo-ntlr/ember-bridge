import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_bridge.harmonize import fit, validate  # noqa: E402
from tests.synthetic import make  # noqa: E402


def test_harmonization_beats_raw():
    df = make(regions=("canada", "amazon"), start="2011-01-01", end="2025-12-31", fires_per_day=4)
    cal = fit(df, "Aqua", start="2012-01-20", end="2021-12-31", n_boot=30)
    res = validate(df, cal, "2022-01-01", "2025-12-31")
    print(cal.global_ratio, res)
    assert res["months"] > 20
    assert res["median_abs_pct_error_harmonized"] < 15
    assert res["median_abs_pct_error_raw"] > 40
    assert res["correlation_harmonized"] > 0.9


if __name__ == "__main__":
    test_harmonization_beats_raw()
    print("ok")
