#!/usr/bin/env python3
"""Race review: where a race was lost, leg by leg, against Typon's ORC certificate and against
competitors that transmit AIS.

    python3 tools/race_review.py --race R4          # everything from tools/regattas.json
    python3 tools/race_review.py 2026-09-19 12:00:00 12:47:06 --handicap 0.9243 \\
        --rival WOWLA=338521423:0.9039

Arguments are the local start and finish times (the finish from the results, not the log), and the
race's time-on-time handicap from the results. --rival NAME=MMSI:HANDICAP[:FINISH], repeatable;
FINISH (local HH:MM:SS, from the results) replaces the rival's last AIS-derived leg end. MMSI 0 =
no AIS: with a certificate in CERTS and a FINISH, the whole-race split still works, because the
rival's ORC time comes from our legs (same marks, same wind) and its elapsed from the results.

Per leg (a leg is a run of upwind or downwind sailing between roundings):
  * the rhumb line through the water, and its angle to the wind;
  * ORC's time for that leg: inside ORC's beat or gybe angle it tacks/gybes at the optimum
    (time = distance along the wind / ORC VMG), outside it reaches straight there at ORC's speed
    for that angle. A cross-wind leg (a race-deck finish under the Gate) is benchmarked as the reach
    it is, not as a run we sailed too high (docs/polar.md §6);
  * time lost against that benchmark, and the tacks/gybes in it;
  * for each rival: time of closest approach to our rounding positions, searched within
    ROUND_WINDOW of our own rounding (a boat can pass the same spot on another leg), so leg times
    compare directly; corrected with each boat's handicap.

Uses the calibration in build_polar.py (heading, leeway, wind at 10 m), and needs
tools/polar_out/grid.pkl (run build_polar.py once). Positions come from $GPRMC and AIS types
1/2/3/18/19; nothing here writes to the logs.
"""
import argparse, glob, io, contextlib, math, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_polar as bp
import regattas
from ais_diagnose import decode_ais   # the repo's Python AIS decoder, with the position-unavailable sentinels

LOGS = os.path.expanduser('~/Documents/typon-nmea-logs')
ROUND_WINDOW = pd.Timedelta(minutes=10)

