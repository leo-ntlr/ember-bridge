# Ember Bridge

**One fire history, whatever the satellite.** A harmonized MODIS + VIIRS burning-activity calendar
for emergency managers (NASA Space Apps Challenge 2026, *Harmonization of MODIS and VIIRS Hot Spots*).

FIRMS hotspots from five satellites cannot simply be added up: VIIRS (375 m) sees about five times
more hotspots than MODIS (1 km), MODIS misses small fires, Terra passes at 10:30 while the others pass
at 13:30, and the counts jump every time a satellite is added (2002, 2012, 2018, 2023). Ember Bridge
gives every hotspot a weight with a 95 % range, so that 26 years of fires (November 2000 to today)
read as one consistent, VIIRS-equivalent series, in three fire regimes: Canada (boreal forest),
the Brazilian Amazon (deforestation fires) and the southern African savanna.

## Results on the real FIRMS archive

Validation of the build of 15 November 2026 (FIRMS data to 2026-11-15, Suomi NPP to 2026-11-01), from
`data/model/validation.json`. Error = median absolute error of monthly totals against what Suomi NPP
saw, over months with at least 20 Suomi NPP hotspots. The 95 % range should contain the truth 95 % of the time.

| Test | Months | Harmonized error | Raw counts error | 95 % range coverage |
|---|---:|---:|---:|---:|
| Aqua, years never seen (fit 2012-2022, test 2023 to 2026-11-01) | 141 | 22.0 % | 83.7 % | 70.9 % |
| same, in 1 degree cells (about 100 km) | 28,498 cell-months | 40.4 % | 83.8 % | 72.3 % |
| Aqua, region never seen: Canada | 179 | 16.4 % | 86.9 % | 79.9 % |
| Aqua, region never seen: Brazilian Amazon | 179 | 18.8 % | 83.0 % | 98.3 % |
| Aqua, region never seen: southern African savanna | 179 | 42.0 % | 84.2 % | 42.5 % |
| Terra, in sample | 537 | 11.3 % | 90.3 % | 97.8 % |
| NOAA-20, in sample | 312 | 3.1 % | 4.4 % | 93.9 % |

Correlation with the truth on the years never seen: 0.98. Known limits: on those years the harmonized
series is 21 % too high (48 % in the Amazon), and our leading suspect, not yet tested, is Aqua's drift
to a later afternoon pass since its last orbit maneuvers in 2021; the 95 % ranges are too narrow out of
sample; moving the model from forests to the savanna is the hardest transfer.

## Quick start (macOS or Linux, Python 3.9+)

```bash
bash ember.sh            # installs, asks the FIRMS MAP_KEY once, checks it, downloads everything (background, resumable)
bash ember.sh status     # progress
bash ember.sh build      # downloads -> harmonized calendar, validation report and web data (background)
bash ember.sh serve      # web app on http://localhost:8000
bash ember.sh update     # newest days, then refresh the current year with the same model
bash ember.sh test       # test suite, no network needed
```

The web app (`web/`) is static: after `build`, the folder can be published as is (Netlify, GitHub
Pages, any web server).

## 1. FIRMS data

**FIRMS area API (recommended).** Free MAP_KEY at https://firms.modaps.eosdis.nasa.gov/api/map_key/.
`bash ember.sh` keeps it in `.env` (git-ignored, never printed or logged) and runs
`python -m ember_bridge.firms_api all`:

* 3 regions x 4 satellite families, about 11,500 requests of 5 days each, in priority order: first what
  the harmonized calendar needs (MODIS since 2000-11, Suomi NPP 2012-01 to 2026-11-01, NOAA-20 since
  2024), then the rest of NOAA-20 and NOAA-21.
* Science-quality (SP) data as far as they go, then near-real-time (NRT), using the FIRMS
  data-availability service; ingest drops NRT rows once SP data exist for that day.
* One gzip CSV per window in `data/raw/<region>/api/`, on a fixed 5-day grid; a crash never leaves a
  half-written file and a new run only fetches what is missing.
* Quota: 5000 transactions per 10 minutes per key, and an area request costs about 4 transactions per
  day of data (about 20 for a 5-day window). A quota guard reads the key's transaction count every few
  seconds and holds every worker at 85 % of the quota, so the full download takes about 9 hours. If
  FIRMS says no anyway (a quota message, or "Invalid MAP_KEY." for a key it accepted before, which is
  how it blocks a key for a while), all workers pause 10, then 20, then 30 minutes and carry on.
  Network or server errors are retried, and a window that keeps failing is fetched again one day at a time.
* A test request on the 2016 Fort McMurray fire checks how FIRMS reads the DATE parameter.
* Single commands: `python -m ember_bridge.firms_api check | all | status | update --region all |
  backfill --region canada --family VIIRS_NOAA20`.

**Archive Download (alternative).** Any FIRMS archive CSV or ZIP dropped in `data/raw/<region>/` is read too.

## 2. Build

`python -m ember_bridge.build` (what `bash ember.sh build` runs) reads one region-year at a time, so
hundreds of millions of detections fit on a laptop:

