#!/usr/bin/env python3
"""Parse a Dyson Sphere Program blueprint (.txt) and dump the orbital-collector geometry.

No third-party dependencies. Only reads; never writes to the blueprint.

Usage:
    python tools/analyze_blueprint.py blueprints/160_orbital_collectors.txt
    python tools/analyze_blueprint.py <file> --csv positions.csv

Blueprint format notes (game version 0.10.35):
    FILE  := 'BLUEPRINT:1,<header fields>"<base64(gzip(payload))>"<32 hex MD5F>'
    PAYLOAD := int32 version(2), int32 cursor_x, cursor_y, targetArea,
               dragBox_x, dragBox_y, int32 primaryAreaIdx, byte areaCount,
               area[areaCount], int32 buildingCount, building[buildingCount],
               int32 patch, byte hasReformData
    AREA     := 14 bytes: sbyte index, sbyte parent, int16 tropicAnchor,
                int16 segments, int16 anchorX, int16 anchorY, int16 width, int16 height
    BUILDING := int32 -102, int32 index, int16 itemId, int16 modelIndex, sbyte area,
                float x, y, z, yaw  (grid units), optional per-type extras,
                int32 outObj, int32 inObj, 6 x sbyte, int16 recipe, int16 filter,
                int16 paramCount, int32[paramCount], int32 contentLength
"""
import argparse
import base64
import gzip
import io
import json
import math
import struct
import sys

# Gas giant constants, game 0.10.35
REAL_RADIUS = 200.0          # gas giant surface radius (planet radius is 200 for every planet)
GRID_SEGMENTS = 80           # longitude segments at the equator of a gas giant grid
GRID_DEG = 360.0 / (GRID_SEGMENTS * 5)   # 0.9 deg per grid cell (5 cells per segment)
HEIGHT_UNIT = 1.3333333      # z grid unit -> world units
HEIGHT_BIAS = 0.2


def split_blueprint(text):
    text = text.strip()
    if not text.startswith("BLUEPRINT:"):
        raise ValueError("not a blueprint file")
    quote = text.rfind('"')
    head = text[:quote]
    b64 = head[head.index('"') + 1:]
    return head[:head.index('"')], b64


def parse_payload(raw):
    r = io.BytesIO(raw)
    version = struct.unpack("<i", r.read(4))[0]
    cursor = struct.unpack("<iiiiii", r.read(24))
    area_count = struct.unpack("<B", r.read(1))[0]
    areas = []
    for _ in range(area_count):
        idx, parent, tropic, segments, ax, ay, w, h = struct.unpack("<bbhhhhhh", r.read(14))
        areas.append(dict(index=idx, parent=parent, tropic_anchor=tropic, segments=segments,
                          anchor_x=ax, anchor_y=ay, width=w, height=h))
    count = struct.unpack("<i", r.read(4))[0]
    buildings = []
    for _ in range(count):
        if struct.unpack("<i", r.read(4))[0] != -102:
            raise ValueError("bad building marker (content strings are not supported)")
        index = struct.unpack("<i", r.read(4))[0]
        item, model = struct.unpack("<hh", r.read(4))
        area = struct.unpack("<b", r.read(1))[0]
        x, y, z, yaw = struct.unpack("<ffff", r.read(16))
        if 2000 < item < 2010:       # belts carry one extra float
            r.read(4)
        elif 2010 < item < 2020:     # inserters carry eight extra floats
            r.read(32)
        r.read(8 + 6 + 4)            # connection indices, slot bytes, recipe/filter
        params = struct.unpack("<h", r.read(2))[0]
        r.read(4 * params)
        content = struct.unpack("<i", r.read(4))[0]
        if content > 0:
            r.read(content)
        buildings.append(dict(index=index, item=item, model=model, area=area,
                              x=x, y=y, z=z, yaw=yaw))
    return dict(version=version, cursor=cursor, areas=areas, buildings=buildings)


