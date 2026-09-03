#!/usr/bin/env python3
"""Convert SI-TEX MDA-5 VDR logs into map-ready GeoJSON and archival GPX.

The MDA-5 writes its internal voyage recorder to a microSD card as
``VDR_YYMM/YYYYMMDD.VDR``: a headerless stream of fixed 20-byte little-endian
records, one GPS fix every 10 s.

    offset  type    field
    0       uint16  low word of the timestamp (repeated, ignored)
    2       uint32  Unix epoch seconds, UTC
    6       int32   latitude,  units of 1/600000 degree (AIS convention)
    10      int32   longitude, units of 1/600000 degree
    14      uint16  COG, tenths of a degree (3600 = not available)
    16      uint16  SOG, tenths of a knot
    18      uint16  high word of the timestamp (repeated, ignored)

Two caveats the data itself forces on us, both documented in the MDA-5 fault
notes:

* Fixes logged between 2026-07-26 and the card's last write carry a GPS
  week-number rollover — the epoch is exactly 1024 weeks early while the
  time-of-day and position are correct. ``--fix-rollover`` corrects those.
* A day file can span more than one voyage. Tracks are split whenever the gap
  between consecutive fixes exceeds ``--gap`` minutes, so a week at the dock
  does not draw a straight line across the Bay.

Usage:
    python3 tools/vdr_to_tracks.py SRC --geojson DIR --gpx DIR
"""

import argparse
import datetime as dt
import glob
import json
import math
import os
import struct
import sys
from collections import defaultdict

RECORD = struct.Struct("<HIiiHHH")
RECORD_SIZE = RECORD.size  # 20
COORD_SCALE = 600000.0
COG_UNAVAILABLE = 3600
ROLLOVER = dt.timedelta(weeks=1024)
# The receiver's firmware epoch expired on this date; anything stamped before it
# is a rolled-back week number, not a real 2006 voyage.
ROLLOVER_CUTOFF = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


def read_fixes(path, fix_rollover=True):
    """Yield (time, lat, lon, cog_or_None, sog) tuples from one .VDR file."""
    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) % RECORD_SIZE:
        print(f"  warn: {path} has {len(blob) % RECORD_SIZE} trailing bytes", file=sys.stderr)
    out = []
    for off in range(0, len(blob) - RECORD_SIZE + 1, RECORD_SIZE):
        _, ts, lat, lon, cog, sog, _ = RECORD.unpack_from(blob, off)
        when = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        if fix_rollover and when < ROLLOVER_CUTOFF:
            when += ROLLOVER
        out.append((when, lat / COORD_SCALE, lon / COORD_SCALE,
                    None if cog == COG_UNAVAILABLE else cog / 10.0, sog / 10.0))
    return out


def split_on_gaps(fixes, gap_minutes):
    """Break a fix list into separate tracks wherever time jumps."""
    gap = dt.timedelta(minutes=gap_minutes)
    segments, current = [], []
    for fix in fixes:
        if current and fix[0] - current[-1][0] > gap:
            segments.append(current)
            current = []
        current.append(fix)
    if current:
        segments.append(current)
    return segments


