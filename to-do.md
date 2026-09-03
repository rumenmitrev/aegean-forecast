# To-do / future improvements

## Config-driven area/spots/dates (no code edits to switch area)

Currently switching areas means hand-editing constants in
`aegean_forecast.py` and strings/maps in `dashboard_template.html` (see the
`# === NEW AREA` markers and `README.md`'s setup guide). Goal: replace that
with a single JSON (or txt) config file the script loads at startup.

**Config file** (e.g. `area.json`), schema:
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
  "spots": [
    {"name": "Keramoti / Thassos", "lat": 40.85, "lon": 24.70,
     "short": "Keramoti", "short_mobile": "Ker"},
    ...
  ]
}
```

**`aegean_forecast.py` changes:**
- Load the config once near the top; replace the hardcoded `TRIP_START`,
  `TRIP_END`, `TRIP_TZ`, `SPOTS`, `CHART_PROJECTION`, `EDGEONE_PROJECT_NAME`
  assignments with values read from it. Low risk -- these are already
  isolated module-level constants (that's why the `# === NEW AREA` markers
  could point at them precisely), so everything downstream keeps working
  unchanged as long as the names/types stay the same.
- `build_dashboard_payload()` needs to also pass through `page_title`,
  `eyebrow`, `heading`, and each spot's `short`/`short_mobile` labels, so
  the template has zero hardcoded per-area strings left.

**`dashboard_template.html` changes:**
- Replace the static `<title>`, `.eyebrow`, `<h1>`, `.route` text with JS
  that fills them from `DATA` at render time (route line can be generated
  from the places list plus trip dates, no need to hand-author it).
- Replace the hardcoded `SHORT` / `SHORT_MOBILE` JS objects with lookups
  into `DATA.params`/a new `DATA.labels` structure sourced from the config's
  per-spot `short`/`short_mobile` fields.
- **Design decision, already made:** do NOT auto-abbreviate place names
  algorithmically for `short`/`short_mobile` -- collision-prone and
  produces silently-bad output for odd names (nothing catches "two spots
  both abbreviate to the same 3 letters"). Require the config to specify
  both labels explicitly per spot instead.
- Replace the fixed `--place-1` through `--place-7` CSS custom properties
  with a larger (10-12 entry) hand-tuned color palette array indexed in JS,
  so adding a spot (up to that count) needs no CSS edit at all.

**Stays manual, on purpose:**
- `fetch_coastline.py` still needs a one-time run per area -- real
  coastline geometry for a new region has to come from somewhere (OSM/
  Overpass), a config file can't substitute for that fetch.
- EdgeOne project identity / GitHub repo / Actions cron schedule -- these
  are about *where* it deploys and *when* it's automated, not what data it
  shows, and should stay a deliberate step rather than config-driven.

**Estimated effort:** a few focused hours, not a rewrite -- mechanical
constant/string relocation plus one JS refactor (header + label rendering),
no architectural changes to the fetch/log/render/deploy pipeline itself.
