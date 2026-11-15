"""End to end: synthetic raw FIRMS files -> store -> model -> web data -> full-archive export."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ember_bridge.build as build_module  # noqa: E402
from ember_bridge.build import build  # noqa: E402
from ember_bridge.export import export  # noqa: E402
from ember_bridge.analytics import circle  # noqa: E402
from tests import synthetic_raw  # noqa: E402


def load_cube(data: Path, region: str, meta: dict) -> dict:
    """Read the cube files of a region and join them, like mergeCubes() in web/app.js."""
    n = meta["regions"][region]["cube_parts"]
    parts = [json.loads((data / f"{region}_cube_{k}.json").read_text()) for k in range(n)]
    assert len(list(data.glob(f"{region}_cube*.json"))) == n          # no stale files left behind
    cube = {k: sum((p[k] for p in parts), []) for k in parts[0] if k != "raw"}
    cube["raw"] = {pl: sum((p["raw"][pl] for p in parts), []) for pl in parts[0]["raw"]}
    assert all(len(v) == len(cube["t"]) for v in [*cube.values(), *cube["raw"].values()] if isinstance(v, list))
    return cube


def annual(cube: dict, key: str) -> dict:
    out = {}
    for i, t in enumerate(cube["t"]):
        y = 2000 + t // 12
        v = sum(cube["raw"][p][i] for p in cube["raw"]) if key == "raw" else cube[key][i]
        out[y] = out.get(y, 0) + v
    return out


def test_build(tmp: Path):
    synthetic_raw.write(tmp / "raw", fires_per_day=1.5)
    build_module.CUBE_ROWS_PER_FILE = 1_000        # small files, so that the split into several files is tested
    report = build(tmp / "raw", tmp / "store", tmp / "model", tmp / "web", n_boot=20, workers=2)
    ts = report["Aqua_time_split_test"]
    print({k: ts[k] for k in ("months", "median_abs_pct_error_harmonized", "median_abs_pct_error_raw",
                              "interval_coverage_pct")})
    assert ts["months"] > 20 and ts["median_abs_pct_error_harmonized"] < 20 < ts["median_abs_pct_error_raw"]
    assert all(f"Aqua_unseen_region_{r}" in report for r in ("canada", "amazon", "southern_africa"))
    ing = (tmp / "store" / "_ingest_report.csv").read_text()
    assert "static_mask_removed" in ing

    data = tmp / "web" / "data"
    meta = json.loads((data / "meta.json").read_text())
    assert set(meta["regions"]) == {"canada", "amazon", "southern_africa"}
    assert meta["backbone"][-1]["platform"] == "NOAA20"
    print("canada cube files:", meta["regions"]["canada"]["cube_parts"])
    assert meta["regions"]["canada"]["cube_parts"] > 1
    cube = load_cube(data, "canada", meta)
    harm, raw = annual(cube, "pred"), annual(cube, "raw")
    # raw counts jump with every new satellite, harmonized ones do not
    jump = lambda s: (s[2013] + s[2014]) / (s[2009] + s[2010])  # noqa: E731
    print("2012 jump raw", round(jump(raw), 2), "harmonized", round(jump(harm), 2))
    assert jump(raw) > 2.0 and 0.75 < jump(harm) < 1.35
    assert harm[2023] > 1.8 * harm[2022]                       # the synthetic record season is visible
    for name in ("canada_events.json", "canada_mtd.json", "canada_recent.json"):
        assert (data / name).exists(), name

    out = export(tmp / "store", tmp / "model", "canada", circle(56.5, -112.0, 150), "2016-01-01", "2016-12-31")
    assert len(out) > 0 and out["weight"].gt(0).all() and out["event_id"].notna().all()
    assert set(out["platform"].astype(str)) == {"SNPP"}
    out_all = export(tmp / "store", tmp / "model", "canada", circle(56.5, -112.0, 150), "2016-01-01", "2016-12-31",
                     all_satellites=True)
    assert len(out_all) > len(out) and out_all["weight"].eq(0).any()

    # incremental refresh of the last year keeps the model and rewrites the web data
    build(tmp / "raw", tmp / "store", tmp / "model", tmp / "web", years={2026}, n_boot=20)
    meta2 = json.loads((data / "meta.json").read_text())
    assert meta2["last_day"] == meta["last_day"]
    assert load_cube(data, "canada", meta2)["t"] == cube["t"]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        test_build(Path(d))
    print("ok")
