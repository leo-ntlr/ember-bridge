import gzip
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_bridge import firms_api as F  # noqa: E402
from tests import fake_firms  # noqa: E402


def test_offline_helpers():
    # the 5-day grid counts from 2000-01-01; 2024-01-05 is day 8770, a multiple of 5
    assert F.windows("2024-01-01", "2024-01-12") == [("2024-01-01", 4), ("2024-01-05", 5), ("2024-01-10", 3)]
    # a range starting elsewhere first catches up with the grid, so later windows keep their names
    assert F.windows("2024-01-03", "2024-01-12") == [("2024-01-03", 2), ("2024-01-05", 5), ("2024-01-10", 3)]
    assert F.windows("2024-01-03", "2024-01-12", align=False) == [("2024-01-03", 5), ("2024-01-08", 5)]
    url = F.area_url("KEY", "VIIRS_NOAA20_NRT", F.REGIONS["amazon"], 5, "2026-11-01")
    assert url.endswith("/api/area/csv/KEY/VIIRS_NOAA20_NRT/-74,-18,-44,5/5/2026-11-01")
    avail = {"VIIRS_NOAA20_SP": ("2018-04-01", "2026-05-31"), "VIIRS_NOAA20_NRT": ("2026-05-01", "2026-11-13")}
    assert F.plan("VIIRS_NOAA20", "2018-04-01", "2026-11-13", avail) == [
        ("VIIRS_NOAA20_SP", "2018-04-01", "2026-05-31"), ("VIIRS_NOAA20_NRT", "2026-06-01", "2026-11-13")]
    assert F.plan("VIIRS_NOAA21", "2023-03-01", "2026-11-13", {"VIIRS_NOAA21_NRT": ("2024-01-17", "2026-11-14")}) \
        == [("VIIRS_NOAA21_NRT", "2024-01-17", "2026-11-13")]
    assert F._key_problem("Invalid MAP_KEY.") and not F._key_problem("latitude,longitude")
    assert F._quota_problem("Exceeding allowed transaction limit") and not F._quota_problem("Invalid MAP_KEY.")
    assert F._redact("bad key abc123", "abc123") == "bad key <MAP_KEY>"


def test_load_key(tmp: Path):
    import os
    old_cwd, old_env = Path.cwd(), os.environ.pop("FIRMS_MAP_KEY", None)
    try:
        os.chdir(tmp)
        (tmp / ".env").write_text('# comment\nexport FIRMS_MAP_KEY="abc123"\n')
        assert F.load_key() == "abc123"
    finally:
        os.chdir(old_cwd)
        if old_env is not None:
            os.environ["FIRMS_MAP_KEY"] = old_env


def _days_in(path: Path) -> set[str]:
    with gzip.open(path, "rt") as f:
        lines = f.read().strip().splitlines()
    col = lines[0].split(",").index("acq_date")
    return {line.split(",")[col] for line in lines[1:]}


def test_end_to_end(tmp: Path):
    server, base = fake_firms.start()
    F.BASE, F.MIN_INTERVAL_S, F.LIMIT_PAUSE_S, F.RETRY_WAIT_S = base, 0.0, 0.2, 0.01
    st, key = fake_firms.STATE, fake_firms.GOOD_KEY
    raw = tmp / "raw"
    tiers = [[("MODIS", "2026-06-20", None), ("VIIRS_SNPP", "2026-10-20", None)],
             [("VIIRS_NOAA20", "2026-10-25", None)]]
    st.quota_at, st.fail_503_at = 6, 9
    st.always_fail = {("MODIS_NRT", "2026-07-13")}
    try:
        try:
            F.check("bad" * 10, raw)
            raise AssertionError("a refused key must stop everything")
        except F.FirmsKeyError as e:
            assert "bad" * 10 not in str(e)

        assert F.check(key, raw)
        summary = F.run_all(key, raw, ["canada", "amazon"], workers=4, end="2026-11-13", tiers=tiers)
        assert summary["missing"] == 0, summary
        wins = F.build_windows(F.availability(key), ["canada", "amazon"], "2026-11-13", tiers)
        assert all(w.done(raw) for w in wins)
        # MODIS: SP archive up to 2026-06-30, NRT afterwards; Suomi NPP NRT stops on 2026-11-01
        names = {p.name for p in (raw / "canada" / "api").iterdir()}
        assert "MODIS_SP_2026-06-28_3.csv.gz" in names and "MODIS_NRT_2026-07-01_2.csv.gz" in names
        snpp_days = [n.split("_")[3] for n in names if n.startswith("VIIRS_SNPP_NRT_")]
        assert snpp_days and max(snpp_days) <= "2026-11-01"
        # the window that kept failing as 5 days was fetched day by day instead
        assert "MODIS_NRT_2026-07-13_5.csv.gz" not in names
        assert {f"MODIS_NRT_2026-07-{d}_1.csv.gz" for d in range(13, 18)} <= names
        assert _days_in(raw / "canada" / "api" / "MODIS_NRT_2026-07-03_5.csv.gz") == \
            {f"2026-07-{d:02d}" for d in range(3, 8)}
        assert (raw / "amazon" / "nrt" / "VIIRS_NOAA20_NRT_latest.csv.gz").exists()
        report = F.status(raw)
        assert "100%" in report and "finished" in report, report

        # a second run downloads nothing again (only the date test and the near-real-time top-up)
        n_before = len(st.area_requests)
        F.run_all(key, raw, ["canada", "amazon"], workers=4, end="2026-11-13", tiers=tiers)
        assert len(st.area_requests) - n_before == 1 + 2 * len(F.NRT_UPDATE_SOURCES)

        # if FIRMS read DATE as the last day of the window, the downloader adapts
        st.semantics = "end"
        assert F.probe_date_semantics(key)[0] == "end"
        w = F.Window("canada", "MODIS_NRT", "2026-11-01", 5)
        F.download_window(key, w, tmp / "raw_end", "end")
        assert _days_in(w.path(tmp / "raw_end")) == {f"2026-11-{d:02d}" for d in range(1, 6)}
        st.semantics = "start"

        # the downloaded files go straight into the ingest step
        from ember_bridge import ingest
        report = ingest.ingest(raw, tmp / "parquet")
        canada = report.set_index("region").loc["canada"]
        assert canada["output"] > 0 and canada["after_dedup"] < canada["input"]   # NRT top-up overlaps
    finally:
        server.shutdown()


