#!/usr/bin/env python3
"""
Sailing forecast pull, config-driven via area.json.

New area or dates: edit area.json -- trip_start/trip_end/timezone,
chart_projection, edgeone_project_name, page_title/eyebrow/heading, the
spots list (name/lat/lon/short/short_mobile), medium_models (which models
feed the medium-range consensus), and optional kernel_step_deg,
map_pad_lon/map_pad_lat, spread_wind_kt/spread_dir_deg/dir_flag_min_wind_kt,
and local_knowledge. No code edit needed for any of that. What still needs a
manual step (documented in README.md): rerun fetch_coastline.py after
changing spots, and set up a fresh EdgeOne project / GitHub repo / Actions
cron if this is meant to run as a second, independent area alongside this
one -- those are about *where*/*when* it deploys, not what data it shows.

Progressively sharper picture as the trip approaches:
  >46 days out : nothing skillful yet -> says when each tier switches on.
  <=46 days out: ECMWF EC46 sub-seasonal ENSEMBLE MEAN (extended range, 36 km).
                 Coarse, smooths fronts -> read as regime/tendency, not a forecast.
  <=15 days out: medium-range consensus across independent models (area.json's
                 medium_models -- ECMWF IFS / NOAA GFS / DWD ICON by default).
                 Each model is read from a 3x3 grid kernel around every spot
                 (~30x25 km by default for this area's spots -- see
                 area.json's kernel_step_deg and KERNEL_STEP_DEG below), not
                 one point -- a boat sailing a spot experiences that whole
                 patch of sea, not a single GPS pin. Wind/temp/rain/direction
                 are the kernel MEAN (smooths single-cell grid noise); gust
                 is the kernel MAX (it's already a worst-case figure, so
                 averaging it away would blunt real local peaks, e.g. a
                 gap-wind gust one cell over). Each model's kernel values are
                 then combined the same way across models -- mean for
                 wind/temp/rain/direction, max for gust -- with a flag when
                 they disagree (real skill, real fronts; thresholds are
                 area.json's spread_wind_kt/spread_dir_deg/
                 dir_flag_min_wind_kt). Per-model detail still goes to
                 runs.csv.
  <=15 days out: sea state (significant wave height, period, direction), same
                 3x3 kernel per spot: wave height is the kernel MAX (same
                 worst-case reasoning as gust), period/direction the kernel
                 mean. Single model (Open-Meteo Marine), so no models-consensus
                 step on top -- the kernel is the only combining here.
  ~<=9-10 days  : ECMWF's own official synoptic chart (MSLP + 850 hPa wind) is
                 pulled and saved as PNG per day -- actual fronts/highs/lows,
                 not a derived number. Horizon shifts slightly run to run.
  ~<=5-6 days   : (tier 5, independent/additive, gated the same as tiers 2-3
                 but only actually populates once its own short horizon
                 reaches the trip) HCMR Poseidon's own regional Greek-seas
                 model (wind + wave, single point, no kernel) -- a second,
                 unofficial, Greece-specific opinion shown alongside tier 4's
                 ECMWF chart, not a replacement for tiers 1-3. See the
                 POSEIDON_* constants below for the real provenance/
                 stability caveats. Set area.json's poseidon_enabled to
                 false for a non-Greek area's copy.

Output is one table per calendar day, places as rows, parameters as columns,
with a RANGE row (never a single area average -- this route spans sheltered
and exposed spots, and an average of e.g. 3kt and 13kt describes nowhere on
it), followed by a trip-wide summary line per tier.

Usage:
    pip install requests
    python aegean_forecast.py
"""
import csv
import datetime as dt
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------- area config --

# Everything specific to *this* area/trip lives in area.json, not here --
# see the module docstring. Loaded once at import time; every area-specific
# "constant" below is derived from this dict, not hand-edited in this file.
AREA_CONFIG_FILE = pathlib.Path(__file__).resolve().parent / "area.json"


def strip_json_comments(text):
    """Strip `//` line comments from area.json text, respecting string
    literals (a `//` inside a quoted value is left alone) -- lets the file
    carry a plain-language description above each parameter/group despite
    the .json extension, no comment-JSON library dependency needed. A
    strict JSON linter/validator will flag the comments as invalid even
    though this loader (and only this loader) reads them fine; trailing
    commas are still a real JSON error, comments or not."""
    out = []
    in_string = False
    escape = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_area_config():
    return json.loads(strip_json_comments(AREA_CONFIG_FILE.read_text(encoding="utf-8")))


_area = load_area_config()

# TRIP_TZ drives every date computation (days_out gating, run_stamp, the
# dashboard's "data captured" line) AND is fed to Open-Meteo as the timezone
# name via TRIP_TZ.key below.
TRIP_START = dt.date.fromisoformat(_area["trip_start"])
TRIP_END = dt.date.fromisoformat(_area["trip_end"])
TRIP_TZ = ZoneInfo(_area["timezone"])

# Must be one of the enum values opencharts_product() rejects with a list of
# alternatives (see chart_for_date's error path) -- area.json's
# chart_projection needs to be one of those, covering the area's location.
CHART_PROJECTION = _area["chart_projection"]

# Give a new area its own project name in area.json, or this overwrites the
# live site of whichever area last used this name instead of standing up a
# second one. RUNS_CSV/DASHBOARD_OUT/SITE_DIR/CHARTS_DIR below are relative
# to this file's own directory, so a fresh copy of the whole project (see
# README.md's new-area guide) keeps a second area's files separate on their
# own regardless.
EDGEONE_PROJECT_NAME = _area["edgeone_project_name"]

# Dashboard header text -- see dashboard_template.html's buildHeader().
PAGE_TITLE = _area["page_title"]
EYEBROW = _area["eyebrow"]
HEADING = _area["heading"]

# {"Name": (lat, lon)} -- names double as dict keys threaded through
# runs.csv, the console tables, and the dashboard payload.
SPOTS = {s["name"]: (s["lat"], s["lon"]) for s in _area["spots"]}
# {"Name": {"short": ..., "shortMobile": ...}} -- fed to the dashboard so
# labels are never auto-abbreviated (collision-prone); each spot names its
# own short forms explicitly in area.json.
SPOT_LABELS = {s["name"]: {"short": s["short"], "shortMobile": s["short_mobile"]} for s in _area["spots"]}

# North-Aegean-specific sailing effects (channel funneling, gap winds, which
# spots are sheltered), fed to the Claude prompt in generate_sailing_summary()
# -- optional; an empty string (area.json's default if omitted) just skips
# that paragraph instead of feeding the model irrelevant Aegean geography.
LOCAL_KNOWLEDGE = _area.get("local_knowledge", "")

EXTENDED_RANGE_DAYS = 46
MEDIUM_RANGE_DAYS = 15

CHART_PRODUCT = "medium-mslp-wind850"
CHARTS_DIR = pathlib.Path(__file__).resolve().parent / "charts"
RUNS_CSV = pathlib.Path(__file__).resolve().parent / "runs.csv"
# Real OSM coastline for the map card -- built once per area by
# fetch_coastline.py, not regenerated on every forecast run (unlike
# everything else write_dashboard embeds).
COASTLINE_JSON = pathlib.Path(__file__).resolve().parent / "coastline.json"
DASHBOARD_TEMPLATE = pathlib.Path(__file__).resolve().parent / "dashboard_template.html"
DASHBOARD_OUT = pathlib.Path(__file__).resolve().parent / "dashboard.html"
# EdgeOne Pages publish: a dedicated site/ dir (index.html only, not the repo)
# gets deployed as a stable named project so every run updates the same URL.
SITE_DIR = pathlib.Path(__file__).resolve().parent / "site"
EDGEONE_TOKEN_FILE = pathlib.Path(__file__).resolve().parent / "edgeone_token.txt"
# LLM sailing summary: Claude API. Tried a local no-auth model
# (deepseek-r1:1.5b via Ollama) first -- confirmed live that it hallucinates
# ungrounded output (wrong units, fabricated place names, wrong dates) on
# this exact task, so that approach was abandoned in favor of a real model.
# Same graceful-skip pattern as the EdgeOne deploy if the key is missing.
ANTHROPIC_API_KEY_FILE = pathlib.Path(__file__).resolve().parent / "anthropic_api_key.txt"
SAILING_SUMMARY_MODEL = "claude-opus-5"
# This API key is identity-linked with access to multiple workspaces, so
# every request must say which one it acts in -- not a secret itself (useless
# without the key), just an org identifier.
ANTHROPIC_WORKSPACE_ID = "wrkspc_01Kcpz2fTqn23suWv5E7VNX9"
# One shared schema across tiers so a single CSV covers wind, temp, and sea
# state -- rows just leave the columns their tier doesn't produce blank.
RUN_FIELDS = ["run_date", "tier", "model", "spot", "date",
              "wind_mean", "wind_max", "gust", "dir", "rain",
              "temp_lo", "temp_hi", "wave", "period", "wave_dir"]