# Rival ORC certificates ("Rated boat velocities in knots"), keyed by the --rival NAME. Both J/100s
# fly asymmetric spinnakers: best downwind angle is 144-156 deg, not Typon's 168-178.
_W = [4, 6, 8, 10, 12, 14, 16, 20, 24]
CERTS = {
    'WOWLA': bp.Cert(_W,  # USA 14, J/100, ORC 2026
        [46.5, 44.2, 41.9, 40.0, 38.3, 37.7, 37.4, 37.8, 39.0],
        [2.28, 3.31, 4.11, 4.68, 4.99, 5.12, 5.19, 5.24, 5.17],
        [[3.58, 5.08, 6.13, 6.75, 7.05, 7.17, 7.25, 7.33, 7.32],
         [3.84, 5.37, 6.39, 6.94, 7.22, 7.38, 7.47, 7.57, 7.59],
         [4.02, 5.53, 6.55, 7.08, 7.40, 7.65, 7.83, 8.04, 8.14],
         [4.07, 5.76, 6.81, 7.23, 7.47, 7.76, 8.07, 8.51, 8.78],
         [4.18, 5.91, 7.02, 7.58, 8.01, 8.34, 8.64, 9.22, 9.78],
         [4.05, 5.77, 6.93, 7.55, 8.09, 8.61, 9.10, 9.93, 10.75],
         [3.57, 5.16, 6.42, 7.20, 7.76, 8.35, 9.04, 10.82, 12.68],
         [2.96, 4.33, 5.50, 6.49, 7.17, 7.64, 8.12, 9.45, 11.85]],
        [2.56, 3.75, 4.76, 5.62, 6.24, 6.66, 7.05, 8.18, 10.26],
        [139.4, 142.4, 146.1, 149.4, 152.8, 155.7, 153.9, 144.0, 143.5]),
    'FEATHER': bp.Cert(_W,  # USA 23, J/100, ORC 2026
        [46.6, 44.1, 41.6, 39.8, 38.6, 37.9, 37.7, 37.7, 38.8],
        [2.31, 3.35, 4.14, 4.69, 4.97, 5.09, 5.16, 5.21, 5.12],
        [[3.63, 5.13, 6.16, 6.76, 7.03, 7.15, 7.22, 7.30, 7.29],
         [3.90, 5.42, 6.41, 6.95, 7.21, 7.35, 7.44, 7.54, 7.56],
         [4.08, 5.60, 6.57, 7.08, 7.40, 7.63, 7.79, 7.99, 8.09],
         [4.09, 5.76, 6.79, 7.19, 7.44, 7.76, 8.04, 8.46, 8.72],
         [4.21, 5.95, 7.02, 7.56, 7.97, 8.26, 8.54, 9.09, 9.62],
         [4.08, 5.82, 6.96, 7.56, 8.08, 8.56, 8.98, 9.77, 10.56],
         [3.60, 5.19, 6.46, 7.24, 7.79, 8.38, 9.05, 10.80, 12.41],
         [2.98, 4.35, 5.52, 6.51, 7.21, 7.68, 8.17, 9.52, 11.91]],
        [2.58, 3.77, 4.78, 5.64, 6.27, 6.69, 7.09, 8.25, 10.32],
        [139.4, 142.4, 146.2, 149.2, 152.9, 155.7, 153.2, 144.4, 144.2]),
    'FINAL FINAL': bp.Cert(_W,  # USA 38301, Beneteau First 30, ORC 2026
        [45.6, 43.3, 40.9, 39.5, 41.0, 40.7, 40.0, 39.7, 41.5],
        [2.29, 3.28, 3.99, 4.49, 4.78, 4.92, 5.00, 5.04, 4.91],
        [[3.59, 4.99, 5.90, 6.60, 6.90, 7.03, 7.11, 7.18, 7.13],
         [3.84, 5.26, 6.14, 6.80, 7.10, 7.26, 7.35, 7.45, 7.41],
         [4.01, 5.43, 6.39, 6.95, 7.31, 7.57, 7.76, 7.96, 8.03],
         [4.03, 5.59, 6.67, 7.11, 7.35, 7.69, 8.01, 8.47, 8.72],
         [4.13, 5.79, 6.91, 7.50, 7.97, 8.29, 8.60, 9.18, 9.47],
         [4.02, 5.67, 6.84, 7.48, 8.06, 8.62, 9.09, 9.95, 10.77],
         [3.55, 5.06, 6.30, 7.15, 7.75, 8.38, 9.12, 11.04, 13.13],
         [2.94, 4.27, 5.40, 6.42, 7.13, 7.63, 8.16, 9.67, 12.83]],
        [2.55, 3.70, 4.67, 5.56, 6.19, 6.62, 7.07, 8.38, 11.11],
        [140.3, 143.8, 147.6, 148.0, 152.3, 152.5, 150.5, 145.0, 144.1]),
    'JARLEN': bp.Cert(_W,  # USA 28528, J/35, ORC 2026 (symmetric spinnaker)
        [45.3, 42.3, 40.2, 37.6, 36.3, 35.6, 35.3, 35.2, 35.7],
        [2.72, 3.78, 4.55, 5.05, 5.28, 5.39, 5.45, 5.51, 5.49],
        [[4.25, 5.71, 6.59, 7.01, 7.21, 7.30, 7.35, 7.43, 7.45],
         [4.56, 5.98, 6.77, 7.14, 7.34, 7.46, 7.52, 7.63, 7.67],
         [4.77, 6.14, 6.87, 7.24, 7.49, 7.68, 7.81, 8.00, 8.13],
         [4.63, 6.01, 6.82, 7.23, 7.53, 7.80, 8.03, 8.40, 8.64],
         [4.02, 5.62, 6.72, 7.27, 7.65, 7.98, 8.28, 8.71, 8.98],
         [3.89, 5.46, 6.57, 7.19, 7.62, 8.03, 8.43, 9.17, 9.72],
         [3.41, 4.92, 6.08, 6.88, 7.36, 7.79, 8.23, 9.25, 10.76],
         [2.85, 4.17, 5.31, 6.28, 6.96, 7.39, 7.78, 8.65, 9.79]],
        [2.46, 3.61, 4.60, 5.45, 6.16, 6.72, 7.15, 7.88, 8.69],
        [140.4, 145.3, 149.6, 152.8, 159.4, 166.4, 172.7, 175.0, 166.7]),
    'FREQUENT FLYER': bp.Cert(_W,  # USA 52, Farr 30, ORC 2026 (US5230); symmetric spinnaker
        [44.9, 42.1, 39.5, 37.2, 36.7, 36.4, 36.2, 36.3, 38.0],
        [2.73, 3.85, 4.62, 4.99, 5.15, 5.24, 5.30, 5.33, 5.21],
        [[4.25, 5.81, 6.63, 6.98, 7.14, 7.24, 7.32, 7.42, 7.40],
         [4.54, 6.09, 6.81, 7.16, 7.36, 7.50, 7.60, 7.74, 7.76],
         [4.76, 6.26, 6.94, 7.35, 7.69, 7.91, 8.08, 8.35, 8.47],
         [4.62, 6.13, 6.91, 7.38, 7.82, 8.22, 8.56, 9.09, 9.46],
         [4.49, 6.21, 7.08, 7.61, 7.98, 8.32, 8.66, 9.69, 10.68],
         [4.33, 6.07, 7.04, 7.66, 8.24, 8.74, 9.21, 10.14, 11.03],
         [3.83, 5.47, 6.65, 7.33, 8.03, 8.86, 9.80, 11.56, 13.08],
         [3.17, 4.62, 5.80, 6.73, 7.37, 8.02, 8.81, 11.03, 14.32]],
        [2.75, 4.00, 5.02, 5.87, 6.53, 7.08, 7.66, 9.55, 12.40],
        [140.2, 143.3, 147.6, 154.3, 160.9, 163.3, 156.8, 147.4, 149.5]),
    'TANGAROA': bp.Cert(_W,  # J/109, ORC 2026 (LOA 10.76)
        [46.1, 43.5, 41.4, 39.6, 37.7, 36.8, 36.7, 36.7, 37.5],
        [2.39, 3.42, 4.19, 4.76, 5.12, 5.28, 5.37, 5.43, 5.41],
        [[3.76, 5.22, 6.22, 6.83, 7.15, 7.31, 7.38, 7.47, 7.49],
         [4.05, 5.51, 6.48, 7.01, 7.31, 7.48, 7.57, 7.68, 7.70],
         [4.26, 5.70, 6.64, 7.15, 7.46, 7.68, 7.85, 8.03, 8.13],
         [4.20, 5.87, 6.91, 7.36, 7.57, 7.74, 7.99, 8.38, 8.63],
         [4.32, 6.02, 7.09, 7.61, 7.98, 8.28, 8.51, 8.95, 9.34],
         [4.20, 5.90, 7.00, 7.57, 8.00, 8.42, 8.82, 9.45, 10.06],
         [3.72, 5.28, 6.52, 7.26, 7.73, 8.20, 8.69, 9.90, 11.82],
         [3.08, 4.46, 5.61, 6.58, 7.24, 7.64, 8.02, 8.86, 10.45]],
        [2.67, 3.86, 4.86, 5.70, 6.31, 6.72, 7.08, 7.74, 9.05],
        [139.7, 143.3, 147.5, 149.6, 153.2, 157.9, 161.5, 159.3, 142.0]),
}


