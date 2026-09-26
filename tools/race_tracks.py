#!/usr/bin/env python3
"""Race tracks scored against ORC, for the replay's race picker (static/races/<series>.json).

    python3 tools/race_tracks.py            # every regatta in tools/regattas.json; needs grid.pkl (build_polar.py)
    python3 tools/race_tracks.py --regatta bbs2026

Every point on a boat's track carries its % of that boat's own ORC certificate, scored the way
docs/polar.md does it:
  * upwind (TWA <= 55): VMG / ORC beat VMG;
  * downwind (TWA >= 130): VMG / ORC run VMG;
  * in between: speed / ORC speed at that angle, so a forced reach (a race-deck finish under the
    Gate) is not scored against a run it could not sail.

Typon: the full build_polar.py calibration (compass deviation, paddlewheel k, vane heel, leeway,
masthead offset + upwash, TWA through the water, wind at 10 m). 30 s rolling median; the turn of
a tack or gybe (TURN_BLANK_S around the side change) is left unscored, the acceleration after it is
not.

Rivals: AIS reports (~30 s apart), reported SOG/COG minus the current Typon measured at that
moment (GPS minus through-water, 5 min mean), against Typon's wind (2 min mean). So a rival's %
carries two assumptions a boat's own instruments would not: the same current and the same wind as
us. A report either side of a tack or gybe is left unscored.
"""
import contextlib, io, json, math, os, shutil, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_polar as bp
import race_review as rr
import regattas

RACES_DIR = os.path.join(bp.REPO, 'static', 'races')   # <regatta>.json + index.json, read by replay's race picker
STEP_S = 5                  # own track resolution in the file
OWN_SMOOTH_S = 31
CURRENT_SMOOTH_S = 300
WIND_SMOOTH_S = 121
MIN_TWS = 4.0               # the certificate's lightest column; below it a % means nothing
# Unscored around a tack or gybe: only the turn itself, while the TWA swings through the wind and a
# VMG % means nothing. The slow build back to speed after it IS scored, so a slow tack shows red.
TURN_BLANK_S = (5, 15)
# AIS positions carry corrupt fixes (Wowla R1/R5: longitude -1.4, off Africa). A rival report
# further than this from us, or implying a faster jump from its previous kept report, is dropped.
AIS_MAX_FROM_US_NM = 10
AIS_MAX_JUMP_KN = 25
OWN_MMSI = 338361814
# The rest of the fleet, for the crew race page (static/race.html): unscored grey boats. A vessel is
# kept if it came within FLEET_RADIUS_NM of us while moving; positions are thinned to FLEET_STEP_S.
FLEET_RADIUS_NM = 3.0
FLEET_MIN_SOG = 1.0         # median kn while near us: drops boats on moorings and in marinas
FLEET_STEP_S = 30
REVIEWS_SRC = os.path.join(bp.REPO, 'docs', 'reviews')     # <race id>.md, e.g. BBS2026-R5.md
REVIEWS_OUT = os.path.join(bp.REPO, 'static', 'reviews')   # copied for GitHub Pages, which serves static/ only

def race_list(rid=None):
    """Races from tools/regattas.json, with the rivals we can draw: a transmitting MMSI and a
    certificate in race_review.CERTS. finish None = not transcribed: the rival's closest AIS pass to
    our finish point is used instead (finish_from_ais)."""
    out = []
    for g, r in regattas.races(rid):
        ais = {n: (int(v['mmsi']), v.get('finish')) for n, v in r.get('rivals', {}).items()
               if v.get('mmsi') and n.upper() in rr.CERTS}
        out.append(dict(regatta=g, race=r, id=f"{g['id'].upper()}-{r['id']}", day=r['day'],
                        start=r['start'], finish=r['finish'], rivals=ais,
                        label=f"{g['name']} – {r['id']} ({r['label']})"))
    return out


def load_tracks():
    """Every race in static/races/*.json that index.json lists (what the replay picker shows)."""
    with open(os.path.join(RACES_DIR, 'index.json')) as f:
        idx = json.load(f)
    races = []
    for e in idx['regattas']:
        with open(os.path.join(RACES_DIR, e['file'])) as f:
            races += json.load(f)['races']
    return races


