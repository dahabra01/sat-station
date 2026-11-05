#!/usr/bin/env python3
"""
Makes footprint/overlay.json: the country borders and city names the Footprint page
draws over the stitched map. Run once (or when the map area changes); the station
doesn't need it day to day.

Data: Natural Earth (public domain), from github.com/nvkelso/natural-earth-vector
  geojson/ne_50m_admin_0_boundary_lines_land.geojson
  geojson/ne_10m_populated_places_simple.geojson

Usage: make_overlay.py BORDERS.geojson PLACES.geojson [--site DIR]
"""
import json
import os
import sys

LON0, LON1, LAT0, LAT1 = 25.0, 92.0, -6.0, 54.0       # same window as azpi_footprint.py
PAD = 1.0
GULF = {"Dubai", "Abu Dhabi", "Muscat", "Doha", "Manama", "Kuwait City", "Riyadh", "Tehran",
        "Karachi", "Mumbai", "Baghdad", "Sanaa", "Jeddah", "Cairo"}


def inside(lon, lat, pad=PAD):
    return LON0 - pad <= lon <= LON1 + pad and LAT0 - pad <= lat <= LAT1 + pad


def lines_of(geom):
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    return []


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    site = sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv else "."
    borders_src, places_src = args[0], args[1]

    borders = []
    for f in json.load(open(borders_src))["features"]:
        for line in lines_of(f["geometry"]):
            run = []
            for lon, lat in line:
                if inside(lon, lat):
                    run.append([round(lon, 2), round(lat, 2)])
                elif run:
                    if len(run) > 1:
                        borders.append(run)
                    run = []
            if len(run) > 1:
                borders.append(run)

    cities = []
    for f in json.load(open(places_src))["features"]:
        p = f["properties"]
        lon, lat = p["longitude"], p["latitude"]
        if not inside(lon, lat, 0) or len(p["name"]) > 18:
            continue
        capital = p["featurecla"] in ("Admin-0 capital", "Admin-0 capital alt")
        if p["name"] in GULF or capital or p["scalerank"] <= 1:
            tier = 1
        elif p["scalerank"] <= 3:
            tier = 2
        else:
            continue
        cities.append({"name": p["name"], "lon": round(lon, 3), "lat": round(lat, 3), "tier": tier,
                       "capital": capital, "pop": int(p.get("pop_max") or 0)})
    # most important first: the page places labels in this order and skips ones that would overlap
    cities.sort(key=lambda c: (c["name"] != "Dubai", c["tier"], -c["pop"]))

    out = os.path.join(site, "footprint", "overlay.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"source": "Natural Earth (public domain)", "borders": borders, "cities": cities},
                  fh, separators=(",", ":"))
    print(f"{out}: {len(borders)} border lines, {len(cities)} cities, {os.path.getsize(out) // 1024} KB")


if __name__ == "__main__":
    main()