# Mean wind spread (kt) or wind-direction spread (deg) across models above
# which a day is flagged as disagreeing. Overridable per area via area.json
# (a calmer/steadier cruising ground might want tighter thresholds than this
# route's mix of open channel and sheltered coast).
SPREAD_WIND_KT = _area.get("spread_wind_kt", 6)
SPREAD_DIR_DEG = _area.get("spread_dir_deg", 45)
# Below this, direction is poorly defined in near-calm air -- don't flag
# direction disagreement between models when nothing is really blowing.
DIR_FLAG_MIN_WIND_KT = _area.get("dir_flag_min_wind_kt", 5)

PLACE_WIDTH = max(len(n) for n in SPOTS) + 2

DAILY_VARS = [
    "wind_speed_10m_mean", "wind_speed_10m_max", "wind_gusts_10m_max",
    "wind_direction_10m_dominant", "precipitation_sum",
    "temperature_2m_mean", "temperature_2m_max", "temperature_2m_min",
]
DAILY = ",".join(DAILY_VARS)

# Sea state: separate API (marine-api.open-meteo.com), separate host from the
# atmospheric calls above. Units are meters (height) / seconds (period) by
# default -- no unit param needed. Horizon is ~9-10 days, same ballpark as
# the medium-range compare, so it's gated the same way. cell_selection is
# accepted here too (confirmed live) even though wave grids are sea-only.
SEA_DAILY = "wave_height_max,wave_period_max,wave_direction_dominant"

# The independent models averaged (wind/temp/rain/direction) or worst-cased
# (gust) into the medium-range consensus tier -- area.json's medium_models,
# each a {"code": <exact Open-Meteo model id>, "label": <human-readable
# name>}. code feeds the API call and the runs.csv model column; label is
# what console output, the dashboard, and the Claude prompt show. A
# different area might have better-skilled regional models worth using
# instead of these three global ones.
MEDIUM_MODELS = [m["code"] for m in _area["medium_models"]]
MEDIUM_MODEL_LABELS = {m["code"]: m["label"] for m in _area["medium_models"]}
MEDIUM_MODELS_LABEL = " / ".join(m["label"] for m in _area["medium_models"])
# All grid-point calls: bias toward the nearest sea cell. These are small
# islands / narrow coastlines on a 9-36 km grid -- without this, a "coastal"
# request can silently resolve to a mainland or mountaintop cell instead.
CELL_SELECTION = "sea"

# Medium-range and sea state (see kernel_points()): each model/the marine
# model is sampled on a 3x3 grid around every spot instead of one point,
# then combined -- per model before the medium-range model-vs-model
# consensus logic runs; directly (no further consensus step) for sea state,
# since Marine is a single model. Default 0.15 deg step -> edge-to-edge span
# of 0.3 deg -- at these latitudes (~39.5-40.9N) that's ~33 km north-south
# and ~25 km east-west (a degree of longitude shrinks with latitude, degree
# of latitude doesn't), so "3x3" is closer to 30x25 km than a literal
# square. Confirmed live that Open-Meteo resolves all 9 requested points to
# 9 distinct grid cells at this spacing, and that multiple locations batch
# into a single HTTP call (no extra request cost for the wider kernel).
# EC46 alone stays single-point/single-model, unaffected by this -- it's
# already someone else's 51-member ensemble mean, nothing to kernel-combine.
# Overridable per area via area.json's optional kernel_step_deg (e.g. a
# tighter archipelago or sharper local terrain might want a smaller kernel
# than this route's mix of open water and coast).
KERNEL_STEP_DEG = _area.get("kernel_step_deg", 0.15)
KERNEL_OFFSETS = (-KERNEL_STEP_DEG, 0.0, KERNEL_STEP_DEG)


def kernel_points(lat, lon):
    """3x3 grid of (lat, lon) pairs around a spot, row-major (dlat outer,
    dlon inner) so index 4 (of 9) is always the exact-point center cell."""
    return [(round(lat + dlat, 4), round(lon + dlon, 4))
            for dlat in KERNEL_OFFSETS for dlon in KERNEL_OFFSETS]


def kernel_span_km():
    """Approximate (north-south, east-west) edge-to-edge km span of the 3x3
    kernel at these spots' average latitude -- for the human-readable
    descriptions in console output and the Claude prompt only, not used in
    any actual calculation. A degree of latitude is ~111km everywhere; a
    degree of longitude shrinks by cos(latitude), which is why the two
    numbers differ."""
    avg_lat = mean([lat for lat, lon in SPOTS.values()])
    span_deg = KERNEL_STEP_DEG * 2
    return span_deg * 111.0, span_deg * 111.0 * math.cos(math.radians(avg_lat))


# --------------------------------------- HCMR Poseidon (tier 5, additive) --

# Independent extra source alongside tier 4's ECMWF chart: HCMR's own
# short-range (empirically ~5-6 days, not published/guaranteed) regional
# Greek-seas model. Found by reading poseidon.hcmr.gr's own public map page
# JavaScript (it calls nodejs.hcmr.gr/forB/<model>/<lat>/<lon>) -- NOT from
# HCMR's published Swagger API (api.poseidon.hcmr.gr/swagger), which only
# covers OAuth-gated *observational* buoy/station data, no forecasts at all.
# UNOFFICIAL AND UNDOCUMENTED: there is no published contract, version, or
# stability guarantee for this endpoint, unlike every other data source in
# this file. Confirmed live during development that it works with no auth
# at this project's call volume (7 spots x 2 models, once a run) -- but it
# could change shape or disappear without notice, which is exactly why this
# tier is built to fail silently (same try/except-per-spot pattern as every
# other tier) and why it's kept fully independent of tiers 1-4's code paths.
# Greece/E.Med-specific by nature (HCMR doesn't cover other seas) -- set
# area.json's poseidon_enabled to false for a non-Greek area's copy, which
# skips fetching this tier entirely and omits it from the status strip.
POSEIDON_ENABLED = _area.get("poseidon_enabled", True)
POSEIDON_BASE = "https://nodejs.hcmr.gr/forB"
# Field order per HCMR's own model definitions (poseidon-map/code.min.js's
# `models` object) -- the API returns a bare comma-separated string per
# timestep with no field names, so this order is load-bearing, not
# discoverable from the response itself.
POSEIDON_WIND_MODEL = "METEO"
POSEIDON_WIND_FIELDS = ["t2m", "w10", "wangle", "cloud", "snow", "rain", "seag"]
# WW3 (WaveWatch III) and WAM both returned live, near-identical wave data
# in testing for this area's spots -- WW3 is the more widely used modern
# standard, picked as primary; swap to "WAM" here if it ever seems better.
POSEIDON_WAVE_MODEL = "WW3"
POSEIDON_WAVE_FIELDS = ["wht", "dummy", "wangle"]
# w10 is raw m/s (confirmed from HCMR's own unit-conversion table, which
# treats "x" -- identity -- as the m/s option); convert to knots like every
# other wind figure in this project.
POSEIDON_MS_TO_KT = 1.944
# Small nearby candidates to retry only when the wave model's exact point
# comes back fully masked (-999) -- confirmed live that this happens for
# genuinely-in-the-sea points near a coastline (a point ~5km away read fine),
# same class of problem as Open-Meteo's cell_selection="sea". Deliberately
# NOT a general 3x3 kernel: no fixed direction consistently works (a
# masked cell's nearest valid water differs per spot depending on which way
# the coast falls), so this tries a handful of directions/distances in
# order and stops at the first one that has any real data, rather than
# always fetching a fixed grid -- keeps the request count down for the 4
# (of 7) spots that never need it at all.
POSEIDON_WAVE_FALLBACK_OFFSETS_DEG = [
    (-0.05, 0), (0.05, 0), (0, 0.05), (0, -0.05),
    (-0.1, 0), (0.1, 0), (0, 0.1), (0, -0.1),
]


def poseidon_forecast(model, lat, lon):
    """One HTTP call for a single point's short-range time series (3-hourly
    steps, UTC timestamps) from HCMR's regional model. Reuses fetch() with
    no query params since this API is purely path-based, unlike Open-Meteo.

    A short delay before each call: confirmed live during development that
    rapid-fire requests to this specific host can trip a transient 503 (rate
    limit or similar), recovering within ~15-30s -- a real, observed fragility
    unlike Open-Meteo/ECMWF's production infra elsewhere in this file. 7
    spots x 2 models = 14 calls/run; ~0.3s each adds a few seconds total,
    negligible for a once-daily job, cheap insurance against the same thing
    happening on the real run."""
    time.sleep(0.3)
    return fetch(f"{POSEIDON_BASE}/{model}/{lat:.4f}/{lon:.4f}", {})