def score(spd, twa, tws, cert):
    """(% of polar, mode) for one moment; twa unsigned, through the water."""
    if not (np.isfinite(spd) and np.isfinite(twa) and np.isfinite(tws)) or tws < MIN_TWS:
        return np.nan, ''
    if twa <= bp.UP_MAX_TWA:
        return spd * bp.cosd(twa) / bp.orc_beat(tws, cert)[0], 'u'
    if twa >= bp.DOWN_MIN_TWA:
        return spd * -bp.cosd(twa) / bp.orc_run(tws, cert)[0], 'd'
    return spd / bp.orc_speed(twa, tws, cert), 'r'


def target(twa, tws, mode, cert):
    """ORC target (angle, speed) for a moment: upwind the beat angle and the speed there, downwind the
    gybe angle and its speed; on a reach the course sets the angle, so (None, speed at the angle sailed)."""
    if mode == 'u':
        v, a = bp.orc_beat(tws, cert)
        return a, v / bp.cosd(a)
    if mode == 'd':
        v, a = bp.orc_run(tws, cert)
        return a, v / -bp.cosd(a)
    if mode == 'r':
        return None, bp.orc_speed(twa, tws, cert)
    return None, None


def _row(t, lat, lon, pct, twa, tws, spd, mode, cert):
    """One output point, FIELDS order; NaN -> null."""
    ok = lambda v: v is not None and np.isfinite(v)
    ta, ts = target(twa, tws, mode, cert) if ok(twa) and ok(tws) else (None, None)
    return [int(t), round(lat, 5), round(lon, 5),
            round(pct * 100) if ok(pct) else None,
            round(twa) if ok(twa) else None,
            round(tws, 1) if ok(tws) else None,
            round(spd, 2) if ok(spd) else None, mode,
            round(ta, 1) if ok(ta) else None,
            round(ts, 2) if ok(ts) else None]


FIELDS = ['t', 'lat', 'lon', 'pct', 'twa', 'tws', 'spd', 'mode', 'tgt_twa', 'tgt_spd']


def _utc_s(day, hms):
    return regattas.local_to_utc_s(day, hms)


