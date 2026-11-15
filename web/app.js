/* Ember Bridge web app: harmonized MODIS + VIIRS burning-activity calendar.
   Static: everything is computed in the browser from the precomputed grid cube (data/*.json). */
"use strict";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const PLATFORM_LABEL = { Terra: "Terra MODIS", Aqua: "Aqua MODIS", SNPP: "Suomi NPP VIIRS", NOAA20: "NOAA-20 VIIRS",
  NOAA21: "NOAA-21 VIIRS" };
const PLATFORM_COLOR = { Terra: "#E69F00", Aqua: "#56B4E9", SNPP: "#009E73", NOAA20: "#CC79A7", NOAA21: "#0072B2" };
const CIVIDIS = ["#00224e", "#123570", "#3b496c", "#575d6d", "#707173", "#8a8779", "#a69d75", "#c4b56c", "#e4cf5b", "#fee838"];
const DIVERGING = ["#4b2c84", "#7b62b0", "#b3a6d4", "#e3dfee", "#f2f0ec", "#fbdcc4", "#f4a86d", "#de6b1f", "#a63f05"];
const HIGHLIGHTS = {
  canada: [["2023 record season", { y: 2023, m: 6 }], ["Fort McMurray, May 2016", { y: 2016, m: 5, area: "Fort McMurray, AB" }],
    ["Jasper, July 2024", { y: 2024, m: 7, area: "Jasper National Park, AB" }], ["Yellowknife, August 2023", { y: 2023, m: 8, area: "Yellowknife, NT" }]],
  amazon: [["2019 Amazon fires", { y: 2019, m: 8 }], ["2024 drought fires", { y: 2024, m: 9 }],
    ["Pantanal, 2020", { y: 2020, m: 9, area: "Northern Pantanal (Pocone), MT" }]],
  southern_africa: [["Peak season, August 2024", { y: 2024, m: 8 }], ["Kafue, 2019", { y: 2019, m: 8, area: "Kafue National Park, Zambia" }]],
};
const DAY_MS = 86400000;
const EPOCH = Date.UTC(2000, 0, 1);

const S = {
  meta: null, watch: {}, region: null, D: null, view: "harm", metric: "hotspots", mode: "counts",
  focus: null, from: null, to: null, sel: null, A: null, map: null, layers: {}, watchAgg: [],
};

/* ------------------------------------------------------------------ utilities */
const $ = (id) => document.getElementById(id);
const tIndex = (y, m) => (y - 2000) * 12 + (m - 1);
const yearOf = (t) => 2000 + Math.floor(t / 12);
const monthOf = (t) => (t % 12) + 1;
const daysIn = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();
const dayNum = (y, m, d) => Math.round((Date.UTC(y, m - 1, d) - EPOCH) / DAY_MS);
const fromDayNum = (n) => new Date(EPOCH + n * DAY_MS);
const isoDay = (n) => fromDayNum(n).toISOString().slice(0, 10);
const monthYear = (n) => { const d = fromDayNum(n); return `${MONTHS_LONG[d.getUTCMonth()]} ${d.getUTCFullYear()}`; };
// "the largest since ...": the month for an old fire, the exact day when the bigger fire is less than a year older
const sinceText = (since, start) => since == null ? null
  : start - since < 365 ? (() => { const d = fromDayNum(since); return `the largest since the fire of ${d.getUTCDate()} ${MONTHS_LONG[d.getUTCMonth()]} ${d.getUTCFullYear()}`; })()
  : `the largest since ${monthYear(since)}`;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmt(v) {
  if (v == null || !isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return Math.round(v / 1e3) + "k";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(Math.round(v));
}
const num = (v, d = 0) => (v == null || !isFinite(v) ? "–" : v.toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));