def to_vec(lat_deg, lon_deg, radius):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    return (radius * math.cos(lat) * math.sin(lon),
            radius * math.sin(lat),
            radius * math.cos(lat) * math.cos(lon))


def dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--csv", help="optional output CSV")
    ap.add_argument("--json", help="optional output JSON summary")
    args = ap.parse_args()

    with open(args.path, "r", encoding="ascii", errors="replace") as f:
        header, b64 = split_blueprint(f.read())
    payload = parse_payload(gzip.decompress(base64.b64decode(b64)))

    collectors = [b for b in payload["buildings"] if b["item"] == 2105]
    if not collectors:
        sys.exit("no orbital collectors (item 2105) in this blueprint")

    z = collectors[0]["z"]
    radius = z * HEIGHT_UNIT + HEIGHT_BIAS + REAL_RADIUS
    pts = []
    for b in collectors:
        lat = b["y"] * GRID_DEG
        lon = b["x"] * GRID_DEG % 360.0
        pts.append(dict(index=b["index"], lat=lat, lon=lon,
                        pos=to_vec(lat, lon, radius)))

    rings = {}
    for p in pts:
        rings.setdefault(round(p["lat"], 3), []).append(p)

    print("file            :", args.path)
    print("header          :", header[:120])
    print("payload version :", payload["version"])
    print("areas           :", payload["areas"])
    print("buildings total :", len(payload["buildings"]), "| collectors:", len(collectors))
    print("item ids        :", sorted({b["item"] for b in payload["buildings"]}))
    print("orbit radius    : %.3f  (z offset %.3f, real radius %.1f)" % (radius, z, REAL_RADIUS))
    print("grid            : %.3f deg/cell, %d cells per circle" % (GRID_DEG, GRID_SEGMENTS * 5))
    print()
    print("rings:")
    print("  lat(deg)  count  first lon  step   circumference  neighbour")
    for lat in sorted(rings):
        ring = sorted(rings[lat], key=lambda p: p["lon"])
        lons = [p["lon"] for p in ring]
        gaps = sorted({round((b - a) % 360.0, 3) for a, b in zip(lons, lons[1:] + [lons[0] + 360.0])})
        circ = 2 * math.pi * radius * math.cos(math.radians(lat))
        nb = min(dist(ring[i]["pos"], ring[(i + 1) % len(ring)]["pos"]) for i in range(len(ring)))
        print("  %7.1f  %5d  %8.2f   %s  %12.1f  %8.2f" % (lat, len(ring), lons[0], gaps, circ, nb))

    nn = [min(dist(p["pos"], q["pos"]) for j, q in enumerate(pts) if j != i) for i, p in enumerate(pts)]
    print()
    print("nearest-neighbour distance: min %.3f  max %.3f  mean %.3f" % (min(nn), max(nn), sum(nn) / len(nn)))
    lat_list = sorted(rings)
    if len(lat_list) > 1:
        gaps = [min(dist(a["pos"], b["pos"]) for a in rings[x] for b in rings[y])
                for x, y in zip(lat_list, lat_list[1:])]
        print("ring-to-ring minimum     : %s" % ["%.2f" % g for g in gaps])

    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            f.write("index,ring_lat_deg,lon_deg,nx,ny,nz,x,y,z\n")
            for p in pts:
                nx, ny, nz = (c / radius for c in p["pos"])
                f.write("%d,%.3f,%.3f,%.6f,%.6f,%.6f,%.3f,%.3f,%.3f\n"
                        % (p["index"], p["lat"], p["lon"], nx, ny, nz, *p["pos"]))
        print("csv written:", args.csv)

    if args.json:
        summary = dict(collections=len(collectors), orbit_radius=radius, grid_deg=GRID_DEG,
                       rings=[dict(lat_deg=l, count=len(rings[l]),
                                   start_lon_deg=sorted(p["lon"] for p in rings[l])[0])
                              for l in lat_list],
                       nn_min=min(nn), nn_max=max(nn), nn_mean=sum(nn) / len(nn))
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print("json written:", args.json)


if __name__ == "__main__":
    main()