def parse_poseidon_series(data, fields):
    """[{"date": "...Z", "data": "v1,v2,..."}] -> [(local_datetime, {field:
    value})]. Converts HCMR's UTC timestamps to TRIP_TZ before any date-
    bucketing happens, matching trip_dates() for every other tier (whose
    timestamps arrive already localized, via Open-Meteo's timezone param).

    -999 is HCMR's own "no data" sentinel (confirmed from their own client
    code: `if(-999==x)return" - "`) -- e.g. a spot too close to shore for
    their wave grid. Converted to None here, not left as a real -999 value
    that would otherwise silently wreck every mean/min/max downstream."""
    out = []
    for entry in data:
        local_dt = dt.datetime.fromisoformat(entry["date"].replace("Z", "+00:00")).astimezone(TRIP_TZ)
        raw = entry.get("data")
        if not raw:
            continue
        values = [None if v in ("", None) or float(v) == -999 else float(v) for v in raw.split(",")]
        out.append((local_dt, dict(zip(fields, values))))
    return out


def poseidon_wave_forecast(lat, lon):
    """Wave point forecast with a small fallback search -- see
    POSEIDON_WAVE_FALLBACK_OFFSETS_DEG above for why this exists and why
    it's a short ordered list of candidates, not a kernel. Tries the exact
    point first; only if every timestep there is masked does it try each
    offset in turn, stopping at the first with any real data."""
    data = poseidon_forecast(POSEIDON_WAVE_MODEL, lat, lon)
    if any(v.get("wht") is not None for _, v in parse_poseidon_series(data, POSEIDON_WAVE_FIELDS)):
        return data
    for dlat, dlon in POSEIDON_WAVE_FALLBACK_OFFSETS_DEG:
        candidate = poseidon_forecast(POSEIDON_WAVE_MODEL, lat + dlat, lon + dlon)
        if any(v.get("wht") is not None for _, v in parse_poseidon_series(candidate, POSEIDON_WAVE_FIELDS)):
            print(f"  (wave: exact point masked, using a valid cell {dlat:+.2f},{dlon:+.2f} deg away instead)")
            return candidate
    return data  # every candidate masked too -- return the original (all-None) response


def extract_poseidon_records(name, wind_data, wave_data):
    """Daily aggregate from HCMR's 3-hourly point series -- single point, no
    3x3 kernel (this is a small, independent additive tier, not folded into
    the medium-range kernel/consensus machinery tiers 2-3 use). wind_mean/
    dir/rain/temp are the day's mean/circular-mean/sum/min-max, matching the
    semantics of the same-named fields elsewhere. wind_max is the day's
    highest 3-hourly sample -- HCMR's model has no separate gust field, so
    this is deliberately labeled 'wind_max', not 'gust', to avoid implying a
    value HCMR doesn't actually provide. Wave height is the day's max (same
    worst-case-matters reasoning as gust/wave elsewhere); HCMR's wave model
    gives no period, so there's no wave-period figure for this tier."""
    wind_series = parse_poseidon_series(wind_data, POSEIDON_WIND_FIELDS)
    wave_series = parse_poseidon_series(wave_data, POSEIDON_WAVE_FIELDS)

    by_day = {}
    for local_dt, vals in wind_series:
        date = local_dt.date()
        if TRIP_START <= date <= TRIP_END:
            by_day.setdefault(date, {"wind": [], "wave": []})["wind"].append(vals)
    for local_dt, vals in wave_series:
        date = local_dt.date()
        if TRIP_START <= date <= TRIP_END:
            by_day.setdefault(date, {"wind": [], "wave": []})["wave"].append(vals)

    out = []
    for date in sorted(by_day):
        wind_vals, wave_vals = by_day[date]["wind"], by_day[date]["wave"]
        speeds_kt = [v["w10"] * POSEIDON_MS_TO_KT for v in wind_vals if v.get("w10") is not None]
        dirs = [v["wangle"] % 360 for v in wind_vals if v.get("wangle") is not None]
        rains = [v["rain"] for v in wind_vals if v.get("rain") is not None]
        temps = [v["t2m"] for v in wind_vals if v.get("t2m") is not None]
        waves = [v["wht"] for v in wave_vals if v.get("wht") is not None]
        # Paired with wht's own validity, not checked independently -- a
        # masked/land wave cell (wht is None) still reports a wangle value
        # (observed: a uniform 0 rather than -999), which would otherwise
        # read as a real "due north" direction for a wave that doesn't exist.
        wave_dirs = [v["wangle"] % 360 for v in wave_vals if v.get("wht") is not None and v.get("wangle") is not None]

        out.append({
            "spot": name, "date": date.isoformat(),
            "wind_mean": mean(speeds_kt), "wind_max": max(speeds_kt) if speeds_kt else None,
            "dir": circular_mean_deg(dirs),
            "rain": sum(rains) if rains else None,
            "temp_lo": min(temps) if temps else None, "temp_hi": max(temps) if temps else None,
            "wave": max(waves) if waves else None,
            "wave_dir": circular_mean_deg(wave_dirs),
        })
    return out


def poseidon_run_rows(records, run_date):
    return [{
        "run_date": run_date, "tier": "poseidon", "model": f"HCMR_{POSEIDON_WIND_MODEL}_{POSEIDON_WAVE_MODEL}",
        "spot": r["spot"], "date": r["date"],
        "wind_mean": r["wind_mean"], "wind_max": r["wind_max"], "dir": r["dir"],
        "rain": r["rain"], "temp_lo": r["temp_lo"], "temp_hi": r["temp_hi"],
        "wave": r["wave"], "wave_dir": r["wave_dir"],
    } for r in records]


def print_poseidon_day_tables(records):
    header = f"{'Place':<{PLACE_WIDTH}}{'Wind':>6}{'Max':>6}  {'Dir':<4}{'Rain':>6}{'T.lo':>6}{'T.hi':>6}  {'Wave':>5}  Wave Dir"
    for date, rows in by_date(records):
        print(f"\n=== {date} ===")
        print(header)
        for r in rows:
            print(f"{r['spot']:<{PLACE_WIDTH}}{fmt_num(r['wind_mean'], 6)}{fmt_num(r['wind_max'], 6)}  "
                  f"{deg_to_compass(r['dir']):<4}{fmt_num(r['rain'], 6, 1)}{fmt_num(r['temp_lo'], 6)}"
                  f"{fmt_num(r['temp_hi'], 6)}  {fmt_num(r['wave'], 5, 1)}  {deg_to_compass(r['wave_dir'])}")
        # Hyphenated "lo-hi" range strings run wider than a single value
        # (e.g. "0.1-0.3"), so -- unlike the fixed-width single-value rows
        # above -- an explicit 2-space gap is needed between adjacent range
        # columns or they can run together with no visible separation.
        rng = (f"{'RANGE':<{PLACE_WIDTH}}{col_range(rows, 'wind_mean'):>6}{col_range(rows, 'wind_max'):>6}  "
               f"{'':<4}{col_range(rows, 'rain', 1):>6}{col_range(rows, 'temp_lo'):>6}{col_range(rows, 'temp_hi'):>6}  "
               f"{col_range(rows, 'wave', 1):>5}")
        print(rng)


def print_poseidon_trip_summary(records):
    print(f"\n>>> HCMR Poseidon regional (short-range, unofficial): {TRIP_START} to {TRIP_END} <<<")
    wind_extremes = named_extremes(records, "wind_mean")
    if wind_extremes:
        lo_v, lo_s, hi_v, hi_s = wind_extremes
        print(f"wind: {lo_s} calmest at {fnum(lo_v, 0)}kt, {hi_s} windiest at {fnum(hi_v, 0)}kt")
    wave_extremes = named_extremes(records, "wave")
    if wave_extremes:
        lo_v, lo_s, hi_v, hi_s = wave_extremes
        print(f"wave: {lo_s} calmest at {fnum(lo_v)}m, {hi_s} biggest at {fnum(hi_v)}m")


# ---------------------------------------------------------------- helpers --