def read_positions(day, mmsis):
    """Own track ($GPRMC, 1 Hz) and AIS positions (with reported SOG/COG and heading) for the given MMSIs
    (None = every vessel heard), on one log day."""
    own, ais = [], []
    for f in sorted(glob.glob(os.path.join(LOGS, f'nmea_{day}*.txt'))):
        for line in open(f, errors='replace'):
            s = line[26:].strip()
            if not (s.startswith('$GPRMC') or s.startswith('!AIVDM')):
                continue
            try:
                t = pd.Timestamp(line[:23]).value / 1e9
            except ValueError:
                continue
            p = s.split('*')[0].split(',')
            if s.startswith('$GPRMC'):
                if len(p) > 6 and p[2] == 'A' and p[3] and p[5]:
                    lat = int(p[3][:2]) + float(p[3][2:]) / 60
                    lon = int(p[5][:3]) + float(p[5][3:]) / 60
                    own.append((t, -lat if p[4] == 'S' else lat, -lon if p[6] == 'W' else lon))
                continue
            if len(p) < 6 or p[1] != '1':      # positions are single-fragment
                continue
            msg = decode_ais(p[5])
            if (mmsis is None or msg['mmsi'] in mmsis) and msg['lat'] is not None and msg['lon'] is not None:
                ais.append((t, msg['mmsi'], msg['lat'], msg['lon'], msg['sog'], msg['cog'], msg.get('hdg')))
    O = pd.DataFrame(own, columns=['t', 'lat', 'lon'])
    O = O.groupby(np.floor(O.t).astype(np.int64)).mean()
    return O, pd.DataFrame(ais, columns=['t', 'mmsi', 'lat', 'lon', 'sog', 'cog', 'hdg'])   # hdg: true heading, None if not sent


