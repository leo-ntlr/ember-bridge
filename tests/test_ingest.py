"""Run with: python -m pytest tests  (or python tests/test_ingest.py)"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_bridge.ingest import ingest  # noqa: E402

MODIS = """latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight,type
55.1,-110.2,330.1,1.2,1.1,2015-07-03,2050,Aqua,MODIS,85,6.1,300.2,45.3,D,0
55.1,-110.2,330.1,1.2,1.1,2015-07-03,2050,Aqua,MODIS,85,6.1,300.2,45.3,D,0
55.2,-110.3,320.0,1.0,1.0,2015-07-03,1730,Terra,MODIS,20,6.1,299.0,10.0,D,0
19.4,-155.3,360.0,1.0,1.0,2015-07-03,0910,Terra,MODIS,90,6.1,300.0,99.0,N,1
55.3,-110.1,325.0,1.1,1.0,2015-07-04,0530,Terra,MODIS,60,6.1,290.0,12.0,N,0
"""
VIIRS = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight,type
55.10,-110.21,340.2,0.4,0.4,2015-07-03,2056,N,VIIRS,n,2,300.1,8.1,D,0
55.11,-110.20,338.0,0.4,0.4,2015-07-03,2056,N,VIIRS,h,2,301.0,6.0,D,0
55.14,-110.24,335.0,0.4,0.4,2015-07-03,2058,N,VIIRS,n,2.0NRT,300.0,5.0,D,0
55.12,-110.22,330.0,0.4,0.4,2019-07-03,2005,1,VIIRS,l,2,299.0,2.0,D,0
55.13,-110.23,332.0,0.4,0.4,2024-07-03,2030,N21,VIIRS,n,2,299.0,3.0,D,2
"""


def test_ingest(tmp_path: Path):
    raw = tmp_path / "raw" / "canada"
    raw.mkdir(parents=True)
    (raw / "modis.csv").write_text(MODIS)
    (raw / "viirs.csv").write_text(VIIRS)
    out = tmp_path / "parquet"
    out.mkdir()
    report = ingest(tmp_path / "raw", out)
    df = pd.read_parquet(out)

    r = report.iloc[0]
    assert r["input"] == 10
    assert r["after_type_filter"] == 8      # volcano (type 1) and static source (type 2) removed
    assert r["after_nrt_superseded"] == 7   # NRT row dropped: science-quality data exists that day
    assert r["after_dedup"] == 6            # duplicated Aqua row removed
    assert r["output"] == 4                 # two low-confidence rows removed (MODIS 20, VIIRS l)
    assert set(df["platform"]) == {"Aqua", "SNPP", "Terra"}
    # At -110 E, local solar time = UTC - 7.3 h: Terra 05:30 UTC (night, ~22:10) folds to morning,
    # Aqua and Suomi NPP ~20:50 UTC are ~13:30 local, i.e. afternoon.
    groups = df.set_index("platform")["orbit_group"].to_dict()
    assert groups["Terra"] == "morning"
    assert groups["SNPP"] == "afternoon"
    assert groups["Aqua"] == "afternoon"

    # Re-running must not duplicate rows.
    ingest(tmp_path / "raw", out)
    assert len(pd.read_parquet(out)) == 4


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_ingest(Path(d))
    print("ok")
