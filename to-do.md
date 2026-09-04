# To-do / future improvements

## Chart tier (tier 4) is single-model (ECMWF) by design, not a gap to close

`chart_for_date()` pulls a real rendered synoptic chart (MSLP + 850hPa wind)
from `charts.ecmwf.int` -- ECMWF's own public chart-rendering service, IFS
only. Asked whether GFS/ICON charts could be added the same way medium-range
combines all three models (tier 2): **checked live, they can't be, cheaply**:

- No equivalent documented, stable, region-selectable chart API exists for
  the other two models. `charts.ecmwf.int`'s opencharts API (structured
  JSON, per-date lookup, named region codes) is the unusual one here, not
  the norm.
- NOAA/WPC publishes surface analysis charts, but only via an archive
  *webpage* (https://www.wpc.ncep.noaa.gov/archives/web_pages/sfc/sfc_archive_zoom.php),
  not a documented API -- would mean reverse-engineering URLs from a
  human-facing page with no stability guarantee. Worse: WPC's surface
  analysis is North-America-focused and may not even cover the North
  Aegean at all.
- DWD has no rendered-chart endpoint in its public offerings at all --
  Bright Sky (brightsky.dev) and Open-Meteo's DWD API give numeric/JSON
  data (already what tiers 1-3 use), and DWD's own Open Data Server
  (opendata.dwd.de) serves raw GRIB model data, not pre-rendered images.

Real alternative, if this is ever revisited: not "add 2 more chart sources"
but either (a) scrape the undocumented NOAA archive page and accept the
region-coverage risk, or (b) build an actual chart-rendering pipeline from
raw gridded data (matplotlib/cartopy, contouring, projection handling) for
GFS/ICON -- a real standalone project, not a small addition. Also note:
even with all three, "averaging" rendered chart images the way tier 2
averages numbers isn't well-defined -- overlaying three models' pressure
contours doesn't produce a readable consensus front; a true multi-model
chart consensus would mean averaging raw gridded MSLP fields *before*
contouring, not blending finished images. Decided not worth the risk of
touching the working single-chart tier for this -- leaving as ECMWF-only.
