#!/usr/bin/env python3
"""
Fetch, stitch, simplify, and clip real OpenStreetMap coastline data for the
dashboard's map card -- run this ONCE per area (not per forecast run; unlike
runs.csv/dashboard.html, coastline.json only changes when SPOTS changes).

New area: after updating the spots list in area.json, rerun this script to
regenerate coastline.json. It reads SPOTS from aegean_forecast.py (which
itself loads from area.json), so there's nothing separate to keep in sync.
Needs `pip install shapely` (not a runtime dependency of aegean_forecast.py
itself, only of this one-off tool).

Usage:
    python3 fetch_coastline.py [--pad-lon 0.55] [--pad-lat 0.45]

--pad-lon/--pad-lat default to area.json's map_pad_lon/map_pad_lat (0.55/0.45
if that area.json omits them) -- pass either flag explicitly to override
just for this run without editing the config.
"""
import argparse
import json
import pathlib

import requests
from shapely.geometry import LineString, MultiLineString, box
from shapely.ops import linemerge

from aegean_forecast import SPOTS, load_area_config

OUT_PATH = pathlib.Path(__file__).resolve().parent / "coastline.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Points closer together than this (in degrees) collapse to one -- ~150m at
# this latitude. Fine for a schematic reference map, not for navigation.
SIMPLIFY_TOLERANCE_DEG = 0.0015
# Fragments shorter than this are Overpass noise (single rocks/skerries) at
# our zoom level -- illegible specks, not worth the bytes.
MIN_FRAGMENT_POINTS = 6


def query_bbox():
    lats = [lat for lat, lon in SPOTS.values()]
    lons = [lon for lat, lon in SPOTS.values()]
    return min(lats), min(lons), max(lats), max(lons)


def fetch_coastline_ways(south, west, north, east):
    query = f"""
    [out:json][timeout:60];
    (
      way["natural"="coastline"]({south},{west},{north},{east});
    );
    out body;
    >;
    out skel qt;
    """
    # Overpass 406s requests' default User-Agent (confirmed) -- needs any
    # descriptive one.
    headers = {"User-Agent": "aegean-forecast-coastline-fetch/1.0 (personal project)"}
    r = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=90)
    r.raise_for_status()
    return r.json()["elements"]


def stitch_and_simplify(elements):
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    ways = [e for e in elements if e["type"] == "way"]
    lines = []
    for w in ways:
        coords = [nodes[n] for n in w["nodes"] if n in nodes]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    merged = linemerge(MultiLineString(lines))
    parts = [merged] if merged.geom_type == "LineString" else list(merged.geoms)
    return [p.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=False) for p in parts]


def clip_to_view(parts, minlon, minlat, maxlon, maxlat):
    clip_box = box(minlon, minlat, maxlon, maxlat)
    clipped = []
    for p in parts:
        inter = p.intersection(clip_box)
        if inter.is_empty:
            continue
        geoms = [inter] if inter.geom_type == "LineString" else list(inter.geoms)
        clipped.extend(g for g in geoms if len(list(g.coords)) >= MIN_FRAGMENT_POINTS)
    return clipped


def main():
    area = load_area_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad-lon", type=float, default=area.get("map_pad_lon", 0.55))
    ap.add_argument("--pad-lat", type=float, default=area.get("map_pad_lat", 0.45))
    args = ap.parse_args()

    south, west, north, east = query_bbox()
    view_minlon, view_maxlon = west - args.pad_lon, east + args.pad_lon
    view_minlat, view_maxlat = south - args.pad_lat, north + args.pad_lat

    # Query Overpass over the PADDED view bbox, not the tight SPOTS bbox --
    # otherwise context beyond the pins themselves (e.g. the far side of an
    # island whose center is a pin but whose coastline extends past it) never
    # gets fetched in the first place, no matter how the clip step pads it.
    print(f"Querying Overpass for coastline in ({view_minlat},{view_minlon},{view_maxlat},{view_maxlon})...")
    elements = fetch_coastline_ways(view_minlat, view_minlon, view_maxlat, view_maxlon)
    print(f"  {sum(1 for e in elements if e['type'] == 'way')} raw ways, "
          f"{sum(1 for e in elements if e['type'] == 'node')} nodes")

    parts = stitch_and_simplify(elements)
    print(f"  {len(parts)} stitched/simplified lines")

    clipped = clip_to_view(parts, view_minlon, view_minlat, view_maxlon, view_maxlat)
    total_pts = sum(len(list(p.coords)) for p in clipped)
    print(f"  {len(clipped)} lines kept after clip+noise-drop, {total_pts} points total")

    payload = {
        "bbox": [view_minlon, view_minlat, view_maxlon, view_maxlat],
        "lines": [list(p.coords) for p in clipped],
        "pins": {name: [lat, lon] for name, (lat, lon) in SPOTS.items()},
    }
    OUT_PATH.write_text(json.dumps(payload), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")
    print("Rerun aegean_forecast.py (or just write_dashboard) to embed it in the page.")


if __name__ == "__main__":
    main()