def _nm(lat1, lon1, lat2, lon2):
    return np.hypot((lat1 - lat2) * 60, (lon1 - lon2) * 60 * math.cos(math.radians(lat1 if np.isscalar(lat1) else 37.8)))


def split_legs(x):
    """Runs of upwind / downwind sailing (60 s-plus), with roundings folded into the leg before."""
    up = (np.cos(np.deg2rad(x.twa)) > 0).astype(float).rolling(91, center=True, min_periods=30).median()
    seg = (up.diff().abs() > 0).cumsum()
    legs = []
    for _, s in x.groupby(seg.values):
        if len(s) < 120 and legs:
            legs[-1] = pd.concat([legs[-1], s])
        elif len(s) >= 120:
            legs.append(s)
    return legs


def count_turns(s):
    """Tacks (upwind leg) or gybes (downwind): changes of side in the 21 s median TWA."""
    side = np.sign(s.twa.rolling(21, center=True, min_periods=10).median()).replace(0, np.nan).dropna()
    return int((side.diff().abs() == 2).sum())


def orc_leg_time(dist, alpha, tws, c=bp.TYPON):
    bv, ba = bp.orc_beat(tws, c)
    rv, ra = bp.orc_run(tws, c)
    if alpha <= ba:
        return dist * bp.cosd(alpha) / bv, 'beat'
    if alpha >= ra:
        return dist * -bp.cosd(alpha) / rv, 'run'
    return dist / bp.orc_speed(alpha, tws, c), f'reach {alpha:.0f}°'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('day', nargs='?'); ap.add_argument('start', nargs='?'); ap.add_argument('finish', nargs='?')
    ap.add_argument('--race', help='take day/start/finish/handicap/rivals from tools/regattas.json, e.g. R5')
    ap.add_argument('--regatta', help='regatta id in regattas.json, when a race id is in more than one')
    ap.add_argument('--handicap', type=float, help="Typon's time-on-time handicap for this race")
    ap.add_argument('--rival', action='append', default=[], help='NAME=MMSI:HANDICAP[:FINISH] (adds to / overrides the file)')
    args = ap.parse_args()
    rivals = {}
    if args.race:
        _, race = regattas.find_race(args.race, args.regatta)
        args.day, args.start, args.finish = race['day'], race['start'], race['finish']
        args.handicap = args.handicap or race['handicap']
        for name, rv in race.get('rivals', {}).items():
            if rv.get('handicap') is not None:          # no handicap: nothing to correct with
                rivals[name.upper()] = (int(rv['mmsi']), float(rv['handicap']), rv.get('finish'))
    if not (args.day and args.start and args.finish and args.handicap):
        ap.error('give DAY START FINISH --handicap H, or --race ID')
    for r in args.rival:
        name, rest = r.split('=')
        mmsi, hc, *fin = rest.split(':', 2)
        rivals[name] = (int(mmsi), float(hc), fin[0] if fin else None)

    g = bp.load_grid()
    with contextlib.redirect_stdout(io.StringIO()):
        g, _ = bp.calibrate(g)
    g['twd'] = (g.hdg + g.twa_bow) % 360
    g['crs_w'] = (g.hdg - np.sign(g.twa) * g.lee) % 360
    t0, t1 = (pd.Timestamp(f'{args.day} {x}') for x in (args.start, args.finish))
    x = g[(g.local >= t0) & (g.local <= t1)].dropna(subset=['stw', 'crs_w', 'twa', 'tws'])
    O, A = read_positions(args.day, {v[0] for v in rivals.values()} - {0})
    O['local'] = regattas.utc_s_to_local(O.t)
    A['local'] = regattas.utc_s_to_local(A.t)
    A = A[(A.local >= t0 - pd.Timedelta(minutes=10)) & (A.local <= t1 + pd.Timedelta(minutes=30))]

    rows, marks = [], []
    for i, s in enumerate(split_legs(x)):
        Dw = (s.stw * np.exp(1j * np.deg2rad(90 - s.crs_w))).sum() / 3600
        twd, tws = bp._cmean(s.twd), s.tws.median()
        alpha = abs(bp.signed((90 - np.rad2deg(np.angle(Dw))) % 360 - twd))
        ideal, how = orc_leg_time(abs(Dw), alpha, tws)
        actual = len(s) / 3600
        end = s.index[-1]
        if end in O.index:
            marks.append((end, O.loc[end, 'lat'], O.loc[end, 'lon']))
        rows.append(dict(leg=i + 1, type='up' if bp.cosd(s.twa.abs().median()) > 0 else 'down',
                         start=s.local.iloc[0].strftime('%H:%M'), min=actual * 60, tws=tws, rhumb=alpha,
                         benchmark=how, nm=abs(Dw), orc_min=ideal * 60, lost_min=(actual - ideal) * 60,
                         pct=ideal / actual * 100, turns=count_turns(s)))
    L = pd.DataFrame(rows)
    # Sea state per leg from our own pitch (tools/sea_state.py; docs/polar.md §8). Imported here, not
    # at the top: sea_state imports race_tracks, which imports this module.
    import sea_state as ss
    if os.path.exists(ss.CACHE):
        P = pd.read_pickle(ss.CACHE)
        st = [ss.leg_state(P, s.index[0], s.index[-1]) for s in split_legs(x)]
        L['pitch'] = [a for a, _ in st]
        L['period_s'] = [b for _, b in st]
    pd.set_option('display.width', 220)
    print(f'Typon {args.day} {args.start}-{args.finish}, wind at 10 m\n' + L.round(1).to_string(index=False))
    print(f"\ntotal: sailed {L['min'].sum():.1f} min, ORC {L.orc_min.sum():.1f} min -> {L.orc_min.sum() / L['min'].sum() * 100:.0f}% of ORC; "
          f"lost {L.lost_min.sum():.1f} min (upwind {L[L.type == 'up'].lost_min.sum():.1f}, downwind {L[L.type == 'down'].lost_min.sum():.1f})")

    if not rivals:
        return
    # leg boundaries: start, our roundings (all but the last leg end), the finish
    ours = [t0] + [regattas.utc_s_to_local(t) for t, _, _ in marks[:-1]] + [t1]
    print('\nleg times, elapsed (corrected = elapsed x handicap); rival rounding = its closest approach to our rounding point')
    print('\nwhole race, each boat against its own certificate over our legs (needs CERTS entry + FINISH):')
    ideal_t = L.orc_min.sum() * 60
    act_t = (t1 - t0).total_seconds()
    for name, (mmsi, hc, fin) in rivals.items():
        cert = CERTS.get(name.upper())
        if cert is None or not fin:
            continue
        ideal_r = sum(orc_leg_time(r.nm, r.rhumb, r.tws, cert)[0] for _, r in L.iterrows()) * 3600
        act_r = (pd.Timestamp(f'{args.day} {fin}') - t0).total_seconds()
        delta = (act_t * args.handicap - act_r * hc) / 60
        rating = (ideal_t * args.handicap - ideal_r * hc) / 60
        print(f'  {name:15s} Typon {ideal_t / act_t * 100:.0f}% of own polar, {name} {ideal_r / act_r * 100:.0f}%; '
              f'corrected gap {delta:+.1f} min = rating {rating:+.1f} + sailing {delta - rating:+.1f}  (+ = rival ahead)')
    for name, (mmsi, hc, fin) in rivals.items():
        r = A[A.mmsi == mmsi].sort_values('t')
        if r.empty:
            if mmsi:
                print(f'  {name}: no AIS positions in the race window')
            continue
        times, prev = [t0], t0
        for j, (t, lat, lon) in enumerate(marks[:-1]):
            ours_t = ours[j + 1]
            c = r[(r.local > prev) & (r.local >= ours_t - ROUND_WINDOW) & (r.local <= ours_t + ROUND_WINDOW)]
            if c.empty:
                print(f'  {name} rounding {len(times)}: no AIS position within {ROUND_WINDOW} of ours; '
                      'the legs either side are merged below')
                break
            d = _nm(c.lat.values, c.lon.values, lat, lon)
            k = int(np.argmin(d))
            times.append(c.local.iloc[k]); prev = c.local.iloc[k]
            print(f'  {name} rounding {len(times) - 1}: {c.local.iloc[k].strftime("%H:%M:%S")} at {d[k] * 1852:.0f} m from our rounding point'
                  f' (ours {ours_t.strftime("%H:%M:%S")})')
        if fin:
            times.append(pd.Timestamp(f'{args.day} {fin}'))
        cert = CERTS.get(name.upper())
        merged = len(times) - 1 < len(L)
        tot = [0.0, 0.0]
        for j in range(len(times) - 1):
            ours_s = (ours[j + 1] - ours[j]).total_seconds()
            theirs = (times[j + 1] - times[j]).total_seconds()
            delta = (ours_s * args.handicap - theirs * hc) / 60
            line = (f'    leg {j + 1} ({L.type.iloc[j] if j < len(L) else "?"}): Typon {ours_s / 60:.1f} min (corr {ours_s * args.handicap / 60:.1f}), '
                    f'{name} {theirs / 60:.1f} min (corr {theirs * hc / 60:.1f}) -> Typon {delta:+.1f} min corrected')
            if cert is not None and not (merged and j == len(times) - 2):
                r = L.iloc[j]
                ideal_t = r.orc_min * 60
                ideal_r, how = orc_leg_time(r.nm, r.rhumb, r.tws, cert)
                ideal_r *= 3600
                rating = (ideal_t * args.handicap - ideal_r * hc) / 60     # the gap if both sail exactly at polar
                line += (f' | vs own polar: Typon {ideal_t / ours_s * 100:.0f}%, {name} {ideal_r / theirs * 100:.0f}% ({how}); '
                         f'rating {rating:+.1f}, sailing {delta - rating:+.1f}')
                tot[0] += rating; tot[1] += delta - rating
            print(line)
        if cert is not None:
            print(f'    split over the timed legs: rating {tot[0]:+.1f} min, sailing {tot[1]:+.1f} min '
                  '(+ = favours the rival; rating = corrected gap if both boats sailed exactly at their own polar, '
                  "same rhumb line and our wind)")


if __name__ == '__main__':
    main()