function hexToRgb(h) { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
function ramp(stops, x) {
  x = Math.max(0, Math.min(1, x));
  const p = x * (stops.length - 1), i = Math.min(Math.floor(p), stops.length - 2), f = p - i;
  const a = hexToRgb(stops[i]), b = hexToRgb(stops[i + 1]);
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",")})`;
}
function inkFor(rgb) {
  const [r, g, b] = rgb.match(/\d+/g).map(Number);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#14181d" : "#ffffff";
}
function median(a) {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y), k = s.length >> 1;
  return s.length % 2 ? s[k] : (s[k - 1] + s[k]) / 2;
}
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
function slope(xs, ys) {
  const mx = mean(xs), my = mean(ys);
  let n = 0, d = 0;
  xs.forEach((x, i) => { n += (x - mx) * (ys[i] - my); d += (x - mx) ** 2; });
  return d ? n / d : 0;
}
function corr(a, b) {
  const ma = mean(a), mb = mean(b);
  let n = 0, da = 0, db = 0;
  a.forEach((x, i) => { n += (x - ma) * (b[i] - mb); da += (x - ma) ** 2; db += (b[i] - mb) ** 2; });
  return da > 0 && db > 0 ? n / Math.sqrt(da * db) : 0;
}
function pointInPolygon(lon, lat, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function circlePolygon(lat, lon, km, n = 64) {
  const out = [];
  for (let k = 0; k < n; k++) {
    const a = (2 * Math.PI * k) / n;
    out.push([lon + (km / (111 * Math.cos((lat * Math.PI) / 180))) * Math.cos(a), lat + (km / 111) * Math.sin(a)]);
  }
  return out;
}
function download(name, text, type = "text/csv") {
  const blob = text instanceof Blob ? text : new Blob([text], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

/* ------------------------------------------------------------------ data */
// The cube of a region comes in several files (each under 10 MB, as static hosts advise): glue them back.
function mergeCubes(parts) {
  if (parts.length === 1) return parts[0];
  const out = { raw: {} };
  for (const k of ["cell", "t", "pred", "lo", "hi", "w2", "fires", "fires_raw"]) out[k] = [].concat(...parts.map((p) => p[k] || []));
  const plats = new Set(parts.flatMap((p) => Object.keys(p.raw || {})));
  plats.forEach((pl) => { out.raw[pl] = [].concat(...parts.map((p) => (p.raw && p.raw[pl]) || new Array(p.t.length).fill(0))); });
  return out;
}

async function loadRegion(name) {
  const g = S.meta.regions[name];
  const urls = g.cube_parts ? Array.from({ length: g.cube_parts }, (_, k) => `data/${name}_cube_${k}.json`) : [`data/${name}_cube.json`];
  const [parts, mtd, ev] = await Promise.all([
    Promise.all(urls.map(getJSON)),
    getJSON(`data/${name}_mtd.json`).catch(() => null),
    getJSON(`data/${name}_events.json`).catch(() => null),
  ]);
  const cube = mergeCubes(parts);
  const n = cube.t.length;
  const D = {
    name, g, n, cell: Int32Array.from(cube.cell), t: Int16Array.from(cube.t),
    pred: Float32Array.from(cube.pred), lo: Float32Array.from(cube.lo), hi: Float32Array.from(cube.hi),
    w2: Float32Array.from(cube.w2), fires: Float32Array.from(cube.fires), firesRaw: Float32Array.from(cube.fires_raw),
    platforms: Object.keys(cube.raw), raw: {}, mtd, ev,
  };
  D.platforms.forEach((p) => { D.raw[p] = Float32Array.from(cube.raw[p]); });
  let tMax = 0;
  for (let i = 0; i < n; i++) if (D.t[i] > tMax) tMax = D.t[i];
  const last = new Date(S.meta.last_day + "T00:00:00Z");
  D.tLast = Math.min(tMax, tIndex(last.getUTCFullYear(), last.getUTCMonth() + 1));
  D.lastDay = { y: last.getUTCFullYear(), m: last.getUTCMonth() + 1, d: last.getUTCDate() };
  D.lastDayNum = dayNum(D.lastDay.y, D.lastDay.m, D.lastDay.d);
  D.partial = D.lastDay.d < daysIn(D.lastDay.y, D.lastDay.m);
  const b0 = S.meta.backbone[0].start;
  D.tFirst = tIndex(+b0.slice(0, 4), +b0.slice(5, 7));
  D.T = D.tLast + 1;
  const ids = Array.from(new Set(cube.cell));
  D.cells = Int32Array.from(ids);
  D.cellLat = new Float32Array(ids.length);
  D.cellLon = new Float32Array(ids.length);
  ids.forEach((c, k) => {
    const i = Math.floor(c / g.ncols), j = c % g.ncols;
    D.cellLat[k] = g.south + (i + 0.5) * g.deg;
    D.cellLon[k] = g.west + (j + 0.5) * g.deg;
  });
  if (ev) {
    ev.n = ev.uid.length;
    ev.platformOk = true;
  }
  return D;
}

function maskFor(D, poly) {
  const m = new Uint8Array(D.g.ncols * D.g.nrows);
  for (let k = 0; k < D.cells.length; k++) {
    if (!poly || pointInPolygon(D.cellLon[k], D.cellLat[k], poly)) m[D.cells[k]] = 1;
  }
  return m;
}

function aggregate(D, mask) {
  const T = D.T, A = { T, pred: new Float64Array(T), lo: new Float64Array(T), hi: new Float64Array(T),
    w2: new Float64Array(T), fires: new Float64Array(T), firesRaw: new Float64Array(T), raw: {}, rawTotal: new Float64Array(T) };
  D.platforms.forEach((p) => { A.raw[p] = new Float64Array(T); });
  const P = D.platforms.map((p) => [D.raw[p], A.raw[p]]);
  for (let i = 0; i < D.n; i++) {
    if (!mask[D.cell[i]]) continue;
    const t = D.t[i];
    if (t >= T) continue;
    A.pred[t] += D.pred[i]; A.lo[t] += D.lo[i]; A.hi[t] += D.hi[i]; A.w2[t] += D.w2[i];
    A.fires[t] += D.fires[i]; A.firesRaw[t] += D.firesRaw[i];
    for (const [src, dst] of P) dst[t] += src[i];
  }
  for (let t = 0; t < T; t++) for (const p of D.platforms) A.rawTotal[t] += A.raw[p][t];
  A.mtd = null;
  if (D.mtd) {
    const by = new Map();
    const M = D.mtd;
    for (let i = 0; i < M.cell.length; i++) {
      if (!mask[M.cell[i]]) continue;
      const r = by.get(M.year[i]) || { pred: 0, lo: 0, hi: 0, w2: 0 };
      r.pred += M.pred[i]; r.lo += M.lo[i]; r.hi += M.hi[i]; r.w2 += M.w2[i];
      by.set(M.year[i], r);
    }
    A.mtd = by;
  }
  return A;
}

function value(A, t, view = S.view, metric = S.metric) {
  if (t < S.D.tFirst || t >= A.T) return null;
  if (metric === "fires") return { v: view === "harm" ? A.fires[t] : A.firesRaw[t], lo: null, hi: null };
  if (view === "raw") return { v: A.rawTotal[t], lo: null, hi: null };
  const noise = 1.96 * Math.sqrt(A.w2[t]);
  return { v: A.pred[t], lo: Math.max(0, A.lo[t] - noise), hi: A.hi[t] + noise };
}

function isPartial(t) { return S.D.partial && t === S.D.tLast; }

/* Same month in earlier years (or same days of the month, for the month in progress). */
function anomaly(A, y, m, view = S.view, metric = S.metric) {
  const t = tIndex(y, m), D = S.D;
  const out = { y, m, partial: isPartial(t), history: [] };
  if (out.partial && view === "harm" && metric === "hotspots" && A.mtd) {
    const r = A.mtd.get(y) || { pred: 0, lo: 0, hi: 0, w2: 0 };
    const noise = 1.96 * Math.sqrt(r.w2);
    Object.assign(out, { v: r.pred, lo: Math.max(0, r.lo - noise), hi: r.hi + noise, mtd: true });
    for (let yy = Math.max(S.from, 2000); yy < y; yy++) {
      if (tIndex(yy, m) < D.tFirst) continue;
      const h = A.mtd.get(yy);
      out.history.push({ y: yy, v: h ? h.pred : 0 });
    }
  } else {
    const cur = value(A, t, view, metric);
    if (!cur) return null;
    Object.assign(out, cur);
    for (let yy = Math.max(S.from, 2000); yy < y; yy++) {
      const h = value(A, tIndex(yy, m), view, metric);
      if (h) out.history.push({ y: yy, v: h.v });
    }
  }
  const hv = out.history.map((h) => h.v);
  if (hv.length) {
    out.median = median(hv);
    out.mean = mean(hv);
    out.rank = 1 + hv.filter((x) => x >= out.v).length;
    out.n = hv.length;
    out.percentile = (100 * hv.filter((x) => x < out.v).length) / hv.length;
    out.ratio = out.median > 0 ? out.v / out.median : null;
    out.significant = out.lo != null && out.median != null ? (out.lo > out.median || out.hi < out.median) : null;
  }
  return out;
}

/* ------------------------------------------------------------------ fires */
function eventsInSelection() {
  const ev = S.D.ev;
  if (!ev) return null;
  if (!S.sel.poly) return null;              // null = every event of the region
  const keep = new Uint8Array(ev.n);
  for (let i = 0; i < ev.n; i++) keep[i] = pointInPolygon(ev.lon[i], ev.lat[i], S.sel.poly) ? 1 : 0;
  return keep;
}

function compareFire(y, m) {
  const ev = S.D.ev, A = S.A, D = S.D;
  if (!ev) return null;
  const keep = S.evKeep;
  const t = tIndex(y, m);
  const inSel = (i) => !keep || keep[i];
  let cur = -1;
  const current = isPartial(t) || t === D.tLast;
  const mStart = dayNum(y, m, 1), mEnd = dayNum(y, m, daysIn(y, m));
  for (let i = 0; i < ev.n; i++) {
    if (!inSel(i)) continue;
    const active = current ? ev.end[i] >= D.lastDayNum - 14 : ev.start[i] <= mEnd && ev.end[i] >= mStart;
    if (active && (cur < 0 || ev.size[i] > ev.size[cur])) cur = i;
  }
  if (cur < 0) return { none: true, current };
  const s = ev.start[cur], size = ev.size[cur];
  const sd = fromDayNum(s), ts = tIndex(sd.getUTCFullYear(), sd.getUTCMonth() + 1);
  let total = 0;
  for (let tt = 0; tt < ts && tt < A.T; tt++) total += A.firesRaw[tt];
  let bigger = 0, sinceDay = null, earlierNotable = 0;
  for (let i = 0; i < ev.n; i++) {
    if (!inSel(i) || ev.start[i] >= s) continue;
    earlierNotable++;
    if (ev.size[i] >= size) { bigger++; if (sinceDay == null || ev.start[i] > sinceDay) sinceDay = ev.start[i]; }
  }
  total = Math.max(total, earlierNotable);
  return { current, i: cur, start: s, end: ev.end[cur], size, lo: ev.lo[cur], hi: ev.hi[cur], km2: ev.km2[cur],
    peak: ev.peak[cur], sensor: ev.sensor[cur], lat: ev.lat[cur], lon: ev.lon[cur], past: total,
    percentile: total ? (100 * (total - bigger)) / total : null, rank: bigger + 1, since: sinceDay };
}

/* ------------------------------------------------------------------ season and analogs */
function seasonTiming(A) {
  const rows = [];
  const lastFull = S.D.partial ? S.D.tLast - 1 : S.D.tLast;
  for (let y = S.from; y <= S.to; y++) {
    if (tIndex(y, 1) < S.D.tFirst || tIndex(y, 12) > lastFull) continue;
    const v = []; for (let m = 1; m <= 12; m++) v.push(value(A, tIndex(y, m), "harm", "hotspots").v);
    const total = v.reduce((a, b) => a + b, 0);
    if (total < 20) continue;
    const q = (f) => {
      let acc = 0, doy = 0;
      for (let m = 1; m <= 12; m++) {
        const dm = daysIn(y, m);
        if (acc + v[m - 1] >= f * total) return doy + (dm * (f * total - acc)) / Math.max(v[m - 1], 1e-9);
        acc += v[m - 1]; doy += dm;
      }
      return doy;
    };
    const peakM = v.indexOf(Math.max(...v)) + 1;
    rows.push({ y, start: q(0.1), mid: q(0.5), end: q(0.9), peakM, total });
  }
  const out = { rows };
  if (rows.length >= 5) {
    out.startTrend = slope(rows.map((r) => r.y), rows.map((r) => r.start)) * 10;
    out.lengthTrend = slope(rows.map((r) => r.y), rows.map((r) => r.end - r.start)) * 10;
  }
  return out;
}

function analogYears(A, y, upto) {
  if (upto < 2) return [];
  const prof = (yy) => { const v = []; for (let m = 1; m <= upto; m++) { const x = value(A, tIndex(yy, m), "harm", "hotspots"); v.push(x ? x.v : 0); } return v; };
  const cur = prof(y), cs = cur.reduce((a, b) => a + b, 0);
  const out = [];
  for (let yy = Math.max(S.from, 2001); yy < y; yy++) {
    if (tIndex(yy, 1) < S.D.tFirst) continue;
    const past = prof(yy), ps = past.reduce((a, b) => a + b, 0);
    const shape = corr(cur, past);
    const level = 1 - Math.abs(Math.log1p(cs) - Math.log1p(ps)) / Math.max(Math.log1p(cs), 1);
    let rest = 0; for (let m = upto + 1; m <= 12; m++) rest += value(A, tIndex(yy, m), "harm", "hotspots").v;
    out.push({ y: yy, sim: 0.7 * shape + 0.3 * level, rest, sofar: ps });
  }
  return out.sort((a, b) => b.sim - a.sim).slice(0, 3);
}

/* ------------------------------------------------------------------ calendar */
function calendarSVG() {
  const A = S.A, D = S.D;
  const years = [];
  for (let y = S.to; y >= S.from; y--) years.push(y);
  const cw = 46, ch = 21, left = 44, top = 22, stripW = 96, gap = 12;
  const W = left + 12 * cw + gap + stripW + 4, H = top + years.length * ch + 4;
  const anomalyMode = S.mode === "anomaly";
  let vmax = 0;
  for (const y of years) for (let m = 1; m <= 12; m++) {
    const x = value(A, tIndex(y, m)); if (x && x.v > vmax) vmax = x.v;
  }
  const clim = {};
  if (anomalyMode) for (let m = 1; m <= 12; m++) {
    const vals = [];
    for (const y of years) { const t = tIndex(y, m); if (isPartial(t)) continue; const x = value(A, t); if (x) vals.push(x.v); }
    clim[m] = median(vals);
  }
  const parts = [];
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Calendar of ${S.metric} per month and year">`);
  parts.push(`<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
    <line x1="0" y1="0" x2="0" y2="6" stroke="#ffffff" stroke-opacity=".75" stroke-width="2"/></pattern>
    <pattern id="hatchDark" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
    <line x1="0" y1="0" x2="0" y2="6" stroke="#14181d" stroke-opacity=".45" stroke-width="1.6"/></pattern></defs>`);
  parts.push(`<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;font-variant-numeric:tabular-nums}</style>`);
  parts.push(`<rect x="0" y="0" width="${W}" height="${H}" fill="#ffffff"/>`);
  MONTHS.forEach((mn, k) => parts.push(`<text x="${left + k * cw + cw / 2}" y="${top - 7}" font-size="11" fill="#5b6570" text-anchor="middle">${mn}</text>`));
  parts.push(`<text x="${left + 12 * cw + gap}" y="${top - 7}" font-size="11" fill="#5b6570">Satellites</text>`);
  const focusT = tIndex(S.focus.y, S.focus.m);
  years.forEach((y, r) => {
    const yy = top + r * ch;
    parts.push(`<text x="${left - 6}" y="${yy + ch / 2 + 4}" font-size="11" fill="#1c2229" text-anchor="end">${y}</text>`);
    for (let m = 1; m <= 12; m++) {
      const t = tIndex(y, m), x = left + (m - 1) * cw;
      const val = value(A, t);
      if (!val) {
        parts.push(`<rect x="${x + 1}" y="${yy + 1}" width="${cw - 2}" height="${ch - 2}" fill="#f1efe9"/>`);
        continue;
      }
      let fill, label;
      const partial = isPartial(t);
      if (anomalyMode) {
        let ratio = null;
        if (partial) { const an = anomaly(A, y, m); ratio = an && an.ratio; }
        else if (clim[m] > 0) ratio = val.v / clim[m];
        if (ratio == null) { fill = "#e9e6df"; label = ""; }
        else {
          const l2 = Math.log2(Math.max(ratio, 1 / 16));
          fill = ramp(DIVERGING, 0.5 + Math.max(-2, Math.min(2, l2)) / 4);
          label = ratio >= 2 ? `×${ratio.toFixed(ratio >= 10 ? 0 : 1)}` : `${ratio >= 1 ? "+" : "−"}${Math.round(Math.abs(ratio - 1) * 100)}%`;
        }
      } else {
        fill = val.v > 0 ? ramp(CIVIDIS, Math.log1p(val.v) / Math.log1p(Math.max(vmax, 1))) : "#f7f6f2";
        label = val.v > 0 ? fmt(val.v) : "0";
      }
      const ink = fill.startsWith("rgb") ? inkFor(fill) : "#5b6570";
      const uncertain = val.lo != null && val.v > 0 && val.hi - val.lo > val.v;
      const lowConf = t < tIndex(2002, 7) || (t === tIndex(2002, 7));
      const tip = tipText(y, m, val, partial, lowConf, uncertain);
      parts.push(`<g class="cell" data-y="${y}" data-m="${m}" data-tip="${esc(tip)}">`);
      parts.push(`<rect class="fill" x="${x + 1}" y="${yy + 1}" width="${cw - 2}" height="${ch - 2}" rx="2" fill="${fill}"/>`);
      if (uncertain || (S.view === "harm" && lowConf)) parts.push(`<rect x="${x + 1}" y="${yy + 1}" width="${cw - 2}" height="${ch - 2}" rx="2" fill="url(#${ink === "#ffffff" ? "hatch" : "hatchDark"})"/>`);
      if (partial) parts.push(`<path d="M${x + cw - 9} ${yy + 1} h8 v8 z" fill="#c2410c"/>`);
      parts.push(`<text x="${x + cw / 2}" y="${yy + ch / 2 + 4}" font-size="10.5" fill="${ink}" text-anchor="middle">${label}</text>`);
      if (t === focusT) parts.push(`<rect x="${x}" y="${yy}" width="${cw}" height="${ch}" rx="3" fill="none" stroke="#c2410c" stroke-width="2.5"/>`);
      parts.push(`</g>`);
    }
    // satellite strip: share of raw detections per platform this year
    const tot = {}; let sum = 0;
    D.platforms.forEach((p) => { tot[p] = 0; for (let m = 1; m <= 12; m++) { const t = tIndex(y, m); if (t < A.T) tot[p] += A.raw[p][t]; } sum += tot[p]; });
    let sx = left + 12 * cw + gap;
    if (sum > 0) {
      ["Terra", "Aqua", "SNPP", "NOAA20", "NOAA21"].filter((p) => tot[p] > 0).forEach((p) => {
        const w = (stripW * tot[p]) / sum;
        parts.push(`<rect x="${sx}" y="${yy + 4}" width="${Math.max(w, 0.5)}" height="${ch - 8}" fill="${PLATFORM_COLOR[p]}"><title>${y}: ${PLATFORM_LABEL[p]} ${Math.round((100 * tot[p]) / sum)}% of raw detections</title></rect>`);
        sx += w;
      });
    }
  });
  parts.push(`</svg>`);
  return parts.join("");
}