def deg_to_compass(d):
    if d is None:
        return "-"
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][int((d + 11.25) // 22.5) % 16]


def angular_diff(a, b):
    if a is None or b is None:
        return None
    return abs((a - b + 180) % 360 - 180)


def circular_mean_deg(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    x = sum(math.cos(math.radians(v)) for v in vals)
    y = sum(math.sin(math.radians(v)) for v in vals)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def fmt_num(v, width=5, decimals=0):
    return f"{v:{width}.{decimals}f}" if v is not None else f"{'-':>{width}}"


def fnum(v, decimals=1):
    return f"{v:.{decimals}f}" if v is not None else "-"


def now_athens_date():
    # Name is stale after a NEW AREA change (behavior already follows TRIP_TZ,
    # wherever that points) -- harmless to leave, or rename at your leisure.
    return dt.datetime.now(TRIP_TZ).date()


def trip_dates(times):
    for i, day in enumerate(times):
        date = dt.date.fromisoformat(day)
        if TRIP_START <= date <= TRIP_END:
            yield i, day


# ----------------------------------------------------------------- network --

def fetch(url, params):
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def ec46(lat, lon):
    return fetch("https://seasonal-api.open-meteo.com/v1/seasonal", {
        "latitude": lat, "longitude": lon,
        "daily": DAILY,
        "models": "ecmwf_ec46_ensemble_mean",
        "forecast_days": EXTENDED_RANGE_DAYS,
        "wind_speed_unit": "kn",
        "timezone": TRIP_TZ.key,
        "cell_selection": CELL_SELECTION,
    })


def medium_range(lat, lon):
    """One HTTP call for all 9 points of the 3x3 kernel around (lat, lon) --
    Open-Meteo accepts comma-separated multi-location lat/lon and returns a
    JSON array in the same order, so the wider kernel costs nothing extra in
    requests. Returns that list of 9 per-cell responses, consumed by
    extract_medium_records()."""
    points = kernel_points(lat, lon)
    return fetch("https://api.open-meteo.com/v1/forecast", {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        "daily": DAILY,
        "models": ",".join(MEDIUM_MODELS),
        "forecast_days": MEDIUM_RANGE_DAYS,
        "wind_speed_unit": "kn",
        "timezone": TRIP_TZ.key,
        "cell_selection": CELL_SELECTION,
    })


def sea_state(lat, lon):
    """One HTTP call for all 9 points of the 3x3 kernel around (lat, lon) --
    same batching as medium_range(). Marine has no per-model consensus step
    (single model, "best_match"), so the kernel here is the only combining
    -- see extract_sea_state_records()."""
    points = kernel_points(lat, lon)
    return fetch("https://marine-api.open-meteo.com/v1/marine", {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        "daily": SEA_DAILY,
        "forecast_days": MEDIUM_RANGE_DAYS,
        "timezone": TRIP_TZ.key,
        "cell_selection": CELL_SELECTION,
    })


def opencharts_product(product, valid_time=None, projection=CHART_PROJECTION):
    params = {"projection": projection}
    if valid_time:
        params["valid_time"] = valid_time
    r = requests.get(f"https://charts.ecmwf.int/opencharts-api/v1/products/{product}/",
                      params=params, timeout=30)
    if r.status_code in (400, 404):
        # These carry a JSON error body (e.g. the list of valid timestamps)
        # that chart_for_date needs -- don't let raise_for_status() eat it.
        return r.json()
    r.raise_for_status()
    return r.json()


def chart_link(data):
    """Pull (href, description) out of an opencharts response, or None if
    the response doesn't have the shape we expect."""
    link = data.get("data", {}).get("link", {}).get("href")
    desc = data.get("data", {}).get("attributes", {}).get("description")
    return (link, desc) if link and desc else None


def chart_for_date(date):
    """(image_url, description) for the ECMWF synoptic chart valid on `date`,
    or (None, reason) once `date` is beyond the current forecast horizon."""
    data = opencharts_product(CHART_PRODUCT, f"{date.isoformat()}T00:00:00Z")
    result = chart_link(data) if "error" not in data else None
    if result:
        return result

    # Exact midnight step may be missing; retry with the latest step ECMWF
    # actually published for this calendar date (still lists later dates too).
    timestamps = re.findall(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", str(data.get("error", "")))
    same_day = [t for t in timestamps if t.startswith(date.isoformat())]
    if same_day:
        data = opencharts_product(CHART_PRODUCT, same_day[-1])
        result = chart_link(data) if "error" not in data else None
        if result:
            return result
    return None, "beyond current forecast horizon"


def save_chart(url, dest):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    dest.write_bytes(r.content)


# --------------------------------------------------------- run log (CSV) --

# runs.csv holds only the latest run, not history -- the first log_rows()
# call in a process overwrites the file (dropping whatever an earlier run
# left behind); later calls in the *same* run (one tier's rows apiece)
# append, so a run with several tiers active still lands together in one
# file. This trades away any before/after diffing across runs -- if that's
# ever wanted again, git history on runs.csv still has every past run's
# committed snapshot, just not accumulated in one file anymore.
_csv_reset_this_run = False


def log_rows(rows):
    if not rows:
        return
    global _csv_reset_this_run
    mode = "a" if _csv_reset_this_run else "w"
    with open(RUNS_CSV, mode, newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUN_FIELDS, restval="")
        if not _csv_reset_this_run:
            w.writeheader()
            _csv_reset_this_run = True
        w.writerows(rows)


# ------------------------------------------ extract: API response -> rows --

def extract_ec46_records(name, data):
    d = data["daily"]
    out = []
    for i, day in trip_dates(d["time"]):
        g = lambda k: d.get(k, [None] * len(d["time"]))[i]
        out.append({
            "spot": name, "date": day,
            "wind_mean": g("wind_speed_10m_mean"), "wind_max": g("wind_speed_10m_max"),
            "gust": g("wind_gusts_10m_max"), "dir": g("wind_direction_10m_dominant"),
            "rain": g("precipitation_sum"),
            "temp_lo": g("temperature_2m_min"), "temp_hi": g("temperature_2m_max"),
        })
    return out


def extract_medium_records(name, cells):
    """cells: the 9 kernel-point responses from medium_range(), in
    kernel_points() order. For each model, combine across the 9 cells first
    -- MEAN for wind/temp/rain/direction (smooths single-cell grid noise),
    MAX for gust (a worst-case figure -- averaging it away would blunt real
    local peaks instead of surfacing them). Everything below this point is
    unchanged from before the kernel: it just receives per-model values that
    are now kernel-combined instead of single-cell, and still averages
    (wind/temp/rain/direction) or worst-cases (gust) those across the three
    models the same way, with the same disagreement flag."""
    ref_time = cells[len(cells) // 2]["daily"]["time"]
    out = []
    for i, day in trip_dates(ref_time):
        per_model = {}
        for m in MEDIUM_MODELS:
            def series(key, _cells=cells, _i=i, _m=m):
                return [c["daily"].get(f"{key}_{_m}", [None] * len(ref_time))[_i] for c in _cells]
            cell_gusts = [v for v in series("wind_gusts_10m_max") if v is not None]
            per_model[m] = {
                "wind_mean": mean(series("wind_speed_10m_mean")),
                "wind_max": mean(series("wind_speed_10m_max")),
                "gust": max(cell_gusts) if cell_gusts else None,
                "dir": circular_mean_deg(series("wind_direction_10m_dominant")),
                "rain": mean(series("precipitation_sum")),
                "temp_lo": mean(series("temperature_2m_min")),
                "temp_hi": mean(series("temperature_2m_max")),
            }
        speeds = [v["wind_mean"] for v in per_model.values() if v["wind_mean"] is not None]
        dirs = [v["dir"] for v in per_model.values() if v["dir"] is not None]
        gusts = [v["gust"] for v in per_model.values() if v["gust"] is not None]

        flags = []
        if len(speeds) >= 2 and (max(speeds) - min(speeds)) > SPREAD_WIND_KT:
            flags.append("wind")
        if speeds and max(speeds) >= DIR_FLAG_MIN_WIND_KT:
            diffs = [angular_diff(a, b) for idx, a in enumerate(dirs) for b in dirs[idx + 1:]]
            if any(x is not None and x > SPREAD_DIR_DEG for x in diffs):
                flags.append("dir")

        out.append({
            "spot": name, "date": day,
            "wind_mean": mean([v["wind_mean"] for v in per_model.values()]),
            "wind_max": mean([v["wind_max"] for v in per_model.values()]),
            "gust": max(gusts) if gusts else None,
            "dir": circular_mean_deg(dirs),
            "rain": mean([v["rain"] for v in per_model.values()]),
            "temp_lo": mean([v["temp_lo"] for v in per_model.values()]),
            "temp_hi": mean([v["temp_hi"] for v in per_model.values()]),
            "flag": "+".join(flags),
            # min-max across models -- shown instead of the bare mean when the
            # "wind" flag fires, since e.g. mean(6, 20) = 13 misrepresents both.
            "wind_span": (min(speeds), max(speeds)) if speeds else None,
            "per_model": per_model,  # raw breakdown, for the CSV log only
        })
    return out


def extract_sea_state_records(name, cells):
    """cells: the 9 kernel-point marine responses from sea_state(), in
    kernel_points() order. Wave height is the kernel MAX -- same reasoning
    as gust in extract_medium_records(): a boat crossing this patch of sea
    can meet the roughest cell in it, not just the exact-point one, so
    averaging it away would understate the real worst case. Period and
    direction are the kernel mean -- less clearly a worst-case quantity, so
    smoothed like wind/temp/rain rather than maxed like wave/gust."""
    ref_time = cells[len(cells) // 2]["daily"]["time"]
    out = []
    for i, day in trip_dates(ref_time):
        def series(key, _cells=cells, _i=i):
            return [c["daily"].get(key, [None] * len(ref_time))[_i] for c in _cells]
        waves = [v for v in series("wave_height_max") if v is not None]
        out.append({
            "spot": name, "date": day,
            "wave": max(waves) if waves else None,
            "period": mean(series("wave_period_max")),
            "dir": circular_mean_deg(series("wave_direction_dominant")),
        })
    return out


def ec46_run_rows(records, run_date):
    return [{
        "run_date": run_date, "tier": "ec46", "model": "ecmwf_ec46_ensemble_mean",
        "spot": r["spot"], "date": r["date"],
        "wind_mean": r["wind_mean"], "wind_max": r["wind_max"],
        "gust": r["gust"], "dir": r["dir"], "rain": r["rain"],
        "temp_lo": r["temp_lo"], "temp_hi": r["temp_hi"],
    } for r in records]


def medium_run_rows(records, run_date):
    rows = []
    for r in records:
        for m, vals in r["per_model"].items():
            rows.append({
                "run_date": run_date, "tier": "medium", "model": m,
                "spot": r["spot"], "date": r["date"],
                "wind_mean": vals["wind_mean"], "wind_max": vals["wind_max"],
                "gust": vals["gust"], "dir": vals["dir"], "rain": vals["rain"],
                "temp_lo": vals["temp_lo"], "temp_hi": vals["temp_hi"],
            })
    return rows


def sea_state_run_rows(records, run_date):
    return [{
        "run_date": run_date, "tier": "sea", "model": "openmeteo_marine_best_match",
        "spot": r["spot"], "date": r["date"],
        "wave": r["wave"], "period": r["period"], "wave_dir": r["dir"],
    } for r in records]


# --------------------------------------------------------- day-table print --

def by_date(records):
    dates = sorted({r["date"] for r in records})
    return [(date, [r for r in records if r["date"] == date]) for date in dates]


def col_range(rows, key, decimals=0):
    """'lo-hi' across rows for one column -- a spread, never averaged away."""
    present = [r[key] for r in rows if r.get(key) is not None]
    return f"{min(present):.{decimals}f}-{max(present):.{decimals}f}" if present else "-"


def named_extremes(rows, key):
    """(low_value, low_spot, high_value, high_spot) across rows, or None."""
    present = [(r[key], r["spot"]) for r in rows if r.get(key) is not None]
    if not present:
        return None
    lo_v, lo_s = min(present, key=lambda p: p[0])
    hi_v, hi_s = max(present, key=lambda p: p[0])
    return lo_v, lo_s, hi_v, hi_s


def print_wind_day_tables(records, show_flag):
    header = f"{'Place':<{PLACE_WIDTH}}{'Wind':>6}{'Max':>6}{'Gust':>6}  {'Dir':<4}{'Rain':>6}{'T.lo':>6}{'T.hi':>6}"
    if show_flag:
        header += "  Flag"
    for date, rows in by_date(records):
        print(f"\n=== {date} ===")
        print(header)
        for r in rows:
            wind_cell = fmt_num(r["wind_mean"], 6)
            # A flagged "wind" disagreement means the models didn't converge --
            # print their span instead of a mean that sits between both and
            # matches neither (e.g. mean(6, 20) = 13 misrepresents both reads).
            if show_flag and "wind" in r["flag"].split("+") and r.get("wind_span"):
                lo, hi = r["wind_span"]
                wind_cell = f"{lo:.0f}-{hi:.0f}".rjust(6)
            line = (f"{r['spot']:<{PLACE_WIDTH}}{wind_cell}{fmt_num(r['wind_max'], 6)}"
                    f"{fmt_num(r['gust'], 6)}  {deg_to_compass(r['dir']):<4}{fmt_num(r['rain'], 6, 1)}"
                    f"{fmt_num(r['temp_lo'], 6)}{fmt_num(r['temp_hi'], 6)}")
            if show_flag:
                line += "  " + ("⚠ " + r["flag"] if r["flag"] else "")
            print(line)

        # A single area-wide average buries exactly the signal that matters on
        # a route spanning sheltered and exposed spots (a 3-13kt day averages
        # to ~8, which describes nowhere on the route) -- show the spread and
        # name which spot sits at each end instead.
        rng = (f"{'RANGE':<{PLACE_WIDTH}}{col_range(rows, 'wind_mean'):>6}{col_range(rows, 'wind_max'):>6}"
               f"{col_range(rows, 'gust'):>6}  {'':<4}{col_range(rows, 'rain', 1):>6}"
               f"{col_range(rows, 'temp_lo'):>6}{col_range(rows, 'temp_hi'):>6}")
        print(rng)
        wind_extremes = named_extremes(rows, "wind_mean")
        if wind_extremes:
            lo_v, lo_s, hi_v, hi_s = wind_extremes
            print(f"  wind: {lo_s} {lo_v:.0f}kt (calmest) -- {hi_s} {hi_v:.0f}kt (windiest)")
        if show_flag:
            n_flagged = sum(1 for r in rows if r["flag"])
            if n_flagged:
                print(f"  {n_flagged}/{len(rows)} spots flagged for model disagreement")


def print_sea_state_day_tables(records):
    header = f"{'Place':<{PLACE_WIDTH}}{'Wave m':>8}{'Period s':>10}  Dir"
    for date, rows in by_date(records):
        print(f"\n=== {date} ===")
        print(header)
        for r in rows:
            print(f"{r['spot']:<{PLACE_WIDTH}}{fmt_num(r['wave'], 8, 1)}{fmt_num(r['period'], 10, 1)}  "
                  f"{deg_to_compass(r['dir'])}")
        print(f"{'RANGE':<{PLACE_WIDTH}}{col_range(rows, 'wave', 1):>8}{col_range(rows, 'period', 1):>10}")
        wave_extremes = named_extremes(rows, "wave")
        if wave_extremes:
            lo_v, lo_s, hi_v, hi_s = wave_extremes
            print(f"  wave: {lo_s} {lo_v:.1f}m -- {hi_s} {hi_v:.1f}m")


def print_wind_trip_summary(label, records):
    print(f"\n>>> {label}: {TRIP_START} to {TRIP_END} <<<")
    wind_extremes = named_extremes(records, "wind_mean")
    if wind_extremes:
        lo_v, lo_s, hi_v, hi_s = wind_extremes
        print(f"wind: {lo_s} calmest at {fnum(lo_v, 0)}kt, {hi_s} windiest at {fnum(hi_v, 0)}kt over the week")
    gust_extremes = named_extremes(records, "gust")
    if gust_extremes:
        _, _, hi_v, hi_s = gust_extremes
        print(f"gust: up to {fnum(hi_v, 0)}kt at {hi_s}")
    print(f"rain {fnum(mean([r['rain'] for r in records]))} mm/day area average, "
          f"temp {fnum(mean([r['temp_lo'] for r in records]), 0)}-{fnum(mean([r['temp_hi'] for r in records]), 0)}C area average")


def print_sea_state_trip_summary(records):
    print(f"\n>>> Sea state: {TRIP_START} to {TRIP_END} <<<")
    wave_extremes = named_extremes(records, "wave")
    if wave_extremes:
        lo_v, lo_s, hi_v, hi_s = wave_extremes
        print(f"wave: {lo_s} calmest at {fnum(lo_v)}m, {hi_s} biggest at {fnum(hi_v)}m over the week")
    print(f"period {fnum(mean([r['period'] for r in records]))} s area average")


# --------------------------------------------------- dashboard.html output --

def all_dates():
    dates, d = [], TRIP_START
    while d <= TRIP_END:
        dates.append(d.isoformat())
        d += dt.timedelta(days=1)
    return dates


def build_param_block(records, dates, field, label, unit, decimals, source_label, no_data_note):
    """One parameter's dashboard payload: per-place series across the trip,
    the trip-wide named extremes, and each place's own week-to-week range.

    Falls back to an unavailable/placeholder block if every value is None --
    e.g. the sea-state tier can be "live" (the API call succeeded) while the
    marine model's real horizon still falls short of the trip dates. Charting
    an all-null series divides by a zero-width range and silently draws
    nothing, so treat "live API, no data yet" the same as "tier not open"."""
    places = list(SPOTS.keys())
    series = {}
    for spot in places:
        vals = []
        for d in dates:
            match = next((r for r in records if r["spot"] == spot and r["date"] == d), None)
            v = match.get(field) if match else None
            vals.append(round(v, 1) if v is not None else None)
        series[spot] = vals

    flat = [(v, spot) for spot, vals in series.items() for v in vals if v is not None]
    if not flat:
        return placeholder_block(label, unit, no_data_note)

    lo = min(flat, key=lambda p: p[0])
    hi = max(flat, key=lambda p: p[0])
    trip_extremes = {"lowValue": lo[0], "lowPlace": lo[1], "highValue": hi[0], "highPlace": hi[1]}

    place_range = {}
    for spot in places:
        vals = [v for v in series[spot] if v is not None]
        place_range[spot] = [min(vals), max(vals)] if vals else None

    return {
        "available": True, "label": label, "unit": unit, "decimals": decimals,
        "series": series, "tripExtremes": trip_extremes, "placeRange": place_range,
        "sourceLabel": source_label,
    }


def build_direction_block(records, dates, field, label, source_label, no_data_note):
    """Compass directions don't fit a numeric line chart -- table only, with
    each place's circular-mean predominant direction instead of a min-max
    range (min/max of a compass bearing isn't a meaningful summary)."""
    places = list(SPOTS.keys())
    series, raw = {}, {}
    for spot in places:
        compass_vals, deg_vals = [], []
        for d in dates:
            match = next((r for r in records if r["spot"] == spot and r["date"] == d), None)
            deg = match.get(field) if match else None
            compass_vals.append(deg_to_compass(deg) if deg is not None else None)
            deg_vals.append(deg)
        series[spot] = compass_vals
        raw[spot] = deg_vals

    if not any(v is not None for vals in raw.values() for v in vals):
        return placeholder_block(label, "", no_data_note)

    predominant = {}
    for spot in places:
        cm = circular_mean_deg(raw[spot])
        predominant[spot] = deg_to_compass(cm) if cm is not None else None

    return {
        "available": True, "type": "direction", "label": label, "unit": "",
        "series": series, "predominant": predominant, "sourceLabel": source_label,
    }


def placeholder_block(label, unit, opens_note):
    return {"available": False, "label": label, "unit": unit, "opensNote": opens_note}


# ------------------------------------------------------- LLM sailing summary --

def summary_data_table(wind_records, sea_records):
    """Plain-text tables the model reads as its only source of truth: the
    full wind/temp/rain table (with the model-disagreement flag column),
    a per-model breakdown on any day models disagreed, and sea state when
    available. Opus 5 is capable enough to read this directly -- no need
    to pre-compress it the way the abandoned small local model needed."""
    lines = ["place,date,wind_mean_kt,gust_kt,dir,rain_mm,temp_lo_c,temp_hi_c,model_flag"]
    for r in wind_records:
        lines.append(f"{r['spot']},{r['date']},{r.get('wind_mean')},{r.get('gust')},"
                      f"{r.get('dir')},{r.get('rain')},{r.get('temp_lo')},{r.get('temp_hi')},"
                      f"{r.get('flag') or '-'}")

    if any(r.get("flag") for r in wind_records):
        lines.append("")
        lines.append("Per-model wind_mean_kt on days models disagreed (model_flag above wasn't '-'):")
        for r in wind_records:
            if r.get("flag"):
                parts = ", ".join(f"{MEDIUM_MODEL_LABELS[m]}={v['wind_mean']}" for m, v in r["per_model"].items())
                lines.append(f"{r['spot']} {r['date']}: {parts}")

    if sea_records:
        lines.append("")
        lines.append("place,date,wave_height_m,wave_period_s,wave_dir")
        for r in sea_records:
            lines.append(f"{r['spot']},{r['date']},{r.get('wave')},{r.get('period')},{r.get('dir')}")
    return "\n".join(lines)


def generate_sailing_summary(wind_records, wind_source_label, sea_records, previous_run_csv):
    """Ask Claude (SAILING_SUMMARY_MODEL) to turn this run's data into a
    sailing-focused narrative. Returns None (never raises) if the API key
    isn't set up or the call fails, so the rest of the pipeline is
    unaffected either way -- same graceful-skip pattern as deploy_dashboard.

    previous_run_csv (raw runs.csv text from before this run overwrote it,
    or None on the very first run) lets the model call out what changed
    since last time instead of only describing a single snapshot."""
    if not wind_records:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and ANTHROPIC_API_KEY_FILE.exists():
        api_key = ANTHROPIC_API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not api_key:
        print(f"Sailing summary skipped: no key (set ANTHROPIC_API_KEY or write {ANTHROPIC_API_KEY_FILE.name}).")
        return None

    places = ", ".join(SPOTS.keys())
    previous_section = ""
    if previous_run_csv:
        previous_section = f"""

PREVIOUS RUN'S DATA, same trip and spots, for comparison (columns: {RUN_FIELDS}):
{previous_run_csv}"""

    changed_instruction = (
        "A closing section: comparing against the previous run's data, what changed since "
        "last time -- regime holding steady vs. specific days/spots getting windier, calmer, "
        "wetter, or backing/veering direction -- and what a sailor should do differently as a "
        "result. If nothing meaningfully changed, say so plainly instead of inventing a "
        "difference; a run confirming the prior one is itself useful information (rising "
        "confidence), not a non-event."
        if previous_run_csv else
        "Skip any before/after comparison -- this is the first run, nothing to compare against."
    )

    # LOCAL_KNOWLEDGE comes from area.json (optional; empty string if
    # omitted). Sent as its own paragraph only when non-empty, so a new area
    # that hasn't written one yet doesn't feed the model an empty heading.
    local_knowledge_section = f"\n{LOCAL_KNOWLEDGE}\n" if LOCAL_KNOWLEDGE else ""

    # Tells the model what it's actually looking at, so it reasons about
    # model_flag/gust correctly instead of assuming a plain single-point
    # forecast. Branches on wind_source_label since EC46 and the medium-range
    # consensus are fetched and combined completely differently.
    if wind_source_label.startswith("Medium-range"):
        ns_km, ew_km = kernel_span_km()
        data_methodology = f"""How the wind/temp/rain/gust figures below were produced: each of the \
models ({MEDIUM_MODELS_LABEL}) is sampled over a 3x3 grid of points (~{ns_km:.0f}x{ew_km:.0f}km) around \
every spot, not one exact GPS point -- reflecting the patch of sea a boat sailing that spot actually \
moves through. Wind speed, temperature, rain, and direction are each model's mean across those 9 \
points; gust is each model's max across those 9 points instead of a mean, since gust is inherently a \
worst-case figure and averaging it away would hide a real local peak (e.g. a gap-wind gust one grid \
cell over). The models' results are then combined the same way on top of that: mean for \
wind/temp/rain/direction, max for gust -- so the gust figure shown is deliberately the strongest plausible \
gust anywhere in that patch across all models, a safety margin rather than a literal single-point \
prediction. model_flag marks a day where the models disagree by more than {SPREAD_WIND_KT}kt (wind) or \
{SPREAD_DIR_DEG} degrees (direction, only counted when wind is at least {DIR_FLAG_MIN_WIND_KT}kt) -- treat \
those days' numbers as less certain; the per-model breakdown further down gives the actual spread."""
    elif wind_source_label.startswith("EC46"):
        data_methodology = """How the wind/temp/rain figures below were produced: this is ECMWF's own \
EC46 sub-seasonal 51-member ensemble mean at ~36km resolution -- a single number per spot/day, already \
averaged by ECMWF, with no per-model breakdown or model_flag at this range (that only exists once the \
medium-range consensus tier takes over closer to the trip). Read it as the week's regime/tendency this \
far out, not a day-by-day forecast -- the ensemble mean smooths out individual fronts."""
    else:
        data_methodology = ""
    if sea_records:
        data_methodology += ("\n\nSea state (wave height/period/direction) comes from a single model "
                              "(Open-Meteo Marine), sampled over the same 3x3 kernel as wind: wave height "
                              "is the kernel max (same worst-case reasoning as gust -- the roughest cell "
                              "in the patch, not just the exact point), period/direction are the kernel "
                              "mean. There's no further models-consensus step on top since Marine is a "
                              "single model, unlike the wind figures' three-model combine.")

    topics = [
        "The overall wind regime for the week -- direction, typical strength, how steady vs. variable.",
        "Which specific days and spots look calmest vs. roughest, and why (cite the actual numbers).",
        "Any notable rain, temperature swings, or model disagreement (model_flag / per-model spread) -- "
        "explain what disagreement means for how much to trust that day's number.",
    ]
    # Only ask for a local-effects paragraph when area.json actually supplied
    # one -- otherwise there's no context to draw it from.
    if LOCAL_KNOWLEDGE:
        topics.append("Local effects worth watching (from the context above) where the data actually supports it.")
    topics.append(changed_instruction)
    topics.append("A closing paragraph of concrete, practical routing/timing advice for the week.")
    topics_block = "\n".join(f"{i}. {t}" for i, t in enumerate(topics, start=1))

    prompt = f"""You are a sailing weather analyst briefing a skipper before a trip. The trip \
runs {TRIP_START} to {TRIP_END} across these spots: {places}. Data source for \
the wind/temp/rain figures below: {wind_source_label}.

{data_methodology}
{local_knowledge_section}
DATA:
{summary_data_table(wind_records, sea_records)}
{previous_section}

Write a thorough (500-700 word) sailing briefing in flowing prose, organized as a few clearly \
distinct paragraphs (blank line between each). Plain prose only -- no markdown of any kind: no \
headers, no bullet lists, no **bold** or *italic* emphasis, no numbered list. This will be \
displayed as plain text, so any markdown characters would show up literally in the output. \
Paragraph topics:
{topics_block}

Ground every claim in the actual data above -- do not invent locations, units, or dates not \
present in it."""

    try:
        import anthropic
        client = anthropic.Anthropic(
            api_key=api_key,
            default_headers={"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID},
        )
        response = client.messages.create(
            model=SAILING_SUMMARY_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
    except ImportError:
        print("Sailing summary skipped: pip install anthropic", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"Sailing summary skipped: API error {e.status_code} ({e.message})", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        print(f"Sailing summary skipped: connection error ({e})", file=sys.stderr)
        return None

    return text.strip() or None


def build_dashboard_payload(run_stamp, wind_records, wind_source_label, wind_opens_note,
                             sea_records, sea_opens_note, poseidon_records, poseidon_opens_note,
                             tiers, sailing_summary):
    dates = all_dates()
    params = {}

    wind_specs = [
        ("wind_mean", "Wind (mean)", "kt", 0),
        ("gust", "Gust (max)", "kt", 0),
        ("rain", "Rain", "mm", 1),
        ("temp_lo", "Temp (low)", "°C", 0),
        ("temp_hi", "Temp (high)", "°C", 0),
    ]
    for key, label, unit, decimals in wind_specs:
        if wind_records:
            params[key] = build_param_block(wind_records, dates, key, label, unit, decimals, wind_source_label,
                                             "No data for these trip dates yet -- rerun closer to the trip")
        else:
            params[key] = placeholder_block(label, unit, wind_opens_note)

    if wind_records:
        params["wind_dir"] = build_direction_block(wind_records, dates, "dir", "Wind direction", wind_source_label,
                                                    "No data for these trip dates yet -- rerun closer to the trip")
    else:
        params["wind_dir"] = placeholder_block("Wind direction", "", wind_opens_note)

    sea_specs = [("wave", "Wave height", "m", 1), ("period", "Wave period", "s", 1)]
    for key, label, unit, decimals in sea_specs:
        if sea_records:
            params[key] = build_param_block(sea_records, dates, key, label, unit, decimals, "Open-Meteo Marine",
                                             "Marine model doesn't reach these trip dates yet -- rerun closer to the trip")
        else:
            params[key] = placeholder_block(label, unit, sea_opens_note)

    if sea_records:
        params["wave_dir"] = build_direction_block(sea_records, dates, "dir", "Wave direction", "Open-Meteo Marine",
                                                    "Marine model doesn't reach these trip dates yet -- rerun closer to the trip")
    else:
        params["wave_dir"] = placeholder_block("Wave direction", "", sea_opens_note)

    # Tier 5 -- independent, additive; entirely absent from the payload (not
    # even a pending placeholder) when area.json's poseidon_enabled is false,
    # so a non-Greek area's dashboard shows no trace of a tier that could
    # never work for it.
    if POSEIDON_ENABLED:
        poseidon_specs = [
            ("poseidon_wind", "wind_mean", "Wind (mean)", "kt", 0),
            ("poseidon_wind_max", "wind_max", "Wind (max)", "kt", 0),
            ("poseidon_rain", "rain", "Rain", "mm", 1),
            ("poseidon_temp_lo", "temp_lo", "Temp (low)", "°C", 0),
            ("poseidon_temp_hi", "temp_hi", "Temp (high)", "°C", 0),
            ("poseidon_wave", "wave", "Wave height", "m", 1),
        ]
        for param_key, field, label, unit, decimals in poseidon_specs:
            if poseidon_records:
                params[param_key] = build_param_block(poseidon_records, dates, field, label, unit, decimals,
                                                       "HCMR Poseidon (unofficial)",
                                                       "No data for these trip dates yet -- rerun closer to the trip")
            else:
                params[param_key] = placeholder_block(label, unit, poseidon_opens_note)

        if poseidon_records:
            params["poseidon_wind_dir"] = build_direction_block(
                poseidon_records, dates, "dir", "Wind direction", "HCMR Poseidon (unofficial)",
                "No data for these trip dates yet -- rerun closer to the trip")
            params["poseidon_wave_dir"] = build_direction_block(
                poseidon_records, dates, "wave_dir", "Wave direction", "HCMR Poseidon (unofficial)",
                "No data for these trip dates yet -- rerun closer to the trip")
        else:
            params["poseidon_wind_dir"] = placeholder_block("Wind direction", "", poseidon_opens_note)
            params["poseidon_wave_dir"] = placeholder_block("Wave direction", "", poseidon_opens_note)

    return {
        "dates": dates, "runStamp": run_stamp, "places": list(SPOTS.keys()),
        "tiers": tiers, "params": params, "sailingSummary": sailing_summary,
        "pageTitle": PAGE_TITLE, "eyebrow": EYEBROW, "heading": HEADING,
        "labels": SPOT_LABELS,
        # For the footer's model-disagreement paragraph -- so it names the
        # actual configured models/thresholds instead of a hardcoded default
        # that could be wrong for a different area.json.
        "mediumModelsLabel": MEDIUM_MODELS_LABEL,
        "spreadWindKt": SPREAD_WIND_KT, "spreadDirDeg": SPREAD_DIR_DEG,
        "dirFlagMinWindKt": DIR_FLAG_MIN_WIND_KT,
    }


def write_dashboard(payload):
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    coastline = COASTLINE_JSON.read_text(encoding="utf-8") if COASTLINE_JSON.exists() else "null"
    html = template.replace("__DASHBOARD_DATA_JSON__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__COASTLINE_DATA_JSON__", coastline)
    DASHBOARD_OUT.write_text(html, encoding="utf-8")
    # Separate publish dir with only index.html -- deploying SITE_DIR (not the
    # repo root) keeps runs.csv / the script / the token file off the public site.
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")


def deploy_dashboard():
    """Publish site/ to EdgeOne Pages under a stable project name, so every
    run updates the same URL instead of minting a new one. No-ops cleanly
    (prints why) if the token isn't set up yet or the CLI isn't installed --
    this is meant to be safe to call from every run regardless."""
    token = os.environ.get("EDGEONE_API_TOKEN")
    if not token and EDGEONE_TOKEN_FILE.exists():
        token = EDGEONE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        print(f"\nEdgeOne publish skipped: no token (set EDGEONE_API_TOKEN or write {EDGEONE_TOKEN_FILE.name}).")
        return

    script_dir = pathlib.Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["npx", "edgeone", "makers", "deploy", str(SITE_DIR),
             "-n", EDGEONE_PROJECT_NAME, "-t", token, "--json"],
            cwd=script_dir, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print("\nEdgeOne publish skipped: npx/node not found.")
        return
    except subprocess.TimeoutExpired:
        print("\nEdgeOne publish failed: deploy command timed out.", file=sys.stderr)
        return

    if result.returncode != 0:
        print(f"\nEdgeOne publish failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stdout.strip() or result.stderr.strip(), file=sys.stderr)
        return

    # Confirmed live: --json emits one line with a "url" field. That URL
    # carries ?eo_token=...&eo_time=... (a signed just-deployed link) --
    # confirmed live that the bare origin alone (no query string) serves the
    # same content and is what stays stable across redeploys, so strip the
    # query string before printing it as *the* link to bookmark/share.
    line = next((l for l in result.stdout.splitlines() if l.strip().startswith("{")), None)
    url = None
    if line:
        try:
            data = json.loads(line)
            url = data.get("url") or data.get("deployUrl") or data.get("domain") or data.get("previewUrl")
        except json.JSONDecodeError:
            pass
    if url:
        print(f"\nPublished to EdgeOne Pages: {url.split('?')[0]}")
    else:
        print(f"\nPublished to EdgeOne Pages (couldn't parse URL from output, full result below):\n{result.stdout.strip()}")


# ------------------------------------------------------------------- main --

def main():
    today = now_athens_date()
    # Minute-resolution, distinct from `today` -- shown as the dashboard's
    # "data captured" timestamp.
    run_stamp = dt.datetime.now(TRIP_TZ).isoformat(timespec="minutes")
    days_out = (TRIP_START - today).days
    print(f"Today {today}, trip starts in {days_out} days.")

    # Snapshot the previous run's data before anything below overwrites it --
    # runs.csv itself only ever holds the latest run (see log_rows), but the
    # file on disk right now is still whatever the *last* run left behind
    # (checked out fresh from git in CI), so this is the last chance to read
    # it. Feeds the LLM summary a same-trip before/after comparison without
    # needing runs.csv to accumulate history again.
    previous_run_csv = RUNS_CSV.read_text(encoding="utf-8") if RUNS_CSV.exists() else None

    ec46_records = medium_records = sea_records = None
    ec46_available = TRIP_START - dt.timedelta(days=EXTENDED_RANGE_DAYS)
    medium_available = TRIP_START - dt.timedelta(days=MEDIUM_RANGE_DAYS)

    print()
    if days_out > EXTENDED_RANGE_DAYS:
        print(f"ECMWF EC46 extended range (46-day) will start covering the trip from ~{ec46_available}.")
    else:
        print("##### ECMWF EC46 sub-seasonal, 51-member ENSEMBLE MEAN (36 km) #####")
        print("Ensemble mean smooths out fronts. Read wind as 'regime tendency', not a forecast.")
        records = []
        for name, (lat, lon) in SPOTS.items():
            try:
                records += extract_ec46_records(name, ec46(lat, lon))
            except Exception as e:
                print(f"{name}: failed ({e})", file=sys.stderr)
        if records:
            ec46_records = records
            log_rows(ec46_run_rows(records, run_stamp))
            print_wind_day_tables(records, show_flag=False)
            print_wind_trip_summary("EC46 extended range", records)

    print()
    if days_out > MEDIUM_RANGE_DAYS:
        print(f"Medium-range consensus ({MEDIUM_MODELS_LABEL}) will start covering the trip from ~{medium_available}.")
    else:
        print(f"##### Medium-range consensus: mean of {MEDIUM_MODELS_LABEL} #####")
        ns_km, ew_km = kernel_span_km()
        print(f"Each model sampled over a ~{ns_km:.0f}x{ew_km:.0f}km kernel (3x3 grid, {KERNEL_STEP_DEG} deg step) around "
              "every spot, not one point. Wind/temp/rain/direction = kernel mean; gust = kernel max, "
              "then worst case across the models. Flag = models disagree on wind "
              f"(>{SPREAD_WIND_KT}kt spread) or direction (>{SPREAD_DIR_DEG}deg spread, wind >= {DIR_FLAG_MIN_WIND_KT}kt). "
              "Per-model detail is in runs.csv.")
        records = []
        for name, (lat, lon) in SPOTS.items():
            try:
                records += extract_medium_records(name, medium_range(lat, lon))
            except Exception as e:
                print(f"{name}: failed ({e})", file=sys.stderr)
        if records:
            medium_records = records
            log_rows(medium_run_rows(records, run_stamp))
            print_wind_day_tables(records, show_flag=True)
            print_wind_trip_summary("Medium-range consensus", records)

    print()
    if days_out > MEDIUM_RANGE_DAYS:
        print(f"Sea state (wave height/period/direction) will start covering the trip from ~{medium_available}.")
    else:
        print("##### Sea state: significant wave height, period, direction #####")
        ns_km, ew_km = kernel_span_km()
        print(f"Same ~{ns_km:.0f}x{ew_km:.0f}km kernel as wind (3x3 grid, {KERNEL_STEP_DEG} deg step): wave height = kernel max "
              "(worst case, same reasoning as gust); period/direction = kernel mean. Single model "
              "(Open-Meteo Marine), no models-consensus step on top.")
        print("'-' beyond ~9-10 days out even though this tier is open -- rerun closer in for those days.")
        records = []
        for name, (lat, lon) in SPOTS.items():
            try:
                records += extract_sea_state_records(name, sea_state(lat, lon))
            except Exception as e:
                print(f"{name}: failed ({e})", file=sys.stderr)
        if records:
            sea_records = records
            log_rows(sea_state_run_rows(records, run_stamp))
            print_sea_state_day_tables(records)
            print_sea_state_trip_summary(records)

    chart_tier_live = False
    print()
    if days_out > MEDIUM_RANGE_DAYS:
        print("ECMWF official synoptic charts (MSLP + 850 hPa wind) open up around 9-10 days out.")
    else:
        print(f"##### ECMWF official synoptic chart: {CHART_PRODUCT} (fronts / highs / lows / gradient wind) #####")
        print(f"Source: charts.ecmwf.int, area '{CHART_PROJECTION.replace('opencharts_', '')}'. "
              "Real ECMWF rendering, not a derived number -- read pressure contours for fronts/lows.")
        CHARTS_DIR.mkdir(exist_ok=True)
        date, any_chart = TRIP_START, False
        while date <= TRIP_END:
            try:
                url, desc = chart_for_date(date)
            except Exception as e:
                print(f"{date}: failed ({e})", file=sys.stderr)
                date += dt.timedelta(days=1)
                continue
            if url:
                any_chart = True
                dest = CHARTS_DIR / f"{date.isoformat()}_{CHART_PRODUCT}.png"
                already_today = (
                    dest.exists()
                    and dt.datetime.fromtimestamp(dest.stat().st_mtime, tz=TRIP_TZ).date() == today
                )
                if already_today:
                    print(f"{date}: {desc}\n  -> already have today's chart at {dest}")
                    date += dt.timedelta(days=1)
                    continue
                try:
                    save_chart(url, dest)
                    print(f"{date}: {desc}\n  -> saved {dest}")
                except Exception as e:
                    print(f"{date}: failed to save chart ({e})", file=sys.stderr)
            else:
                print(f"{date}: {desc}")
            date += dt.timedelta(days=1)
        if not any_chart:
            print("(None of the trip dates are inside the chart horizon yet -- rerun closer to the trip.)")
        chart_tier_live = any_chart

    # Tier 5, right next to tier 4 in the console: independent, purely
    # additive HCMR Poseidon regional forecast -- see the POSEIDON_* block
    # above for what this is and its unofficial/undocumented caveats. Gated
    # the same as tiers 2-3 (attempt from 15 days out), but its own real
    # horizon is much shorter (~5-6 days, not published) -- state below
    # reflects whether trip dates actually came back, not an assumed cutoff.
    poseidon_records = None
    print()
    if not POSEIDON_ENABLED:
        pass
    elif days_out > MEDIUM_RANGE_DAYS:
        print(f"HCMR Poseidon regional forecast (unofficial) will start covering the trip from ~{medium_available} "
              "(its actual short horizon may be much narrower -- checked at that point).")
    else:
        print("##### HCMR Poseidon regional forecast: wind + wave (unofficial, Greek-seas-specific) #####")
        print("Independent second opinion alongside the ECMWF chart above -- own model, own point (no kernel), "
              "no gust or wave-period field available from this source. Not a documented/stable API; "
              "see aegean_forecast.py's POSEIDON_* comments.")
        records = []
        for name, (lat, lon) in SPOTS.items():
            try:
                wind_data = poseidon_forecast(POSEIDON_WIND_MODEL, lat, lon)
                wave_data = poseidon_wave_forecast(lat, lon)
                records += extract_poseidon_records(name, wind_data, wave_data)
            except Exception as e:
                print(f"{name}: failed ({e})", file=sys.stderr)
        if records:
            poseidon_records = records
            log_rows(poseidon_run_rows(records, run_stamp))
            print_poseidon_day_tables(records)
            print_poseidon_trip_summary(records)
        else:
            print("(Trip dates are beyond HCMR's actual short horizon yet -- rerun closer to the trip.)")

    # Medium-range consensus supersedes EC46 once it's live -- it's
    # independent models agreeing (or flagged when they don't), not a single
    # coarse ensemble mean, so prefer it for the dashboard's wind/temp/rain cards.
    if medium_records:
        wind_records, wind_source_label = medium_records, f"Medium-range consensus ({MEDIUM_MODELS_LABEL})"
    elif ec46_records:
        wind_records, wind_source_label = ec46_records, "EC46 ensemble mean"
    else:
        wind_records, wind_source_label = None, None

    tiers = [
        {"name": "EC46 extended range", "state": "live" if ec46_records else "pending",
         "note": "51-member ensemble mean" if ec46_records else f"opens ~{ec46_available}"},
        {"name": "Medium-range consensus", "state": "live" if medium_records else "pending",
         "note": MEDIUM_MODELS_LABEL if medium_records else f"opens ~{medium_available}"},
        {"name": "Sea state", "state": "live" if sea_records else "pending",
         "note": "Open-Meteo Marine" if sea_records else f"opens ~{medium_available}"},
        {"name": "ECMWF synoptic charts", "state": "live" if chart_tier_live else "pending",
         "note": "MSLP + 850 hPa wind, per day" if chart_tier_live else "opens ~9-10 days out"},
    ]
    if POSEIDON_ENABLED:
        tiers.append({"name": "Poseidon regional (unofficial)", "state": "live" if poseidon_records else "pending",
                       "note": "HCMR wind + wave" if poseidon_records else f"opens ~{medium_available} (short horizon)"})

    print()
    sailing_summary = generate_sailing_summary(wind_records, wind_source_label, sea_records, previous_run_csv)
    if sailing_summary:
        print(f"##### Sailing summary ({SAILING_SUMMARY_MODEL}, read the numbers above too) #####")
        print(sailing_summary)

    payload = build_dashboard_payload(
        run_stamp=run_stamp,
        wind_records=wind_records, wind_source_label=wind_source_label,
        wind_opens_note=f"Opens ~{ec46_available} (EC46 extended range)",
        sea_records=sea_records,
        sea_opens_note=f"Sea state opens ~{medium_available} — rerun the forecast script closer to the trip",
        poseidon_records=poseidon_records,
        poseidon_opens_note=f"HCMR Poseidon (unofficial) opens ~{medium_available} — actual horizon is much "
                             "shorter, rerun closer to the trip",
        tiers=tiers, sailing_summary=sailing_summary,
    )
    write_dashboard(payload)
    print(f"\nDashboard written to {DASHBOARD_OUT}")
    deploy_dashboard()


if __name__ == "__main__":
    main()
