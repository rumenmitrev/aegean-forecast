#!/usr/bin/env python3
"""
North Aegean sailing forecast pull (3-10 Oct 2026).

New area or dates: this line is cosmetic, but everything functional lives in
the constants below (TRIP_START/END/TZ, SPOTS, CHART_PROJECTION,
EDGEONE_PROJECT_NAME -- each marked "NEW AREA" inline) and in
dashboard_template.html's static header text (title/eyebrow/h1/route line)
plus its SHORT/SHORT_MOBILE label maps.

Progressively sharper picture as the trip approaches:
  >46 days out : nothing skillful yet -> says when each tier switches on.
  <=46 days out: ECMWF EC46 sub-seasonal ENSEMBLE MEAN (extended range, 36 km).
                 Coarse, smooths fronts -> read as regime/tendency, not a forecast.
  <=15 days out: medium-range consensus across three independent models --
                 ECMWF IFS, NOAA GFS, DWD ICON -- averaged into one number per
                 place/day, with a flag when the three disagree (real skill,
                 real fronts). Per-model detail still goes to runs.csv.
  <=15 days out: sea state (significant wave height, period, direction).
  ~<=9-10 days  : ECMWF's own official synoptic chart (MSLP + 850 hPa wind) is
                 pulled and saved as PNG per day -- actual fronts/highs/lows,
                 not a derived number. Horizon shifts slightly run to run.

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
from zoneinfo import ZoneInfo

import requests

# === NEW AREA/DATES: trip window and local timezone =========================
# TRIP_TZ drives every date computation (days_out gating, run_stamp, the
# dashboard's "data captured" line) AND is fed to Open-Meteo as the timezone
# name via TRIP_TZ.key below -- change it here only, nowhere else.
TRIP_START = dt.date(2026, 10, 3)
TRIP_END = dt.date(2026, 10, 10)
TRIP_TZ = ZoneInfo("Europe/Athens")
# ==============================================================================

EXTENDED_RANGE_DAYS = 46
MEDIUM_RANGE_DAYS = 15

CHART_PRODUCT = "medium-mslp-wind850"
# === NEW AREA: ECMWF opencharts region code ==================================
# Must be one of the enum values opencharts_product() rejects with a list of
# alternatives (see chart_for_date's error path) -- pick the one covering the
# new area, e.g. opencharts_north_america, opencharts_south_east_asia, etc.
CHART_PROJECTION = "opencharts_south_east_europe"
# ==============================================================================
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
# === NEW AREA: give it its own project name, or this overwrites the live
# aegean-forecast site instead of standing up a second one. Also point
# RUNS_CSV/DASHBOARD_OUT/SITE_DIR/CHARTS_DIR above at a fresh directory, so
# a second area's files don't land in the first area's project folder.
# ==============================================================================
EDGEONE_PROJECT_NAME = "aegean-forecast"
# ==============================================================================
EDGEONE_TOKEN_FILE = pathlib.Path(__file__).resolve().parent / "edgeone_token.txt"
# One shared schema across tiers so a single CSV covers wind, temp, and sea
# state -- rows just leave the columns their tier doesn't produce blank.
RUN_FIELDS = ["run_date", "tier", "model", "spot", "date",
              "wind_mean", "wind_max", "gust", "dir", "rain",
              "temp_lo", "temp_hi", "wave", "period", "wave_dir"]

# Mean wind spread (kt) or wind-direction spread (deg) across models above
# which a day is flagged as disagreeing.
SPREAD_WIND_KT = 6
SPREAD_DIR_DEG = 45
# Below this, direction is poorly defined in near-calm air -- don't flag
# direction disagreement between models when nothing is really blowing.
DIR_FLAG_MIN_WIND_KT = 5

# === NEW AREA: the 6 places (name, (lat, lon)) ================================
# Names double as dict keys threaded through runs.csv, the console tables, and
# the dashboard payload -- rename freely, but also update dashboard_template.
# html's SHORT / SHORT_MOBILE maps (they're keyed by these exact strings) and
# the static header text (title/eyebrow/h1/route line). Count can change too;
# nothing downstream assumes exactly 6 except that CSS only defines --place-1
# through --place-6 (dashboard_template.html) -- add more slots there if the
# new area has more than 6 spots.
SPOTS = {
    "Keramoti / Thassos": (40.85, 24.70),
    "Samothrace (S coast)": (40.42, 25.55),
    "Lemnos (Myrina)": (39.87, 25.05),
    "Thracian Sea (open water)": (40.45, 25.10),
    "Gulf of Saros (E of Samothrace)": (40.55, 26.40),
    "Agios Efstratios (S of Lemnos)": (39.50, 24.98),
    "Mount Athos (N approach)": (40.55, 24.182),
}
# ==============================================================================
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

MEDIUM_MODELS = ["ecmwf_ifs", "gfs_seamless", "icon_seamless"]
MEDIUM_LABELS = {"ecmwf_ifs": "ECMWF", "gfs_seamless": "GFS", "icon_seamless": "ICON"}
# All grid-point calls: bias toward the nearest sea cell. These are small
# islands / narrow coastlines on a 9-36 km grid -- without this, a "coastal"
# request can silently resolve to a mainland or mountaintop cell instead.
CELL_SELECTION = "sea"


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
    return fetch("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon,
        "daily": DAILY,
        "models": ",".join(MEDIUM_MODELS),
        "forecast_days": MEDIUM_RANGE_DAYS,
        "wind_speed_unit": "kn",
        "timezone": TRIP_TZ.key,
        "cell_selection": CELL_SELECTION,
    })


def sea_state(lat, lon):
    return fetch("https://marine-api.open-meteo.com/v1/marine", {
        "latitude": lat, "longitude": lon,
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


def extract_medium_records(name, data):
    d = data["daily"]
    out = []
    for i, day in trip_dates(d["time"]):
        per_model = {}
        for m in MEDIUM_MODELS:
            per_model[m] = {
                "wind_mean": d.get(f"wind_speed_10m_mean_{m}", [None] * len(d["time"]))[i],
                "wind_max": d.get(f"wind_speed_10m_max_{m}", [None] * len(d["time"]))[i],
                "gust": d.get(f"wind_gusts_10m_max_{m}", [None] * len(d["time"]))[i],
                "dir": d.get(f"wind_direction_10m_dominant_{m}", [None] * len(d["time"]))[i],
                "rain": d.get(f"precipitation_sum_{m}", [None] * len(d["time"]))[i],
                "temp_lo": d.get(f"temperature_2m_min_{m}", [None] * len(d["time"]))[i],
                "temp_hi": d.get(f"temperature_2m_max_{m}", [None] * len(d["time"]))[i],
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


def extract_sea_state_records(name, data):
    d = data["daily"]
    out = []
    for i, day in trip_dates(d["time"]):
        g = lambda k: d.get(k, [None] * len(d["time"]))[i]
        out.append({
            "spot": name, "date": day,
            "wave": g("wave_height_max"), "period": g("wave_period_max"), "dir": g("wave_direction_dominant"),
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


def build_dashboard_payload(run_stamp, wind_records, wind_source_label, wind_opens_note,
                             sea_records, sea_opens_note, tiers):
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

    return {
        "dates": dates, "runStamp": run_stamp, "places": list(SPOTS.keys()),
        "tiers": tiers, "params": params,
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
        print(f"Medium-range consensus (ECMWF/GFS/ICON) will start covering the trip from ~{medium_available}.")
    else:
        print("##### Medium-range consensus: mean of ECMWF IFS / NOAA GFS / DWD ICON #####")
        print("Gust = worst case across the three. Flag = models disagree on wind "
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

    # Medium-range consensus supersedes EC46 once it's live -- it's three
    # independent models agreeing (or flagged when they don't), not a single
    # coarse ensemble mean, so prefer it for the dashboard's wind/temp/rain cards.
    if medium_records:
        wind_records, wind_source_label = medium_records, "Medium-range consensus (ECMWF/GFS/ICON)"
    elif ec46_records:
        wind_records, wind_source_label = ec46_records, "EC46 ensemble mean"
    else:
        wind_records, wind_source_label = None, None

    tiers = [
        {"name": "EC46 extended range", "state": "live" if ec46_records else "pending",
         "note": "51-member ensemble mean" if ec46_records else f"opens ~{ec46_available}"},
        {"name": "Medium-range consensus", "state": "live" if medium_records else "pending",
         "note": "ECMWF IFS / NOAA GFS / DWD ICON" if medium_records else f"opens ~{medium_available}"},
        {"name": "Sea state", "state": "live" if sea_records else "pending",
         "note": "Open-Meteo Marine" if sea_records else f"opens ~{medium_available}"},
        {"name": "ECMWF synoptic charts", "state": "live" if chart_tier_live else "pending",
         "note": "MSLP + 850 hPa wind, per day" if chart_tier_live else "opens ~9-10 days out"},
    ]

    payload = build_dashboard_payload(
        run_stamp=run_stamp,
        wind_records=wind_records, wind_source_label=wind_source_label,
        wind_opens_note=f"Opens ~{ec46_available} (EC46 extended range)",
        sea_records=sea_records,
        sea_opens_note=f"Sea state opens ~{medium_available} — rerun the forecast script closer to the trip",
        tiers=tiers,
    )
    write_dashboard(payload)
    print(f"\nDashboard written to {DASHBOARD_OUT}")
    deploy_dashboard()


if __name__ == "__main__":
    main()