def _circ_roll(deg, n):
    r = np.deg2rad(deg)
    s = pd.Series(np.sin(r), index=deg.index).rolling(n, center=True, min_periods=n // 3).mean()
    c = pd.Series(np.cos(r), index=deg.index).rolling(n, center=True, min_periods=n // 3).mean()
    return np.rad2deg(np.arctan2(s, c)) % 360


def own_frame(g):
    """Per-second score inputs on the calibrated grid, plus the measured current and smoothed wind
    that rivals are scored against."""
    g = g.copy()
    g['twd'] = (g.hdg + g.twa_bow) % 360
    g['crs_w'] = (g.hdg - np.sign(g.twa) * g.lee) % 360
    w = np.deg2rad(g.crs_w)
    gr = np.deg2rad(g.cog)
    g['cur_e'] = (g.sog * np.sin(gr) - g.stw * np.sin(w)).rolling(CURRENT_SMOOTH_S, center=True, min_periods=60).mean()
    g['cur_n'] = (g.sog * np.cos(gr) - g.stw * np.cos(w)).rolling(CURRENT_SMOOTH_S, center=True, min_periods=60).mean()
    g['twd_s'] = _circ_roll(g.twd.dropna(), WIND_SMOOTH_S).reindex(g.index)
    g['tws_s'] = g.tws.rolling(WIND_SMOOTH_S, center=True, min_periods=30).median()
    return g


def own_inst(g, pts):
    """[heel, heading] per own-track point (same order as pts), for the race page's instrument strip.
    Heel is signed as logged (+ starboard); heading magnetic-corrected as in the grid."""
    out = []
    for p in pts:
        t = p[0]
        h = g.heel.get(t, np.nan) if t in g.index else np.nan
        d = g.hdg.get(t, np.nan) if t in g.index else np.nan
        out.append([round(float(h), 1) if np.isfinite(h) else None, round(float(d)) % 360 if np.isfinite(d) else None])
    return out


def fleet(O, A, t0, t1, skip, names):
    """Every other AIS vessel that sailed within FLEET_RADIUS_NM of us during the race, thinned to one
    position per FLEET_STEP_S: [{mmsi, name, pts: [[t, lat, lon, sog, cog], ...]}]. Unscored."""
    a = A[(A.t >= t0) & (A.t <= t1) & ~A.mmsi.isin(skip)].copy()
    if a.empty:
        return []
    a['s'] = np.floor(a.t).astype(np.int64)
    idx = np.clip(O.index.searchsorted(a.s.values), 0, len(O) - 1)
    a['d'] = rr._nm(a.lat.values, a.lon.values, O.lat.values[idx], O.lon.values[idx])
    out = []
    for mmsi, v in a.groupby('mmsi'):
        near = v[v.d <= FLEET_RADIUS_NM]
        if len(near) < 3 or near.sog.median() < FLEET_MIN_SOG:
            continue
        v = v[v.d <= AIS_MAX_FROM_US_NM]                   # corrupt fixes, as for rivals
        v = v.groupby(v.s // FLEET_STEP_S).first()
        pts = [[int(q.s), round(q.lat, 5), round(q.lon, 5),
                round(q.sog, 1) if np.isfinite(q.sog) else None,
                round(q.cog) if np.isfinite(q.cog) else None] for q in v.itertuples()]
        out.append(dict(mmsi=int(mmsi), name=names.get(str(int(mmsi))), pts=pts))
    return out


def vessel_names():
    """MMSI -> name, the same database the app uses (tools/build_vessel_names.mjs)."""
    try:
        with open(os.path.join(bp.REPO, 'static', 'vessel_names.json')) as f:
            return json.load(f).get('names', {})
    except (OSError, ValueError):
        return {}


def write_fleet(race_id, fl):
    name = f'{race_id}-fleet.json'
    with open(os.path.join(RACES_DIR, name), 'w') as f:
        json.dump(dict(fields=['t', 'lat', 'lon', 'sog', 'cog'], vessels=fl), f, separators=(',', ':'))
    return name


def attach_review(race):
    """Copy docs/reviews/<race id>.md next to the page and point the race at it, if there is one."""
    src = os.path.join(REVIEWS_SRC, f"{race['id']}.md")
    if not os.path.exists(src):
        return
    os.makedirs(REVIEWS_OUT, exist_ok=True)
    shutil.copyfile(src, os.path.join(REVIEWS_OUT, f"{race['id']}.md"))
    race['review'] = f"reviews/{race['id']}.md"


def own_track(g, O, t0, t1):
    x = g.loc[t0:t1]
    raw = [score(s, abs(a), w, bp.TYPON) for s, a, w in zip(x.stw, x.twa, x.tws)]
    pct = pd.Series([p for p, _ in raw], index=x.index)
    mode = pd.Series([m for _, m in raw], index=x.index)
    pct = pct.rolling(OWN_SMOOTH_S, center=True, min_periods=OWN_SMOOTH_S // 2).median()
    # What the box and tooltip show, smoothed like the % so the three numbers agree.
    sm = lambda v: v.rolling(OWN_SMOOTH_S, center=True, min_periods=OWN_SMOOTH_S // 2).median()
    twa_s, stw_s, tws_s = sm(x.twa.abs()), sm(x.stw), sm(x.tws)
    side = np.sign(x.twa.rolling(21, center=True).median())
    for t in x.index[(side.diff().abs() == 2).values]:
        pct.loc[t - TURN_BLANK_S[0]:t + TURN_BLANK_S[1]] = np.nan
    pts = []
    for t in range(t0 - t0 % STEP_S + STEP_S, t1 + 1, STEP_S):
        if t not in O.index or t not in x.index:
            continue
        pts.append(_row(t, O.at[t, 'lat'], O.at[t, 'lon'], pct.loc[t], twa_s.loc[t], tws_s.loc[t],
                        stw_s.loc[t], mode.loc[t], bp.TYPON))
    return pts


def rival_track(g, O, A, mmsi, t0, t1, cert):
    r = A[(A.mmsi == mmsi) & A.sog.notna() & A.cog.notna()].copy()
    r['s'] = np.floor(r.t).astype(np.int64)
    r = r[(r.s >= t0) & (r.s <= t1)].groupby('s').first()       # one reception per report
    rows, prev, dropped = [], None, 0
    for s, a in r.iterrows():
        if s not in g.index:
            continue
        near = O.index[np.clip(O.index.searchsorted(s), 0, len(O) - 1)]
        far = rr._nm(a.lat, a.lon, O.at[near, 'lat'], O.at[near, 'lon']) > AIS_MAX_FROM_US_NM
        jump = prev is not None and rr._nm(a.lat, a.lon, prev[1], prev[2]) / max(s - prev[0], 1) * 3600 > AIS_MAX_JUMP_KN
        if far or jump:
            dropped += 1
            continue
        prev = (s, a.lat, a.lon)
        o = g.loc[s]
        if a.sog < 1 or not np.isfinite(o.cur_e) or not np.isfinite(o.twd_s):
            rows.append((s, a.lat, a.lon, np.nan, np.nan, np.nan, np.nan, ''))
            continue
        c = math.radians(a.cog)
        e, n = a.sog * math.sin(c) - o.cur_e, a.sog * math.cos(c) - o.cur_n
        spd, crs = math.hypot(e, n), math.degrees(math.atan2(e, n)) % 360
        twa = float(bp.signed(o.twd_s - crs))
        p, m = score(spd, abs(twa), o.tws_s, cert)
        rows.append((s, a.lat, a.lon, p, twa, o.tws_s, spd, m))
    if dropped:
        print(f'  {mmsi}: dropped {dropped} implausible AIS position(s)')
    R = pd.DataFrame(rows, columns=['t', 'lat', 'lon', 'pct', 'twa', 'tws', 'spd', 'mode'])
    side = np.sign(R.twa)
    turned = (side != side.shift(1)) | (side != side.shift(-1))
    R['pct'] = R.pct.rolling(3, center=True, min_periods=2).median().where(~turned)
    R['twa'] = R.twa.abs().rolling(3, center=True, min_periods=2).median()
    R['spd'] = R.spd.rolling(3, center=True, min_periods=2).median()
    return [_row(q.t, q.lat, q.lon, q.pct, q.twa, q.tws, q.spd, q.mode, cert) for q in R.itertuples()]


def finish_from_ais(O, A, mmsi, t0, t1):
    """A rival's finish when the results weren't transcribed: its closest AIS pass to where we
    finished, between halfway through the race and 30 min after our finish."""
    fin = O.index[np.clip(O.index.searchsorted(t1), 0, len(O) - 1)]
    lat, lon = O.at[fin, 'lat'], O.at[fin, 'lon']
    r = A[(A.mmsi == mmsi) & (A.t >= (t0 + t1) / 2) & (A.t <= t1 + 1800)]
    if r.empty:
        return None
    d = rr._nm(r.lat.values, r.lon.values, lat, lon)
    k = int(np.argmin(d))
    return int(r.t.iloc[k]), d[k] * 1852


def summary(pts):
    df = pd.DataFrame(pts, columns=FIELDS).dropna(subset=['pct'])
    by = df.groupby('mode').pct.median()
    return (f"median {df.pct.median():.0f}% (" + ', '.join(f'{ {"u": "up", "d": "down", "r": "reach"}[k] } {v:.0f}'
                                                     for k, v in by.items() if k) + f'), {len(df)} scored points')


def build(g, rid):
    """Score every race of one regatta; returns the races list for its JSON file."""
    races, positions, names = [], {}, vessel_names()
    for R in race_list(rid):
        rid_, day, rivals = R['race']['id'], R['day'], R['rivals']
        t0, t1 = _utc_s(day, R['start']), _utc_s(day, R['finish'])
        if day not in positions:
            positions[day] = rr.read_positions(day, None)     # every vessel: rivals and the fleet
        O, A = positions[day]
        if O.empty:
            print(f'{R["id"]}: no GPS positions in the logs for {day} (was the logger running?)')
            continue
        own = own_track(g, O, t0, t1)
        boats = [dict(name='Typon', mmsi=OWN_MMSI, source='instruments', pts=own, inst=own_inst(g, own))]
        print(f'{R["id"]} Typon {summary(boats[0]["pts"])}')
        for name, (mmsi, fin) in rivals.items():
            if fin:
                t_fin = _utc_s(day, fin)
            else:
                f = finish_from_ais(O, A, mmsi, t0, t1)
                if f is None:
                    print(f'{R["id"]} {name}: no AIS reports near our finish')
                    continue
                t_fin = f[0]
                print(f"{R['id']} {name}: finish from AIS {regattas.utc_s_to_local(t_fin).strftime('%H:%M:%S')}"
                      f' ({f[1]:.0f} m from our finish point)')
            pts = rival_track(g, O, A, mmsi, t0, t_fin, rr.CERTS[name.upper()])
            if pts:
                boats.append(dict(name=name, mmsi=mmsi, source='ais', pts=pts))
                print(f'{R["id"]} {name} {summary(pts)}')
            else:
                print(f'{R["id"]} {name}: no AIS reports in the race window')
        skip = {OWN_MMSI} | {v[0] for v in rivals.values()}
        fl = fleet(O, A, t0, t1, skip, names)
        print(f'{R["id"]} fleet: {len(fl)} other vessel(s) within {FLEET_RADIUS_NM:g} nm')
        race = dict(id=R['id'], label=R['label'], start=t0 * 1000, finish=t1 * 1000, boats=boats)
        # Its own file: only the crew page draws the fleet, and replay's picker loads every race at once.
        race['fleet'] = write_fleet(R['id'], fl)
        attach_review(race)
        races.append(race)
    return races


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--regatta', help='only this regatta id from tools/regattas.json (default: all)')
    args = ap.parse_args()
    g = bp.load_grid()
    with contextlib.redirect_stdout(io.StringIO()):
        g, _ = bp.calibrate(g)
    g = own_frame(g)
    os.makedirs(RACES_DIR, exist_ok=True)
    for reg in regattas.regattas():
        if args.regatta and reg['id'] != args.regatta:
            continue
        doc = dict(series=reg['id'], name=reg['name'], generated=pd.Timestamp.now().strftime('%Y-%m-%d'),
                   fields=FIELDS, timezone=regattas._load()['timezone'],
                   inst_fields=['heel', 'hdg'],
                   note='pct = % of the boat\'s own ORC certificate: VMG upwind (u) and downwind (d), speed on a '
                        'reach (r); null = tack/gybe or no data. twa through the water, tws at 10 m; tgt_twa/tgt_spd = ORC '
                        'target angle (beat/gybe; null on a reach, where the course sets it) and speed. Rivals: AIS, '
                        'scored with Typon\'s current and wind. Typon inst = [heel, hdg] per point. fleet = the '
                        'race\'s <id>-fleet.json (other AIS vessels, unscored); review = its narrative, if any. '
                        'tools/race_tracks.py, docs/polar.md §7.',
                   races=build(g, reg['id']))
        out = os.path.join(RACES_DIR, f"{reg['id']}.json")
        with open(out, 'w') as f:
            json.dump(doc, f, separators=(',', ':'))
        print(f'wrote {os.path.relpath(out, bp.REPO)} ({os.path.getsize(out) / 1024:.0f} KB)')
    # The picker's list: every regatta whose file exists, in regattas.json order.
    idx = [dict(id=r['id'], name=r['name'], file=f"{r['id']}.json") for r in regattas.regattas()
           if os.path.exists(os.path.join(RACES_DIR, f"{r['id']}.json"))]
    with open(os.path.join(RACES_DIR, 'index.json'), 'w') as f:
        json.dump(dict(regattas=idx), f, indent=1)
    print(f'wrote static/races/index.json ({len(idx)} regatta(s))')


if __name__ == '__main__':
    main()