function tipText(y, m, val, partial, lowConf, uncertain) {
  const what = S.metric === "fires" ? "fires" : "hotspots";
  const series = S.view === "harm" ? `harmonized ${what}` : `raw ${what}, every satellite`;
  let s = `${MONTHS_LONG[m - 1]} ${y}${partial ? ` (1-${S.D.lastDay.d}, in progress)` : ""}: ${num(val.v)} ${series}`;
  if (val.lo != null) s += ` (95% range ${num(val.lo)}-${num(val.hi)})`;
  if (uncertain) s += ". Hatched: the range is wider than the value";
  if (lowConf && S.view === "harm") s += ". Terra-only period: lower confidence";
  return s + ".";
}

function legendHTML() {
  if (S.mode === "anomaly") {
    const stops = DIVERGING.map((c, i) => `${c} ${(100 * i) / (DIVERGING.length - 1)}%`).join(",");
    return `<span>÷4</span><span class="ramp" style="background:linear-gradient(90deg,${stops})"></span><span>×4 vs the month's median</span>`;
  }
  const stops = CIVIDIS.map((c, i) => `${c} ${(100 * i) / (CIVIDIS.length - 1)}%`).join(",");
  const hatch = S.view === "harm" ? `<span><span class="sw" style="background:repeating-linear-gradient(45deg,#777 0 2px,#fff 2px 5px)"></span>uncertain</span>` : "";
  const partial = S.D.partial ? `<span><span class="sw" style="background:#c2410c;border-color:#c2410c"></span>month in progress</span>` : "";
  return `<span>few</span><span class="ramp" style="background:linear-gradient(90deg,${stops})"></span><span>many (log scale)</span>${hatch}${partial}`;
}