def simplify(points, epsilon_m):
    """Douglas-Peucker on an equirectangular projection about the track's mean.

    Iterative rather than recursive: a full-resolution day is ~4000 points and
    a degenerate near-straight leg would otherwise blow the stack.
    """
    if epsilon_m <= 0 or len(points) < 3:
        return points
    m_per_deg = 111320.0
    cos_lat = math.cos(math.radians(points[len(points) // 2][1]))

    def xy(p):
        return p[2] * m_per_deg * cos_lat, p[1] * m_per_deg

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        x1, y1 = xy(points[first])
        x2, y2 = xy(points[last])
        dx, dy = x2 - x1, y2 - y1
        span = math.hypot(dx, dy)
        worst, worst_i = 0.0, -1
        for i in range(first + 1, last):
            x, y = xy(points[i])
            if span < 1e-9:
                dist = math.hypot(x - x1, y - y1)
            else:
                dist = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / span
            if dist > worst:
                worst, worst_i = dist, i
        if worst > epsilon_m:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    return [p for p, k in zip(points, keep) if k]


def distance_nm(points):
    total = 0.0
    for a, b in zip(points, points[1:]):
        lat1, lon1, lat2, lon2 = map(math.radians, (a[1], a[2], b[1], b[2]))
        h = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
        total += 2 * 3440.065 * math.asin(min(1.0, math.sqrt(h)))
    return total


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_geojson(path, segments, epsilon_m, precision, min_nm=0.0):
    features, kept, raw, dropped = [], 0, 0, 0
    for seg in segments:
        raw += len(seg)
        thin = simplify(seg, epsilon_m)
        # A one-point LineString is invalid GeoJSON (RFC 7946 needs two
        # positions) and Leaflet silently draws nothing for it. Simplification
        # can also collapse a track that never left the slip.
        if len(thin) < 2:
            dropped += 1
            continue
        nm = distance_nm(thin)
        if nm < min_nm:
            dropped += 1
            continue
        kept += len(thin)
        speeds = [p[4] for p in thin]
        features.append({
            "type": "Feature",
            "properties": {
                "date": seg[0][0].strftime("%Y-%m-%d"),
                "start": iso(seg[0][0]),
                "end": iso(seg[-1][0]),
                "hours": round((seg[-1][0] - seg[0][0]).total_seconds() / 3600, 2),
                "distance_nm": round(nm, 2),
                "max_sog": max(speeds),
                "fixes": len(seg),
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(p[2], precision), round(p[1], precision)] for p in thin],
            },
        })
    doc = {"type": "FeatureCollection", "features": features}
    with open(path, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    return kept, raw, dropped


def write_gpx(path, segments, name):
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    with open(path, "w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<gpx version="1.1" creator="vdr_to_tracks.py" '
                 'xmlns="http://www.topografix.com/GPX/1/1">\n')
        fh.write(f"  <metadata><name>{esc(name)}</name></metadata>\n")
        for seg in segments:
            fh.write(f"  <trk>\n    <name>{esc(seg[0][0].strftime('%Y-%m-%d %H:%M'))}</name>\n"
                     "    <trkseg>\n")
            for when, lat, lon, cog, sog in seg:
                fh.write(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">'
                         f"<time>{iso(when)}</time>")
                if cog is not None:
                    fh.write(f"<course>{cog:.1f}</course>")
                fh.write(f"<speed>{sog * 0.514444:.2f}</speed></trkpt>\n")
            fh.write("    </trkseg>\n  </trk>\n")
        fh.write("</gpx>\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="directory holding VDR_YYMM/ subdirectories")
    ap.add_argument("--geojson", help="output directory for simplified per-year GeoJSON")
    ap.add_argument("--js", metavar="FILE",
                    help="also write the GeoJSON as a JS assignment to window.VDR_TRACKS, "
                         "so static/tracks.html works from a file:// URL where fetch() "
                         "would be blocked by CORS")
    ap.add_argument("--gpx", help="output directory for full-resolution per-year GPX")
    ap.add_argument("--epsilon", type=float, default=10.0,
                    help="Douglas-Peucker tolerance in metres for GeoJSON (default 10)")
    ap.add_argument("--precision", type=int, default=5,
                    help="decimal places in GeoJSON coordinates (default 5, ~1.1 m)")
    ap.add_argument("--gap", type=float, default=30.0,
                    help="minutes of silence that starts a new track (default 30)")
    ap.add_argument("--min-distance", type=float, default=0.05,
                    help="drop GeoJSON tracks shorter than this many nm (default 0.05); "
                         "the GPX archive always keeps everything")
    ap.add_argument("--no-fix-rollover", action="store_true",
                    help="keep the raw GPS week-rollover timestamps")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.source, "VDR_*", "*.VDR")))
    if not files:
        sys.exit(f"no VDR files under {args.source}")

    # Pool every fix before splitting. A voyage is not a file: an overnight
    # passage spans two day files, and the week-rollover fixes live in a folder
    # 20 years away from the track they continue. Splitting per file would cut
    # both. Sorting by time and splitting on real gaps recovers the voyages.
    all_fixes = []
    for path in files:
        all_fixes.extend(read_fixes(path, fix_rollover=not args.no_fix_rollover))
    all_fixes.sort(key=lambda f: f[0])
    deduped = [f for i, f in enumerate(all_fixes)
               if i == 0 or f[0] != all_fixes[i - 1][0]]
    total_fixes = len(deduped)
    if len(all_fixes) != total_fixes:
        print(f"dropped {len(all_fixes) - total_fixes} duplicate-timestamp fixes")

    by_year = defaultdict(list)
    for seg in split_on_gaps(deduped, args.gap):
        by_year[seg[0][0].year].append(seg)

    print(f"{len(files)} files, {total_fixes} fixes, "
          f"{sum(len(v) for v in by_year.values())} tracks, "
          f"{len(by_year)} years")

    index = []
    for year in sorted(by_year):
        segments = sorted(by_year[year], key=lambda s: s[0][0])
        nm = round(sum(distance_nm(s) for s in segments), 1)
        entry = {"year": year, "tracks": len(segments),
                 "fixes": sum(len(s) for s in segments), "distance_nm": nm}
        if args.geojson:
            out = os.path.join(args.geojson, f"{year}.geojson")
            kept, raw, dropped = write_geojson(out, segments, args.epsilon,
                                               args.precision, args.min_distance)
            entry["file"] = f"{year}.geojson"
            entry["tracks"] = len(segments) - dropped
            entry["points"] = kept
            entry["bytes"] = os.path.getsize(out)
            print(f"  {year}: {len(segments) - dropped:3} tracks  {raw:6} fixes -> {kept:6} pts  "
                  f"{nm:8.1f} nm  {entry['bytes'] / 1024:7.1f} KB"
                  + (f"  ({dropped} moored/short dropped)" if dropped else ""))
        if args.gpx:
            write_gpx(os.path.join(args.gpx, f"{year}.gpx"), segments, f"Typon {year}")
        index.append(entry)

    if args.geojson:
        with open(os.path.join(args.geojson, "index.json"), "w") as fh:
            json.dump({"source": "SI-TEX MDA-5 internal VDR",
                       "epsilon_m": args.epsilon,
                       "years": index}, fh, indent=2)

    if args.js:
        if not args.geojson:
            sys.exit("--js needs --geojson (it re-reads what was just written)")
        payload = {str(e["year"]): json.load(open(os.path.join(args.geojson, e["file"])))
                   for e in index}
        with open(args.js, "w") as fh:
            fh.write("// Generated by tools/vdr_to_tracks.py -- do not edit.\n")
            fh.write("window.VDR_TRACKS = ")
            json.dump(payload, fh, separators=(",", ":"))
            fh.write(";\n")
        print(f"  wrote {args.js} ({os.path.getsize(args.js) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