1. **ingest**: clean, compact Parquet store `data/store/region=<r>/year=<y>/<platform>.parquet`.
   Vegetation fires only (FIRMS `type` 0). NRT files have no `type`: static sources learned from the
   science-quality archive (gas flares, mines, industry, volcanoes) are masked in them, so the month in
   progress is compared fairly. Duplicates and low-confidence detections removed.
2. **fit tables**: for each region-year, cell-day pairs (0.25 degree cells) of Aqua, Terra and NOAA-20
   with Suomi NPP, reduced to small additive tables.
3. **fit**: Aqua, Terra and NOAA-20 -> Suomi NPP calibrations.
4. **validate**: trained on 2012-2022 and tested on 2023 to 2026-11-01, for whole regions and for
   1 degree cells (about 100 km, the size of an area drawn in the app); trained on two regions and tested
   on the third. Report: `data/model/validation.json`.
5. **harmonize**: weights for every backbone hotspot (Terra 2000-11 to 2002-07, Aqua to 2012-01, Suomi
   NPP to 2026-11-01, NOAA-20 after), monthly cube on a 0.25 degree grid, fire events.
6. **web**: compact JSON in `web/data/`, with the monthly cube split into files of about 5 MB so that
   any static host accepts them.

`--years 2026` refreshes one year with the model already fitted (minutes instead of an hour).

## 3. Method

* **Resolution**: the ratio depends on the real MODIS pixel size (scan x track grows toward the swath edge).
* **Sensitivity**: the ratio depends on fire radiative power, plus a "missed fires" correction per
  region, month and day/night, set so that in the overlap years the weights add up exactly to what
  Suomi NPP saw (ratio and correction never count the same fire twice).
* **Observation schedule**: the reference series is the afternoon/night one (Aqua, Suomi NPP, NOAA-20);
  the ratio depends on the real local solar time of each pass, so Terra and Aqua orbit drift is seen by
  the model; Terra-only years are flagged as lower confidence.
* `weight = ratio(situation) x (1 + missed correction)`. Sparse situations fall back to coarser ones.
* **Uncertainty**: block bootstrap (blocks = cell x month) for the calibration, widened to the
  year-to-year spread of the correction (a prediction interval for a new year), plus compound-Poisson
  detection noise; every calendar cell has a 95 % range, hatched when wider than the value.
* **Fires, not only hotspots**: hotspots touching in space (~5 km) and time (up to 2 days apart) form a
  fire; fire size is measured in harmonized hotspots; MODIS-era fire counts are scaled by the number of
  fires Suomi NPP sees per fire Aqua sees (overlap years). The current fire is ranked against every
  earlier fire of the area.

## 4. Web app

Region tabs; draw a polygon or rectangle, type lat/lon + radius, or pick a watched area (an area outside
the region, over open water, or smaller than one 0.25 degree cell gets a note saying what is counted);
a map legend; month x year
calendar (harmonized or raw, hotspots or fires, counts or anomaly, cividis palette, values in cells,
hatching for uncertainty, satellite strip); the selected month against the same month of every
earlier year (or the same days, for the month in progress) with rank and significance; fire comparator;
situation brief; fire-season timing and trend; analog years; watchlist of sensitive areas;
downloads: calendar CSV, hotspots of the last 31 days CSV, calendar PNG.

## 5. Full-archive export and Python use

```bash
python -m ember_bridge.export --region canada --circle 56.73,-111.38,80 --start 2016-04-01 --end 2016-07-31 --out fort_mcmurray_2016.csv
```

`ember_bridge.analytics` (calendar, anomaly, watchlist, analog years, season timing, brief) and
`ember_bridge.events` (fires, comparator) also work on any harmonized table in memory.

## Tests

```bash
bash ember.sh test
```

`test_firms_api` runs the downloader end to end against a local fake FIRMS server; `test_build` runs the
whole pipeline on synthetic FIRMS files whose truth is known (raw counts jump x2.5 when VIIRS arrives,
harmonized counts stay flat; monthly error ~8 % harmonized vs ~75 % raw MODIS on held-out years, and
~17 % vs ~79 % in 1 degree cells).

## Acknowledgments (required by NASA FIRMS, copy into the Space Apps "References" field)

We acknowledge the use of data and/or imagery from NASA's Fire Information for Resource Management
System (FIRMS) (https://earthdata.nasa.gov/firms), part of NASA's Land, Atmosphere Near real-time
Capability for Earth observations (LANCE) (https://earthdata.nasa.gov/lance) and NASA's Earth Science
Data and Information System (ESDIS).

Third-party code shipped in `web/vendor/`: Leaflet 1.9.4 (BSD-2-Clause) and Leaflet.draw 1.0.4 (MIT).
Map background: we acknowledge the use of imagery provided by services from NASA's Global Imagery Browse
Services (GIBS), part of NASA's Earth Science Data and Information System (ESDIS): Blue Marble Next Generation
(default) and VIIRS NOAA-20 Corrected Reflectance (true colour of the latest day). The map needs no key.

## License

MIT, see [LICENSE](LICENSE). The FIRMS data and the NASA GIBS imagery keep their own terms (see Acknowledgments above).