/* ------------------------------------------------------------------ cards */
function renderAnomalyCard() {
  const { y, m } = S.focus;
  const an = anomaly(S.A, y, m);
  const el = $("anomalyCard");
  const what = S.metric === "fires" ? "fires" : "hotspots";
  if (!an) { el.innerHTML = `<h2>${MONTHS_LONG[m - 1]} ${y}</h2><p class="empty">No data for this month.</p>`; return; }
  const period = an.mtd ? `1-${S.D.lastDay.d} ${MONTHS_LONG[m - 1]} ${y}` : `${MONTHS_LONG[m - 1]} ${y}`;
  const compare = an.mtd ? `the same days of ${MONTHS_LONG[m - 1]}` : `every ${MONTHS_LONG[m - 1]}`;
  let badge = "";
  if (an.ratio != null) {
    const cls = an.ratio >= 1.25 ? "up" : an.ratio <= 0.8 ? "down" : "flat";
    const txt = an.significant ? (an.ratio >= 1 ? "Significantly above normal" : "Significantly below normal")
      : an.significant === false ? "Within the uncertainty of normal" : (an.ratio >= 1 ? "Above the median" : "Below the median");
    badge = `<span class="badge ${cls}">${txt}</span>`;
  }
  const partialNote = an.partial && !an.mtd ? `<p class="hint">Month in progress: switch to Harmonized hotspots for a fair same-days comparison.</p>` : "";
  el.innerHTML = `
    <div class="card-head"><h2>${period}</h2><span class="kicker">${an.history.length ? `vs ${compare} since ${an.history[0].y}` : "no earlier year to compare"}</span></div>
    <div class="big">${num(an.v)} <span class="range">${S.view === "harm" ? "harmonized" : "raw"} ${what}</span></div>
    ${an.lo != null ? `<div class="range">95% range ${num(an.lo)} to ${num(an.hi)}</div>` : ""}
    <div class="stat-row">
      <div class="stat"><b>${an.median != null ? num(an.median) : "–"}</b><span>median</span></div>
      <div class="stat"><b>${an.ratio != null ? "×" + an.ratio.toFixed(an.ratio >= 10 ? 0 : 2) : "–"}</b><span>vs median</span></div>
      <div class="stat"><b>${an.rank ? `${an.rank} of ${an.n + 1}` : "–"}</b><span>rank</span></div>
    </div>
    ${badge}${partialNote}
    ${miniBars(an)}`;
}

function miniBars(an) {
  if (!an.history.length) return "";
  const all = [...an.history, { y: an.y, v: an.v }];
  const W = 320, H = 70, bw = W / all.length, vmax = Math.max(...all.map((h) => h.v), 1);
  const bars = all.map((h, i) => {
    const bh = (H - 14) * (h.v / vmax);
    return `<rect x="${i * bw + 0.5}" y="${H - 12 - bh}" width="${Math.max(bw - 1, 1)}" height="${bh}" fill="${h.y === an.y ? "#c2410c" : "#8a8779"}"><title>${h.y}: ${num(h.v)}</title></rect>`;
  }).join("");
  const my = H - 12 - (H - 14) * ((an.median || 0) / vmax);
  return `<svg class="mini" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Same month in every year">
    ${bars}<line x1="0" x2="${W}" y1="${my}" y2="${my}" stroke="#1c2229" stroke-dasharray="3 3"/>
    <text x="0" y="${H - 1}" font-size="10" fill="#5b6570">${all[0].y}</text><text x="${W}" y="${H - 1}" font-size="10" fill="#5b6570" text-anchor="end">${an.y}</text></svg>`;
}

