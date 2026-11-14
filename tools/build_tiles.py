#!/usr/bin/env python3
"""Zerlegt alle Automaten (amenity=vending_machine) aus einem OSM-Export in kleine Kacheln.

Aufruf: build_tiles.py <vending.geojsonseq> <gebiet.poly> <ausgabeordner> [--with-atm]

- Ohne --with-atm: nur Automaten (Datenstand v1, für App-Version 1.0).
- Mit --with-atm: zusätzlich Geldautomaten (amenity=atm, Banken mit atm=yes) samt amenity-Tag (v2).

- Eingabe: `osmium export … -f geojsonseq -a type,id` (Punkte, Linien, Flächen)
- Kacheln: 0,1° × 0,1° (ca. 11 × 7 km), Datei <ausgabe>/<floor(lat*10)>/<floor(lon*10)>.json
- Jede Kachel im abgedeckten Gebiet bekommt eine Datei, auch wenn sie leer ist ("[]").
  Fehlt eine Kachel (HTTP 404), liegt sie außerhalb des Gebiets – die App fragt dann Overpass.
- Inhalt je Kachel: [[id, lat, lon, {tags}], …]

Daten © OpenStreetMap-Mitwirkende, ODbL 1.0.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

TILE = 0.1
KEEP_TAGS = {
    "name", "brand", "operator", "vending", "opening_hours", "description", "note",
    "addr:street", "addr:housenumber", "addr:city", "location", "indoor", "level", "access",
}
ATM_TAGS = {"amenity", "atm", "network", "cash_in", "fee"}


def is_wanted(props, with_atm):
    amenity = props.get("amenity")
    if amenity == "vending_machine":
        return True
    if not with_atm:
        return False
    return amenity == "atm" or (amenity == "bank" and props.get("atm") == "yes")


def tile_key(lat, lon):
    return math.floor(lat / TILE + 1e-9), math.floor(lon / TILE + 1e-9)


def representative_point(geometry):
    """Punkt bzw. Mittelwert der äußeren Koordinaten einer Linie/Fläche."""
    kind, coords = geometry["type"], geometry["coordinates"]
    if kind == "Point":
        return coords[1], coords[0]
    if kind == "LineString":
        ring = coords
    elif kind == "Polygon":
        ring = coords[0]
    elif kind == "MultiPolygon":
        ring = coords[0][0]
    else:
        return None
    if not ring:
        return None
    lon = sum(c[0] for c in ring) / len(ring)
    lat = sum(c[1] for c in ring) / len(ring)
    return lat, lon


def read_poly(path):
    """Liest eine .poly-Datei: Liste von (ist_loch, [(lon, lat), …])."""
    rings, current, hole = [], None, False
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    for line in lines[1:]:  # erste Zeile = Name
        if current is None:
            if line == "END":
                break
            hole = line.startswith("!")
            current = []
        elif line == "END":
            rings.append((hole, current))
            current = None
        else:
            lon, lat = map(float, line.split()[:2])
            current.append((lon, lat))
    return rings


def inside(ring, lon, lat):
    result = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            result = not result
        j = i
    return result


def covered_tiles(rings):
    lons = [p[0] for _, r in rings for p in r]
    lats = [p[1] for _, r in rings for p in r]
    la0, lo0 = tile_key(min(lats), min(lons))
    la1, lo1 = tile_key(max(lats), max(lons))
    outer = [r for h, r in rings if not h]
    holes = [r for h, r in rings if h]
    tiles = set()
    for la in range(la0, la1 + 1):
        for lo in range(lo0, lo1 + 1):
            # Kachel zählt, wenn eine Ecke oder die Mitte im Gebiet liegt.
            samples = [(la + dy, lo + dx) for dy in (0.0, 0.5, 1.0) for dx in (0.0, 0.5, 1.0)]
            for sy, sx in samples:
                lat, lon = sy * TILE, sx * TILE
                if any(inside(r, lon, lat) for r in outer) and not any(inside(r, lon, lat) for r in holes):
                    tiles.add((la, lo))
                    break
    return tiles


def main():
    src, poly, out = sys.argv[1:4]
    with_atm = "--with-atm" in sys.argv[4:]
    keep = KEEP_TAGS | ATM_TAGS if with_atm else KEEP_TAGS
    tiles = {}
    seen = set()
    count = 0
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            props = feature.get("properties") or {}
            if not is_wanted(props, with_atm):
                continue  # z. B. mitexportierte Knoten von Flächen
            osm_id = f'{props.get("@type", "node")}/{props.get("@id")}'
            if osm_id in seen:
                continue
            point = representative_point(feature.get("geometry") or {})
            if point is None:
                continue
            seen.add(osm_id)
            tags = {k: v for k, v in props.items()
                    if k in keep or (k.startswith("payment:") and v == "yes")}
            lat, lon = point
            tiles.setdefault(tile_key(lat, lon), []).append([osm_id, round(lat, 6), round(lon, 6), tags])
            count += 1

    covered = covered_tiles(read_poly(poly)) | set(tiles)
    for la, lo in covered:
        folder = os.path.join(out, str(la))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{lo}.json"), "w", encoding="utf-8") as f:
            json.dump(tiles.get((la, lo), []), f, ensure_ascii=False, separators=(",", ":"))

    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "OpenStreetMap via Geofabrik (germany-latest.osm.pbf)",
        "license": "ODbL 1.0 – © OpenStreetMap-Mitwirkende, https://www.openstreetmap.org/copyright",
        "tileSizeDegrees": TILE,
        "points": count,
        "tiles": len(covered),
        "tilesWithData": len(tiles),
    }
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