def test_quota_guard(tmp: Path):
    """The night of 15 Nov 2026: FIRMS counted about 20 transactions per window, the quota ran out in
    2 minutes, and after the 10-minute pause FIRMS answered "Invalid MAP_KEY." to a key it had accepted
    for 480 windows. The guard must keep the key under its quota, and a block must only pause the night."""
    server, base = fake_firms.start()
    F.BASE, F.MIN_INTERVAL_S, F.RETRY_WAIT_S = base, 0.0, 0.01
    F.LIMIT_PAUSE_S, F.STATUS_EVERY_S, F.HOLD_S = 0.3, 0.0, 0.05
    st, key = fake_firms.STATE, fake_firms.GOOD_KEY
    tiers = [[("MODIS", "2026-01-01", None)]]

    def tight_quota():
        st.reset()
        # 4 transactions per day, a rolling interval of 0.5 s; the guard holds at 85 % of 600 and four
        # downloads in flight add at most 4 x 20, so a guarded run can never go over the limit
        st.limit, st.interval_s, st.cost_per_day = 600, 0.5, 4
        st.lockout_after, st.lockout_s = 2, 0.4
        F.GATE = F._Gate()

    try:
        # 1. with quota readings, the guard holds the downloads and FIRMS never has to say no
        tight_quota()
        summary = F.run_all(key, tmp / "guarded", ["canada", "amazon"], workers=4, end="2026-11-13", tiers=tiers)
        assert summary["missing"] == 0, summary
        assert st.overruns == 0 and st.lockouts == 0, (st.overruns, st.lockouts)
        assert F.GATE.holds >= 1 and F.GATE.quota and F.GATE.quota[1] == 600

        # 2. without quota readings, FIRMS says no, then blocks the key: the downloads pause, then finish
        tight_quota()
        st.status_broken = True
        summary = F.run_all(key, tmp / "blocked", ["canada", "amazon"], workers=4, end="2026-11-13", tiers=tiers)
        assert summary["missing"] == 0, summary
        assert st.overruns >= 2 and st.lockouts >= 1, (st.overruns, st.lockouts)
        wins = F.build_windows(F.availability(key), ["canada", "amazon"], "2026-11-13", tiers)
        assert all(w.done(tmp / "blocked") for w in wins)

        # 3. a key FIRMS never accepted still stops everything at once, even in the middle of a run
        bad = "bad" * 10
        try:
            F.fetch(F.area_url(bad, "MODIS_NRT", F.REGIONS["canada"], 1), secret=bad)
            raise AssertionError("a key that never worked must stop everything")
        except F.FirmsKeyError as e:
            assert bad not in str(e)

        # 4. status says the downloads stopped and how to resume them
        (tmp / "blocked" / "download_done.json").unlink()
        assert "stopped (bash ember.sh resumes it)" in F.status(tmp / "blocked")
    finally:
        st.reset()
        F.GATE = F._Gate()
        server.shutdown()


if __name__ == "__main__":
    test_offline_helpers()
    with tempfile.TemporaryDirectory() as d:
        test_load_key(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_end_to_end(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_quota_guard(Path(d))
    print("ok")