function renderFireCard() {
  const el = $("fireCard");
  const { y, m } = S.focus;
  const f = compareFire(y, m);
  if (!f) { el.innerHTML = `<h2>Fire comparator</h2><p class="empty">No fire events computed for this region.</p>`; return; }
  if (f.none) {
    el.innerHTML = `<h2>${f.current ? "Current fire" : "Largest fire this month"}</h2><p class="empty">No fire ${f.current ? "active in the last 14 days" : "active this month"} in this area.</p>`;
    return;
  }
  const dur = f.end - f.start + 1;
  const since = sinceText(f.since, f.start) || `the largest on record (since ${S.meta.backbone[0].start.slice(0, 4)})`;
  const share = f.rank === 1 ? `all ${num(f.past)} earlier fires` : `${f.percentile.toFixed(f.percentile >= 99 ? 1 : 0)}% of the ${num(f.past)} earlier fires`;
  const verdict = f.percentile != null && f.percentile >= 90 ? `Larger than ${share} in this area: ${since}.`
    : f.percentile != null ? `A typical fire for this area: larger than ${f.percentile.toFixed(0)}% of ${num(f.past)} earlier fires.` : "";
  el.innerHTML = `
    <div class="card-head"><h2>${f.current ? "Current fire" : "Largest fire this month"}</h2><span class="kicker">${f.current ? "active in the last 14 days" : MONTHS_LONG[m - 1] + " " + y}</span></div>
    <div class="big">${num(f.size)} <span class="range">harmonized hotspots</span></div>
    <div class="range">95% range ${num(f.lo)} to ${num(f.hi)} · peak day ${num(f.peak)}</div>
    <div class="stat-row">
      <div class="stat"><b>${isoDay(f.start)}</b><span>start</span></div>
      <div class="stat"><b>${dur} day${dur > 1 ? "s" : ""}</b><span>duration</span></div>
      <div class="stat"><b>${num(f.km2)} km²</b><span>footprint (approx.)</span></div>
    </div>
    <p class="brief">${verdict}</p>
    <p class="hint">Fire = hotspots touching in space (~5 km) and time (up to 2 days apart). Seen mostly by ${f.sensor}. Centre ${f.lat.toFixed(2)}, ${f.lon.toFixed(2)}.</p>`;
  S.currentFire = f;
}

function briefText() {
  const { y, m } = S.focus;
  const an = anomaly(S.A, y, m, "harm", "hotspots");
  if (!an) return "";
  const area = S.sel.label;
  const period = an.mtd ? `1-${S.D.lastDay.d} ${MONTHS_LONG[m - 1]} ${y}` : `${MONTHS_LONG[m - 1]} ${y}`;
  const parts = [];
  if (an.median) {
    const r = an.ratio, word = r >= 1 ? "above" : "below";
    parts.push(`${area}, ${period}: ${num(an.v)} harmonized hotspots (95% range ${num(an.lo)}-${num(an.hi)}), ${Math.abs((r - 1) * 100).toFixed(0)}% ${word} the ${an.n}-year median${an.mtd ? " for the same days" : ""} (${num(an.median)}); rank ${an.rank} of ${an.n + 1}${an.significant ? ", a significant anomaly" : ""}.`);
  } else parts.push(`${area}, ${period}: ${num(an.v)} harmonized hotspots.`);
  const f = compareFire(y, m);
  if (f && !f.none && f.percentile != null) {
    const dur = f.end - f.start + 1;
    const head = `The ${f.current ? "current" : "largest"} fire (${isoDay(f.start)} to ${isoDay(f.end)}, ${dur} day${dur > 1 ? "s" : ""}, ${num(f.size)} harmonized hotspots)`;
    const share = f.rank === 1 ? `all ${num(f.past)} earlier fires` : `${f.percentile.toFixed(f.percentile >= 99 ? 1 : 0)}% of the ${num(f.past)} earlier fires`;
    parts.push(f.percentile >= 90 ? `${head} is larger than ${share} in this area: ${sinceText(f.since, f.start) || "the largest on record"}.`
      : `${head} is a typical fire for this area.`);
  }
  const an2 = analogYears(S.A, y, an.partial ? m - 1 : m);
  if (an2.length >= 2) parts.push(`So far this year looks most like ${an2[0].y} and ${an2[1].y}.`);
  const st = seasonTiming(S.A);
  if (st.startTrend != null) {
    const d = (v) => `${Math.abs(v).toFixed(0)} day${Math.round(Math.abs(v)) === 1 ? "" : "s"}`;
    const startTxt = Math.abs(st.startTrend) < 1 ? "starts at the same time as before (less than a day per decade of change)"
      : `starts ${d(st.startTrend)} per decade ${st.startTrend < 0 ? "earlier" : "later"}`;
    const lenTxt = Math.abs(st.lengthTrend) < 1 ? "its length has not changed measurably"
      : `it lasts ${d(st.lengthTrend)} per decade ${st.lengthTrend > 0 ? "longer" : "shorter"}`;
    parts.push(`Since ${st.rows[0].y} the fire season ${startTxt}, and ${lenTxt}.`);
  }
  return parts.join(" ");
}

function renderBrief() {
  const text = briefText();
  $("briefCard").innerHTML = `<div class="card-head"><h2>Situation brief</h2><button type="button" id="copyBrief">Copy</button></div>
    <p class="brief">${esc(text)}</p><p class="hint">Plain-language summary for a situation report, generated from the harmonized series.</p>`;
  $("copyBrief").onclick = () => navigator.clipboard && navigator.clipboard.writeText(text).then(() => { $("copyBrief").textContent = "Copied"; });
}

function renderSeason() {
  const st = seasonTiming(S.A);
  const el = $("seasonCard");
  if (!st.rows.length) { el.innerHTML = `<h2>Fire season</h2><p class="empty">Not enough activity in this area.</p>`; return; }
  const doy = (d) => { const dt = new Date(Date.UTC(2001, 0, 1) + (d - 1) * DAY_MS); return `${dt.getUTCDate()} ${MONTHS[dt.getUTCMonth()]}`; };
  const last = st.rows[st.rows.length - 1];
  const trend = st.startTrend != null ? `<div class="stat-row">
      <div class="stat"><b>${st.startTrend > 0 ? "+" : ""}${st.startTrend.toFixed(1)} d</b><span>start shift per decade</span></div>
      <div class="stat"><b>${st.lengthTrend > 0 ? "+" : ""}${st.lengthTrend.toFixed(1)} d</b><span>length change per decade</span></div></div>` : "";
  const W = 320, H = 90, x0 = st.rows[0].y, x1 = last.y, sx = (y) => 6 + ((W - 12) * (y - x0)) / Math.max(x1 - x0, 1), sy = (d) => 6 + ((H - 18) * d) / 366;
  const lines = st.rows.map((r) => `<line x1="${sx(r.y)}" x2="${sx(r.y)}" y1="${sy(r.start)}" y2="${sy(r.end)}" stroke="#8a8779" stroke-width="3"><title>${r.y}: ${doy(r.start)} to ${doy(r.end)}</title></line><circle cx="${sx(r.y)}" cy="${sy(r.mid)}" r="2.2" fill="#c2410c"/>`).join("");
  el.innerHTML = `<div class="card-head"><h2>Fire season</h2><span class="kicker">10% to 90% of each year's activity</span></div>
    <p class="brief">${last.y}: ${doy(last.start)} to ${doy(last.end)}, peak in ${MONTHS_LONG[last.peakM - 1]}.</p>${trend}
    <svg class="mini" viewBox="0 0 ${W} ${H}" role="img" aria-label="Season start and end per year">${lines}
    <text x="6" y="${H - 2}" font-size="10" fill="#5b6570">${x0}</text><text x="${W - 6}" y="${H - 2}" font-size="10" fill="#5b6570" text-anchor="end">${x1}</text></svg>
    <p class="hint">Estimated from monthly harmonized totals (vertical axis: day of year, January at the top).</p>`;
}

