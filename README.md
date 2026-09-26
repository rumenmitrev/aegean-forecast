# Aegean Forecast

A sailing-forecast pipeline for the North Aegean (Samothrace, Lemnos,
Thassos, and nearby spots). Every run:

1. Pulls ECMWF EC46 extended-range, medium-range consensus (ECMWF IFS / NOAA
   GFS / DWD ICON), and Open-Meteo Marine sea-state data -- whichever tiers
   are close enough to the trip dates to have real skill. The medium-range
   consensus samples each model over a 3x3 grid kernel around every spot
   (not one point) -- see "How the numbers are calculated" below. EC46 fills
   in dates beyond the medium-range horizon so the back half of the trip
   isn't blank.
2. Fetches ECMWF official synoptic charts (two products per trip day: MSLP +
   850 hPa wind, and 500 hPa geopotential + 850 hPa temperature) from
   `charts.ecmwf.int` via the OpenCharts API (~9-10 days out). Saved to
   `charts/` and deployed alongside the dashboard in `site/charts/`. Chart
   URLs are cached in `.url` sidecar files; downloads are skipped when the
   URL hasn't changed. The newest ECMWF run is tried first; dates it doesn't reach fall
   back to the newest 00Z/12Z run (06Z/18Z runs only go to +144h, 00Z/12Z to +240h).
3. Fetches GFS 500 hPa upper-atmosphere data (geopotential height, temperature,
   wind) and convection indicators (max CAPE across all spots, min 850 hPa
   temperature, SST from the marine model) for synoptic context.
4. Prints console tables and writes a row per place/day/tier to `runs.csv`
   -- overwritten fresh each run, so it always reflects only the latest
   run's data (past runs' snapshots still exist in git history, just not
   accumulated in the file itself).