function renderAnalogs() {
  const { y, m } = S.focus;
  const upto = isPartial(tIndex(y, m)) ? m - 1 : m;
  const an = analogYears(S.A, y, upto);
  const el = $("analogCard");
  if (!an.length) { el.innerHTML = `<h2>Analog years</h2><p class="empty">Pick a month after February to compare whole seasons.</p>`; return; }
  el.innerHTML = `<div class="card-head"><h2>Analog years</h2><span class="kicker">January to ${MONTHS_LONG[upto - 1]}</span></div>
    <table class="watch"><thead><tr><th>Year</th><th class="num">Similarity</th><th class="num">Same period</th><th class="num">Rest of year</th></tr></thead>
    <tbody>${an.map((a) => `<tr data-y="${a.y}"><td>${a.y}</td><td class="num">${(a.sim * 100).toFixed(0)}%</td><td class="num">${num(a.sofar)}</td><td class="num">${num(a.rest)}</td></tr>`).join("")}</tbody></table>
    <p class="hint">Past seasons with the most similar shape and level so far. What happened next in those years is a guide to the rest of ${y}.</p>`;
  el.querySelectorAll("tr[data-y]").forEach((tr) => { tr.onclick = () => setFocus(+tr.dataset.y, m); });
}

function renderWatch() {
  const list = S.watch[S.region] || [];
  const { y, m } = S.focus;
  $("watchMonth").textContent = `${MONTHS[m - 1]} ${y}`;
  const rows = S.watchAgg.map((w) => ({ w, an: anomaly(w.A, y, m, "harm", "hotspots") }))
    .sort((a, b) => ((b.an && b.an.ratio) || -1) - ((a.an && a.an.ratio) || -1));
  if (!list.length) { $("watch").innerHTML = `<tr><td class="empty">No watched areas for this region.</td></tr>`; return; }
  $("watch").innerHTML = `<thead><tr><th>Area</th><th class="num">Hotspots</th><th class="num">vs median</th><th class="num">Rank</th></tr></thead><tbody>${
    rows.map(({ w, an }) => `<tr data-name="${esc(w.area.name)}" tabindex="0"><td>${esc(w.area.name)}</td><td class="num">${an ? num(an.v) : "–"}</td>
      <td class="num">${an && an.ratio != null ? (an.significant ? "<b>" : "") + "×" + an.ratio.toFixed(2) + (an.significant ? "</b>" : "") : "–"}</td>
      <td class="num">${an && an.rank ? an.rank + "/" + (an.n + 1) : "–"}</td></tr>`).join("")}</tbody>`;
  $("watch").querySelectorAll("tr[data-name]").forEach((tr) => {
    const go = () => selectArea(list.find((a) => a.name === tr.dataset.name));
    tr.onclick = go;
    tr.onkeydown = (e) => { if (e.key === "Enter") go(); };
  });
}

function renderMethod() {
  const v = S.meta.validation || {};
  const ts = v.Aqua_time_split_test || {};
  const loro = Object.entries(v).filter(([k]) => k.startsWith("Aqua_unseen_region_"));
  const label = (r) => (S.meta.regions[r] ? S.meta.regions[r].label : r);
  $("method").innerHTML = `<div class="card-head"><h2>How the bridge works</h2><span class="kicker">validated, not assumed</span></div>
    <p>FIRMS hotspots from five satellites cannot be added up: they differ in <b>resolution</b> (1 km MODIS vs 375 m VIIRS pixels), <b>sensitivity</b> (MODIS misses small fires)
    and <b>observation schedule</b> (Terra passes at 10:30, the others at 13:30, and Terra and Aqua drift). Ember Bridge learns from the years when Aqua and Suomi NPP flew together
    how many VIIRS hotspots each MODIS hotspot stands for, depending on fire power, pixel size, cluster size and the real local time of the pass, plus a correction for the fires MODIS never saw, set so that the weights add up to what Suomi NPP saw in those years.
    Every hotspot gets a weight with a 95% range; the calendar adds them up along one backbone: Terra, then Aqua, then Suomi NPP, then NOAA-20.</p>
    <ul>
      ${ts.months ? `<li><b>Held-out years</b> (${esc(ts.test || "")}, never used for fitting): typical monthly error ${ts.median_abs_pct_error_harmonized}% harmonized vs ${ts.median_abs_pct_error_raw}% raw MODIS; correlation ${ts.correlation_harmonized}; the 95% range contains the truth in ${ts.interval_coverage_pct}% of ${ts.months} months.</li>` : ""}
      ${ts.local_1deg_cells && ts.local_1deg_cells.cell_months ? `<li><b>Small areas</b> (1° cells, about 100 km, held-out years): typical monthly error ${ts.local_1deg_cells.median_abs_pct_error_harmonized}% harmonized vs ${ts.local_1deg_cells.median_abs_pct_error_raw}% raw; the 95% range contains the truth in ${ts.local_1deg_cells.interval_coverage_pct}% of ${num(ts.local_1deg_cells.cell_months)} cell-months.</li>` : ""}
      ${loro.map(([k, r]) => r.months ? `<li><b>Never-seen region</b>, ${esc(label(k.replace("Aqua_unseen_region_", "")))}: ${r.median_abs_pct_error_harmonized}% error harmonized vs ${r.median_abs_pct_error_raw}% raw (${r.months} months).</li>` : "").join("")}
      <li>Fires are counted too: VIIRS splits what MODIS sees as one fire, so MODIS-era fire counts are scaled by the ratio learned on the overlap years${v.fire_count_factor_global ? ` (×${v.fire_count_factor_global} overall)` : ""}.</li>
      <li>Near-real-time files have no "static source" flag: gas flares, mines and volcanoes learned from the science-quality archive are masked, so the month in progress is compared fairly.</li>
    </ul>`;
}

/* ------------------------------------------------------------------ map */
// Background imagery from NASA GIBS (Global Imagery Browse Services, the imagery behind NASA Worldview):
// Blue Marble by default, or the NOAA-20 VIIRS true-colour image of the day before the last data day.
// GIBS needs no key. Its Web Mercator tiles are blank at zoom 0, hence minZoom 2.
const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/";
const GIBS_CREDIT = 'Imagery <a href="https://earthdata.nasa.gov">NASA GIBS</a> (ESDIS)';
const OSM_CREDIT = "&copy; OpenStreetMap contributors &copy; CARTO";

function dayBefore(iso) {
  const t = Date.parse(`${iso}T00:00:00Z`);
  return Number.isFinite(t) ? new Date(t - 864e5).toISOString().slice(0, 10) : "default";
}

function initMap() {
  S.map = L.map("map", { preferCanvas: true, worldCopyJump: true, zoomSnap: 0.25, minZoom: 2 });
  const day = dayBefore(S.meta.last_day);
  const bases = {
    "NASA Blue Marble": L.tileLayer(`${GIBS}BlueMarble_NextGeneration/default/EPSG3857_500m/{z}/{y}/{x}.jpeg`,
      { maxNativeZoom: 8, maxZoom: 12, opacity: 0.65, attribution: GIBS_CREDIT }),
    [`NASA satellite image, ${day === "default" ? "latest day" : day} (NOAA-20 VIIRS)`]: L.tileLayer(
      `${GIBS}VIIRS_NOAA20_CorrectedReflectance_TrueColor/default/${day}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg`,
      { maxNativeZoom: 9, maxZoom: 12, opacity: 0.85, attribution: GIBS_CREDIT }),
    "Street map (OpenStreetMap)": L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      { maxZoom: 12, subdomains: "abcd", attribution: OSM_CREDIT }),
  };
  const labels = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}{r}.png",
    { maxZoom: 12, subdomains: "abcd", zIndex: 5, attribution: OSM_CREDIT });
  bases["NASA Blue Marble"].addTo(S.map);
  labels.addTo(S.map);
  L.control.layers(bases, { "Place names": labels }, { position: "topleft" }).addTo(S.map);
  S.layers.cells = L.layerGroup().addTo(S.map);
  S.layers.fires = L.layerGroup().addTo(S.map);
  S.layers.sel = L.featureGroup().addTo(S.map);
  S.layers.region = L.layerGroup().addTo(S.map);
  const draw = new L.Control.Draw({
    position: "topright",
    draw: { polyline: false, circle: false, marker: false, circlemarker: false,
      polygon: { shapeOptions: { color: "#c2410c", weight: 2, fillOpacity: 0.05 } },
      rectangle: { shapeOptions: { color: "#c2410c", weight: 2, fillOpacity: 0.05 } } },
    edit: false,
  });
  S.map.addControl(draw);
  S.map.on(L.Draw.Event.CREATED, (e) => {
    const ll = e.layer.getLatLngs()[0].map((p) => [p.lng, p.lat]);
    setSelection({ poly: ll, label: e.layerType === "rectangle" ? "Drawn rectangle" : "Drawn polygon" });
  });
}

function drawMap(fit) {
  const D = S.D, g = D.g, A = S.A;
  S.layers.region.clearLayers();
  L.rectangle([[g.south, g.west], [g.north, g.east]], { color: "#1c2229", weight: 1, fill: false, dashArray: "4 4", interactive: false }).addTo(S.layers.region);
  S.layers.cells.clearLayers();
  const t = tIndex(S.focus.y, S.focus.m);
  const vals = new Map();
  for (let i = 0; i < D.n; i++) {
    if (D.t[i] !== t) continue;
    let v;
    if (S.metric === "fires") v = S.view === "harm" ? D.fires[i] : D.firesRaw[i];
    else if (S.view === "raw") { v = 0; for (const p of D.platforms) v += D.raw[p][i]; }
    else v = D.pred[i];
    if (v > 0) vals.set(D.cell[i], (vals.get(D.cell[i]) || 0) + v);
  }
  let vmax = 1; vals.forEach((v) => { if (v > vmax) vmax = v; });
  const renderer = L.canvas({ padding: 0.3 });
  vals.forEach((v, c) => {
    const i = Math.floor(c / g.ncols), j = c % g.ncols;
    const s = g.south + i * g.deg, w = g.west + j * g.deg;
    L.rectangle([[s, w], [s + g.deg, w + g.deg]], { renderer, stroke: false, fillColor: ramp(CIVIDIS, 0.25 + 0.75 * Math.log1p(v) / Math.log1p(vmax)),
      fillOpacity: 0.78, interactive: false }).addTo(S.layers.cells);
  });
  S.layers.fires.clearLayers();
  const f = S.currentFire;
  if (f && !f.none) {
    L.circleMarker([f.lat, f.lon], { radius: 6 + Math.min(18, Math.sqrt(f.size) / 3), color: "#c2410c", weight: 2.5, fillColor: "#fcc419", fillOpacity: 0.9 })
      .bindTooltip(`${f.current ? "Current" : "Largest"} fire: ${num(f.size)} harmonized hotspots, from ${isoDay(f.start)}`).addTo(S.layers.fires);
  }
  S.layers.sel.clearLayers();
  if (S.sel.poly) L.polygon(S.sel.poly.map(([x, y]) => [y, x]), { color: "#c2410c", weight: 2, fillOpacity: 0.04 }).addTo(S.layers.sel);
  if (fit) {
    if (S.sel.poly) S.map.fitBounds(S.layers.sel.getBounds(), { padding: [20, 20] });
    else S.map.fitBounds([[g.south, g.west], [g.north, g.east]], { padding: [10, 10] });
  }
}

/* ------------------------------------------------------------------ state changes */
function setSelection(sel, fit = true) {
  S.sel = sel;
  S.A = aggregate(S.D, maskFor(S.D, sel.poly));
  S.evKeep = eventsInSelection();
  $("selLabel").textContent = sel.label;
  renderAll(fit);
}

function selectArea(area) {
  if (!area) return;
  $("cLat").value = area.lat; $("cLon").value = area.lon; $("cRad").value = area.radius_km;
  setSelection({ poly: circlePolygon(area.lat, area.lon, area.radius_km), label: area.name });
}

function setFocus(y, m) {
  const t = Math.max(S.D.tFirst, Math.min(S.D.tLast, tIndex(y, m)));
  S.focus = { y: yearOf(t), m: monthOf(t) };
  renderAll(false);
}

function renderAll(fit) {
  $("fYear").value = S.focus.y; $("fMonth").value = S.focus.m;
  const what = S.metric === "fires" ? "Fires" : "Hotspots";
  $("calTitle").textContent = `${what} per month, ${S.view === "harm" ? "harmonized (VIIRS-equivalent)" : "raw: every satellite added up"}`;
  $("legend").innerHTML = legendHTML();
  $("calendar").innerHTML = calendarSVG();
  $("calNote").textContent = S.view === "harm"
    ? "Hatched cells: 95% range wider than the value, or the 2000-2002 Terra-only period. Click a month to inspect it."
    : "Raw counts jump each time a satellite is added (2002, 2012, 2018, 2023): this is the problem the harmonized series fixes.";
  bindCalendar();
  renderAnomalyCard();
  renderFireCard();
  renderBrief();
  renderSeason();
  renderAnalogs();
  renderWatch();
  drawMap(fit);
  try { history.replaceState(null, "", `#${S.region}/${S.focus.y}-${String(S.focus.m).padStart(2, "0")}`); } catch (e) { /* ignore */ }
}

function bindCalendar() {
  const tip = $("tip");
  $("calendar").querySelectorAll(".cell").forEach((g) => {
    g.addEventListener("click", () => setFocus(+g.dataset.y, +g.dataset.m));
    g.addEventListener("mousemove", (e) => {
      tip.hidden = false; tip.textContent = g.dataset.tip;
      tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 290) + "px"; tip.style.top = e.clientY + 14 + "px";
    });
    g.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
}

async function setRegion(name, focus) {
  S.region = name;
  document.querySelectorAll("#regions button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.region === name)));
  $("calendar").innerHTML = `<p class="empty">Loading ${esc(S.meta.regions[name].label)}...</p>`;
  S.D = await loadRegion(name);
  const D = S.D;
  const y0 = yearOf(D.tFirst), y1 = yearOf(D.tLast);
  S.from = y0; S.to = y1;
  const opts = (a, b, sel) => { let s = ""; for (let y = b; y >= a; y--) s += `<option value="${y}"${y === sel ? " selected" : ""}>${y}</option>`; return s; };
  $("yFrom").innerHTML = opts(y0, y1, y0); $("yTo").innerHTML = opts(y0, y1, y1); $("fYear").innerHTML = opts(y0, y1, y1);
  $("fMonth").innerHTML = MONTHS_LONG.map((mn, k) => `<option value="${k + 1}">${mn}</option>`).join("");
  S.focus = focus || { y: yearOf(D.tLast), m: monthOf(D.tLast) };
  S.watchAgg = (S.watch[name] || []).map((area) => ({ area, A: aggregate(D, maskFor(D, circlePolygon(area.lat, area.lon, area.radius_km))) }));
  $("highlights").innerHTML = (HIGHLIGHTS[name] || []).map(([label], k) => `<button type="button" data-k="${k}">${esc(label)}</button>`).join("");
  $("highlights").querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      const h = HIGHLIGHTS[name][+b.dataset.k][1];
      S.focus = { y: h.y, m: h.m };
      if (h.area) selectArea((S.watch[name] || []).find((a) => a.name === h.area));
      else setSelection({ poly: null, label: `${S.meta.regions[name].label} (whole region)` });
    };
  });
  $("fresh").textContent = `FIRMS data to ${D.lastDay.d} ${MONTHS_LONG[D.lastDay.m - 1]} ${D.lastDay.y} · updated ${S.meta.generated.slice(0, 10)}`;
  renderMethod();
  setSelection({ poly: null, label: `${S.meta.regions[name].label} (whole region)` });
}