5. Asks Claude (`claude-opus-5`) for three types of AI analysis (all skipped
   cleanly if `ANTHROPIC_API_KEY` isn't set):
   - **Sailing briefing** -- narrative comparing this run to the previous one,
     covering regime, per-spot roughness/calm, disagreement days, convection
     and squall risk, 500 hPa synoptic context, and routing/timing advice.
   - **Synoptic chart summaries** -- for each downloaded chart image, a 2-4
     sentence plain-English interpretation (multimodal: Claude reads the PNG).
     Summaries are cached by chart URL in `charts/summaries.json`.
   - **Disagreement notes** -- for each flagged place/day, a 1-3 sentence note
     explaining what the models are split on, informed by 500 hPa and
     convection data where available.
6. Writes `dashboard.html` with:
   - Real-coastline map card
   - Official-sources bar (before the AI summary) with links from `area.json`
   - AI sailing summary (clearly labelled as interpretation only)
   - Wind, wind direction, wind max, gust, rain, temperature cards (medium-range
     consensus or EC46 fill); ⚠ cells open a popup with per-model table + AI note
   - Wave height (Hs), wave direction, wave period cards; steep-sea cells amber
   - **Passage planner** -- pick any two spots, departure time and boat speed;
     browser fetches GFS hourly wind live and shows per-hour wind, angle to
     heading, point of sail, and gust along the route
   - **Convection / squall risk** card -- daily CAPE (worst across spots),
     min T850, SST, SST-T850 delta; flags CAPE > 500 J/kg and delta > 13°C
   - **Synoptic charts** card -- date tabs and MSLP/500hPa toggle with AI
     synoptic interpretation per chart
   - Status strip showing which tiers are live
7. Deploys to EdgeOne Pages via the `edgeone` CLI, so the same URL stays
   current run after run.

Automated via a scheduled GitHub Actions workflow (`.github/workflows/forecast.yml`, once daily at 10:00 Europe/Athens) that runs the script and commits the updated `runs.csv`/`dashboard.html` back to the repo.

## How the numbers are calculated

The medium-range consensus (`area.json`'s `medium_models`, three global models by default -- the
tier that's live from ~15 days out) doesn't read a single point per spot. Each model is sampled
over a **3x3 grid kernel** (`area.json`'s `kernel_step_deg`, default `0.15` -> ~30x25 km at these
latitudes) around every spot -- a boat sailing a spot moves through that whole patch of sea, not
one exact GPS pin. Open-Meteo batches all 9 points into one HTTP call per model set, so the wider
kernel costs nothing extra in requests.

- **Wind speed, temperature, rain, direction**: each model's **mean** across the 9 kernel points --
  smooths single-cell grid noise for values that vary smoothly in space.
- **Gust**: each model's **max** across the 9 kernel points instead of the mean -- gust is already
  a worst-case figure, so averaging it away would blunt a real local peak (e.g. a gap-wind gust one
  grid cell over) instead of surfacing it.
- Each model's kernel-combined values are then **combined the same way across models**: mean
  for wind/temp/rain/direction, max for gust. So the gust shown is deliberately the strongest
  plausible gust anywhere in that ~30x25 km patch, across every configured model -- a safety
  margin, not a literal single-point prediction.
- **Wind (max)**: the daily highest hourly mean wind. Shown alongside gust so you can compute
  the gust/wind-max ratio (1.3–1.5 is normal open-sea; well above that is worth noting). Gust
  is the single worst 3-second moment from the worst kernel cell across all models; wind-max is
  a more meaningful comparator than wind-mean (which averages 24 hours including calm nights).
- **Model disagreement**: a place/day is flagged when the models' wind speeds spread by more
  than `area.json`'s `spread_wind_kt` (default 6kt), or their directions by more than
  `spread_dir_deg` (default 45deg) while wind is at least `dir_flag_min_wind_kt` (default 5kt).
  Only models blowing above the wind threshold are compared for direction. Flagged wind cells
  show the actual min–max span instead of a blended mean; flagged direction cells show the
  per-model bearings (e.g. `NE / SW`); when the model directions cancel completely the cell
  shows **VAR**. The circular mean is computed with `min_r=0.5` for the model consensus (VAR
  when resultant < 50%, i.e. spread > 120°) and `min_r=0.15` elsewhere (per-model kernel
  average, Poseidon daily mean, 500 hPa flow, week's predominant direction). Clicking a ⚠ cell
  opens a popup with the per-model table, numeric spreads, and an AI note for that day.
- **EC46 gap-fill**: when medium-range and EC46 are both live, dates beyond the medium-range
  horizon are filled from EC46. Each record carries a `source` field (`"medium"` or `"ec46"`)
  passed to the AI prompt so it doesn't misread EC46's smoothed late-week values as a consensus
  calm forecast.
- **Poseidon directions**: HCMR's `wangle` field is the direction the wind/waves blow **toward**
  (TO convention), confirmed by cross-check against Open-Meteo GFS. Both wind and wave wangle
  are converted to FROM convention (`+180° mod 360`) before use.

Sea state (wave height, period, direction; Open-Meteo Marine) uses the same 3x3 kernel and the
same max-vs-mean split: **wave height is the kernel max** (a boat crossing this patch of sea can
meet its roughest cell, not just the exact point -- the same reasoning as gust), **period and
direction are the kernel mean**. Marine is a single model, so unlike wind there's no further
multi-model combine on top -- the kernel is the only combining step for sea state.
Open-Meteo's default (`best_match`) wave model in the Aegean is Meteo-France MFWAM, which stops
~9-10 days out; trip dates it leaves blank are filled from ECMWF WAM (`ecmwf_wam025`, 15 days),
tagged in `runs.csv`'s model column and marked `*` on the dashboard.

EC46 (the coarser, farther-out tier) is the one tier still read at a single point with no kernel
or per-model combining at all -- it's already ECMWF's own finished 51-member ensemble mean, so
there's nothing to kernel-combine. The dashboard's footer explains all of this in plain language
for a reader, and the Claude prompt in `generate_sailing_summary()` gets its own methodology
paragraph (branched by tier, plus a sea-state note) so the AI summary reasons about
`model_flag`/gust/wave correctly instead of assuming a plain point forecast.

## Files

| File | What it is |
|---|---|
| `aegean_forecast.py` | The pipeline: fetch, log, render, deploy |
| `area.json` | Everything specific to this area/trip: dates, timezone, spots, header text, chart region, EdgeOne project name -- see "Setting up a new area" below |
| `dashboard_template.html` | Static HTML/CSS/JS shell; `aegean_forecast.py` fills in the data (including header text and place labels, sourced from `area.json`) |
| `fetch_coastline.py` | One-off tool: fetches real OSM coastline for the map card (rerun only when `area.json`'s spots change) |
| `runs.csv` | Historical log, one row per place/day/tier/run |
| `charts/` | Downloaded ECMWF synoptic chart PNGs (both products, per trip day); copied to `site/charts/` at write time |
| `dashboard.html` / `site/index.html` | Generated output (the latter is what actually gets deployed) |
| `site/charts/` | Chart PNGs deployed alongside `index.html` |
| `coastline.json` | Generated by `fetch_coastline.py`; embedded into the dashboard |
| `edgeone_token.txt` | EdgeOne API token (gitignored, never commit) |
| `anthropic_api_key.txt` | Claude API key for sailing summary + chart summaries + disagreement notes (gitignored, never commit) |

## Setting up a new area

Steps to point this whole pipeline at a different sailing area and/or
different trip dates.

Decide first: **new repo, or reuse this one?** Everything below assumes a
fresh copy of the project (new directory, new git repo) for a second area --
running two areas' automation against the same files means each one's
scheduled run overwrites the other's `runs.csv`/`dashboard.html`/deploy with
its own latest snapshot instead of the two coexisting. If you're just
changing dates for the *same* area, you can edit this repo in place and skip
the repo/deploy-identity steps (8, 9).

### 1. Copy the project

```
cp -r aegean-forecast new-area-forecast
cd new-area-forecast
rm -rf .git node_modules __pycache__ .edgeone site charts
rm -f runs.csv dashboard.html coastline.json cron.log
rm -f edgeone_token.txt tencentcloud_credentials.env anthropic_api_key.txt   # never copy secrets
```

You'll reinstall `node_modules` (step 5) and regenerate everything else
(steps 4-10) fresh.

### 2. Edit `area.json`

This is the only file that needs new-area content -- no code edits, no
HTML edits. It supports plain `//` line comments despite the `.json`
extension (`aegean_forecast.py` strips them before parsing -- see
`strip_json_comments()`), so the shipped file documents every field inline;
a strict JSON linter will flag those comments as invalid even though the
pipeline reads the file fine. Fields:

```json
{
  "trip_start": "2026-10-03",
  "trip_end": "2026-10-10",
  "timezone": "Europe/Athens",
  "chart_projection": "opencharts_south_east_europe",
  "edgeone_project_name": "aegean-forecast",
  "page_title": "Aegean Forecast",
  "eyebrow": "North Aegean · sailing forecast",
  "heading": "Thassos · Samothrace · Lemnos",
  "medium_models": [
    {"code": "ecmwf_ifs", "label": "ECMWF IFS"},
    {"code": "gfs_seamless", "label": "NOAA GFS"},
    {"code": "icon_seamless", "label": "DWD ICON"}
  ],
  "spread_wind_kt": 6,
  "spread_dir_deg": 45,
  "dir_flag_min_wind_kt": 5,
  "kernel_step_deg": 0.15,
  "map_pad_lon": 0.55,
  "map_pad_lat": 0.45,
  "local_knowledge": "...",
  "spots": [
    {"name": "Keramoti / Thassos", "lat": 40.85, "lon": 24.70,
     "short": "Keramoti", "short_mobile": "Ker"}
  ]
}
```

- **`trip_start` / `trip_end` / `timezone`** -- ISO dates and an IANA
  timezone name for the new area (e.g. `"America/Nassau"` for the Bahamas).
  `timezone` alone drives every date computation *and* is passed to
  Open-Meteo, so this one field is the only place to change it.
- **`chart_projection`** -- the ECMWF opencharts region code covering the
  new area. To find the valid list: temporarily call
  `opencharts_product(CHART_PRODUCT, "2099-01-01T00:00:00Z")` (an
  obviously-invalid date) from a `python3 -i` shell -- the error message
  lists every valid projection code. Pick the one that covers the new area.
- **`edgeone_project_name`** -- give it a new name. Reusing the old one
  overwrites the live site for the original area instead of creating a
  second one.
- **`page_title` / `eyebrow` / `heading`** -- the dashboard's tab title,
  small caps label, and big `<h1>`. The route line under them (dates + place
  list) is generated automatically from `trip_start`/`trip_end` and each
  spot's `short` label -- nothing to write by hand for that part.
- **`medium_models`** -- which independent models feed the medium-range
  consensus tier. Each entry's `code` is the exact Open-Meteo model
  identifier (fed to the API call and written to `runs.csv`'s `model`
  column); `label` is the human-readable name shown in console output, the
  dashboard, and the Claude prompt. A different area might have
  better-skilled regional models worth using instead of these three global
  ones -- console output, the dashboard footer, and the Claude prompt all
  read this list, not a hardcoded name, so changing it needs no other edit.
- **`spread_wind_kt` / `spread_dir_deg` / `dir_flag_min_wind_kt`** (optional,
  defaults `6`/`45`/`5`) -- the model-disagreement flag thresholds (see "How
  the numbers are calculated" above). A calmer/steadier cruising ground
  might reasonably want tighter thresholds than this route's mix of open
  channel and sheltered coast.
- **`spots`** -- the new places, each with `lat`/`lon` plus explicit
  `short`/`short_mobile` labels (table headers, legend, map pins, and phone-
  width columns respectively). Labels are never auto-abbreviated --
  collision-prone and silently wrong for odd names (nothing would catch two
  spots both landing on the same 3 letters) -- so write both by hand.
  Up to 12 spots need no other change; see step 3 for more than that.
- **`kernel_step_deg`** (optional, default `0.15`) -- the medium-range/sea-
  state kernel size in degrees (see "How the numbers are calculated" above).
  Only worth changing if the new area's geography is much tighter or looser
  than this route's mix of open water and coast.
- **`map_pad_lon` / `map_pad_lat`** (optional, default `0.55`/`0.45`) --
  how far past the spots (in degrees) `fetch_coastline.py` extends the map
  view. A tightly clustered set of spots wants less padding than a
  spread-out route; see step 4's sanity-check note if a pin ends up too
  close to the map's edge or a landmass gets cut off.
- **`local_knowledge`** (optional, default empty) -- a paragraph of
  area-specific sailing effects (channel funneling, gap winds, which spots
  are sheltered) fed to the AI summary prompt. Leave it out entirely for a
  new area until you've written a real one -- an empty/missing field just
  skips that paragraph, rather than feeding the model North-Aegean-specific
  effects for a place they don't exist in.

### 3. Edit `dashboard_template.html` (usually nothing to do here)

Only touch this file if:
- **More than 12 spots** -- `PALETTE_LIGHT`/`PALETTE_DARK` in the `<script>`
  (search for `applyPlacePalette`) have 12 hand-tuned colors; add a matching
  pair of hex values to both arrays for each spot beyond that, or extra
  spots cycle color reuse instead of erroring.
- **Renaming the script file** away from `aegean_forecast.py`, or **changing
  the kernel/flag constants** (`area.json`'s `kernel_step_deg`, or
  `aegean_forecast.py`'s `SPREAD_WIND_KT`/`SPREAD_DIR_DEG`/
  `DIR_FLAG_MIN_WIND_KT`) -- the footer's explanatory numbers are
  hand-written to match those, not templated in.

Everything else (title, header text, route line, `SHORT`/`SHORT_MOBILE`
labels, place colors) is already sourced from `area.json` at render time.

### 4. Regenerate the coastline map

```
pip install shapely
python3 fetch_coastline.py
```

This queries OpenStreetMap's Overpass API for real coastline in a box
around `area.json`'s spots, simplifies it, and writes `coastline.json`. It
reads the spots from `aegean_forecast.py` (which itself loads `area.json`),
so there's nothing separate to configure -- just rerun it whenever the
spots change. `shapely` is only needed for this one-off tool, not for
`aegean_forecast.py` itself.

Sanity-check the result before trusting it: open `dashboard.html` (after
step 8) and look at the map card -- coastlines should actually resemble the
real area, pins should land on/near their real locations. If a pin sits in
open water or a big landmass is missing, adjust `area.json`'s
`map_pad_lon`/`map_pad_lat` (default 0.55/0.45 degrees if omitted) for the
new area's geography -- or pass `--pad-lon`/`--pad-lat` to override just
for one run without editing the config.

### 5. Reinstall the EdgeOne CLI locally

```
npm install edgeone
```

(`package.json`/`package-lock.json` came with the copy in step 1, so this
just restores `node_modules`, which was deliberately not copied.)

### 6. Get an EdgeOne API token

If you're deploying under the **same EdgeOne account** as the original
area, you can reuse that account but still need a **fresh token scoped to
the new project** -- or just reuse the existing token if it's account-wide
rather than project-scoped (check in the Makers console). Simplest path:
generate a new one at **console.tencentcloud.com/edgeone/makers** -> your
project -> **Settings** -> **API Token** -> **Create API Token**.

Save it locally for testing:
```
echo -n "YOUR_TOKEN" > edgeone_token.txt
chmod 600 edgeone_token.txt
```

### 7. Get a Claude API key (for the AI sailing summary)

Only needed if you want that card -- the rest of the pipeline runs fine
without it (the summary card just stays hidden). Generate a key at
**console.anthropic.com** -> **API Keys** -> **Create Key** (this is the
separate developer console, not claude.ai). If the key is identity-linked
with access to multiple workspaces, API calls will 400 asking for a
workspace id -- either scope the key to one workspace in the console, or
set `ANTHROPIC_WORKSPACE_ID` in `aegean_forecast.py` to the workspace id
(from the console URL when that workspace is selected) and pass it via
`default_headers={"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID}` on the
client, same as the current code does.

Save it locally for testing:
```
echo -n "YOUR_KEY" > anthropic_api_key.txt
chmod 600 anthropic_api_key.txt
```

### 8. Test locally before automating anything

```
python3 aegean_forecast.py
```

Check the console output looks right for the new area/dates, open
`dashboard.html` in a browser, confirm the map, AI summary, and all cards
render correctly, and confirm the "Published to EdgeOne Pages: ..." line at
the end gives you a working URL.

### 9. Set up GitHub + automation (optional, mirrors the original area)

```
git init
git branch -m main
git add -A                    # review with `git status` -- confirm no
                               # secrets are staged (edgeone_token.txt,
                               # anthropic_api_key.txt, any credentials
                               # file) before committing
git commit -m "Initial commit: <new area> forecast pipeline"
gh repo create <new-repo-name> --private --source=. --remote=origin --push
gh secret set EDGEONE_API_TOKEN < edgeone_token.txt
gh secret set ANTHROPIC_API_KEY < anthropic_api_key.txt   # skip if not using the AI summary
```

Then edit `.github/workflows/forecast.yml`'s cron schedule for the new
area's timezone. GitHub Actions cron is always UTC with no DST awareness --
convert the desired local run time to UTC using the new area's *current*
offset, and leave a comment noting when it'll drift (next DST changeover)
same as the original file does.

Trigger it once manually before trusting the schedule:
```
gh workflow run forecast.yml
gh run watch $(gh run list --workflow=forecast.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
```

### 10. Retire (or keep) the old automation

If this is meant to replace the original area's automation rather than run
alongside it, remove the old crontab entry / disable the old repo's
workflow so the two don't both fire.

### Quick checklist

- [ ] `area.json`: dates, timezone, chart projection, EdgeOne project name,
      page title/eyebrow/heading, `medium_models`, spots (with explicit
      `short`/`short_mobile` per spot), optional `spread_wind_kt`/
      `spread_dir_deg`/`dir_flag_min_wind_kt`/`kernel_step_deg`/
      `map_pad_lon`/`map_pad_lat`/`local_knowledge`
- [ ] `dashboard_template.html`: only if more than 12 spots (extend the
      palette arrays) -- everything else is already `area.json`-driven
- [ ] `pip install shapely && python3 fetch_coastline.py` -- verify the map
      visually
- [ ] `npm install edgeone`
- [ ] New EdgeOne token saved to `edgeone_token.txt`
- [ ] (Optional) new Claude API key saved to `anthropic_api_key.txt` for the
      AI summary card, per-chart synoptic summaries, and disagreement notes
- [ ] Local test run looks right, dashboard renders, deploy URL works
- [ ] (Optional) new GitHub repo, `EDGEONE_API_TOKEN` + `ANTHROPIC_API_KEY`
      secrets, workflow cron adjusted for the new timezone, manually
      triggered once and confirmed green
- [ ] Old automation (cron or Actions) disabled if this replaces it