function bindControls() {
  const seg = (attr, key) => document.querySelectorAll(`[data-${attr}]`).forEach((b) => {
    b.onclick = () => {
      S[key] = b.dataset[attr];
      document.querySelectorAll(`[data-${attr}]`).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      renderAll(false);
    };
  });
  seg("view", "view"); seg("metric", "metric"); seg("mode", "mode");
  $("yFrom").onchange = () => { S.from = Math.min(+$("yFrom").value, S.to); renderAll(false); };
  $("yTo").onchange = () => { S.to = Math.max(+$("yTo").value, S.from); renderAll(false); };
  $("fYear").onchange = () => setFocus(+$("fYear").value, +$("fMonth").value);
  $("fMonth").onchange = () => setFocus(+$("fYear").value, +$("fMonth").value);
  $("resetSel").onclick = () => setSelection({ poly: null, label: `${S.meta.regions[S.region].label} (whole region)` });
  $("circleForm").onsubmit = (e) => {
    e.preventDefault();
    const lat = +$("cLat").value, lon = +$("cLon").value, km = +$("cRad").value;
    setSelection({ poly: circlePolygon(lat, lon, km), label: `${km} km around ${lat.toFixed(2)}, ${lon.toFixed(2)}` });
  };
  $("dlCalendar").onclick = downloadCalendar;
  $("dlHotspots").onclick = downloadHotspots;
  $("dlPng").onclick = downloadPng;
}

/* ------------------------------------------------------------------ downloads */
function slug(s) { return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, ""); }

function downloadCalendar() {
  const A = S.A, D = S.D;
  const head = ["year", "month", "harmonized_hotspots", "low_95", "high_95", "raw_hotspots_all_satellites",
    ...D.platforms.map((p) => `raw_${p}`), "harmonized_fires", "raw_fires", "terra_only_period", "uncertain", "month_in_progress"];
  const rows = [head.join(",")];
  for (let y = S.from; y <= S.to; y++) for (let m = 1; m <= 12; m++) {
    const t = tIndex(y, m), h = value(A, t, "harm", "hotspots");
    if (!h) continue;
    rows.push([y, m, h.v.toFixed(2), h.lo.toFixed(2), h.hi.toFixed(2), A.rawTotal[t], ...D.platforms.map((p) => A.raw[p][t]),
      A.fires[t].toFixed(2), A.firesRaw[t], t <= tIndex(2002, 7), h.hi - h.lo > h.v && h.v > 0, isPartial(t)].join(","));
  }
  const header = `# Ember Bridge harmonized burning-activity calendar. Area: ${S.sel.label}. Region: ${D.g.label}. Grid ${D.g.deg} deg. FIRMS data to ${S.meta.last_day}.\n`;
  download(`ember-bridge_${slug(S.sel.label)}_calendar.csv`, header + rows.join("\n") + "\n");
}

async function downloadHotspots() {
  const note = $("dlNote");
  note.textContent = "Preparing the hotspots...";
  let R;
  try { R = S.D.recent || (S.D.recent = await getJSON(`data/${S.region}_recent.json`)); } catch (e) { note.textContent = "No recent hotspots available for this region."; return; }
  const poly = S.sel.poly;
  const rows = ["time_utc,lat,lon,satellite,sensor,daynight,confidence,frp_mw,harmonized_weight,weight_low_95,weight_high_95,fire_id"];
  let n = 0;
  for (let i = 0; i < R.t.length; i++) {
    if (poly && !pointInPolygon(R.lon[i], R.lat[i], poly)) continue;
    const ts = new Date(R.t[i] * 60000).toISOString().slice(0, 16).replace("T", " ");
    const sensor = R.platform[i] === "Terra" || R.platform[i] === "Aqua" ? "MODIS" : "VIIRS";
    rows.push([ts, R.lat[i], R.lon[i], R.platform[i], sensor, R.daynight[i], R.confidence[i], R.frp[i], R.w[i], R.wlo[i], R.whi[i], R.fire[i]].join(","));
    n++;
  }
  download(`ember-bridge_${slug(S.sel.label)}_hotspots_last31days.csv`, rows.join("\n") + "\n");
  note.innerHTML = `${num(n)} hotspots exported (backbone satellites, last 31 days). Full archive: <code>python -m ember_bridge.export</code>.`;
}

function downloadPng() {
  const svg = $("calendar").querySelector("svg");
  if (!svg) return;
  const vb = svg.viewBox.baseVal, scale = 2;
  const img = new Image();
  const src = new XMLSerializer().serializeToString(svg);
  img.onload = () => {
    const c = document.createElement("canvas");
    c.width = vb.width * scale; c.height = (vb.height + 34) * scale;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
    ctx.scale(scale, scale);
    ctx.fillStyle = "#1c2229"; ctx.font = "600 13px -apple-system, Segoe UI, Arial";
    ctx.fillText(`${S.sel.label}: ${$("calTitle").textContent}`, 8, 18);
    ctx.drawImage(img, 0, 30, vb.width, vb.height);
    c.toBlob((b) => download(`ember-bridge_${slug(S.sel.label)}_calendar.png`, b, "image/png"));
  };
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(src);
}

/* ------------------------------------------------------------------ start */
async function init() {
  try {
    S.meta = await getJSON("data/meta.json");
  } catch (e) {
    $("calendar").innerHTML = `<p class="empty">No data yet: run <code>python -m ember_bridge.build</code> to create web/data/.</p>`;
    return;
  }
  S.watch = await getJSON("watchlist.json").catch(() => ({}));
  const names = Object.keys(S.meta.regions);
  $("regions").innerHTML = names.map((n) => `<button type="button" data-region="${n}" aria-pressed="false">${esc(S.meta.regions[n].label)}</button>`).join("");
  $("regions").querySelectorAll("button").forEach((b) => { b.onclick = () => setRegion(b.dataset.region); });
  initMap();
  bindControls();
  const m = location.hash.match(/^#([a-z_]+)\/(\d{4})-(\d{2})$/);
  const start = m && names.includes(m[1]) ? m[1] : names.includes("canada") ? "canada" : names[0];
  await setRegion(start, m && m[1] === start ? { y: +m[2], m: +m[3] } : null);
}
init();
