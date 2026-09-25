#!/usr/bin/env python3
"""Typon's polar: the ORC certificate for the router, plus a measured check of how we sail against it.

    python3 tools/build_polar.py                  # analyse the logs -> charts + report in docs/polar/
    python3 tools/build_polar.py --write-js       # ...and write the ORC table into router.js + route-worker.js
    python3 tools/build_polar.py --cache          # reuse the parsed 1 Hz grid (tools/polar_out/grid.pkl)

The router polar is Typon's ORC International certificate (ORC_* below), expanded onto a regular
TWA x TWS grid so that VMG peaks exactly at the certificate's beat and gybe angles. The logs are
NOT used to set the polar; they measure how close we sail to it. docs/polar.md has the reasoning.

Log pipeline:
  1. Parse GPRMC (SOG/COG), HCHDG (10 Hz heading), YXXDR (heel), IIMWV (apparent wind), IIVHW
     (paddlewheel), AGRSA (rudder) into a 1 Hz grid on the log timestamp (GPS time, P42).
  2. Calibrate from the data: paddlewheel scale per day (GPS vs BSP, current held constant per
     15 min); vane heel correction (cos heel); leeway = K*heel/STW^2 (heading vs GPS track, both-tack
     windows); masthead offset + symmetric upwash (upwind TWA equal on both tacks, no TWD jump).
     TWA is referenced to the course through the water, as ORC's angles are, not to the bow.
  3. Keep race windows only, beats (TWA <= 55) and runs (TWA >= 130) only, steady 60 s stretches,
     60 s averages (1 Hz binning puts a boat still carrying a gust's speed into the lull's bin).
"""
import argparse, glob, math, os, re, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR_E = 15.1              # magnetic variation, from $GPRMC on the Bay
LOCAL_UTC_OFFSET_H = -7   # PDT

# Race windows, LOCAL time: start = the turn upwind after the pre-start circling,
# end = head-to-wind as the sails come down (all four within 0.2 nm, off the St. Francis).
RACES = [
    ('2026-09-17 10:04', '2026-09-17 14:21'),
    ('2026-09-18 11:45', '2026-09-18 16:49'),
    ('2026-09-19 12:00', '2026-09-19 15:43'),
    ('2026-09-20 11:51', '2026-09-20 13:55'),
]
UP_MAX_TWA, DOWN_MIN_TWA = 55, 130

# ---- Typon ORC International certificate, "Rated boat velocities in knots"
ORC_TWS = [4, 6, 8, 10, 12, 14, 16, 20, 24]
ORC_BEAT_ANGLE = [47.1, 44.1, 41.6, 40.3, 39.3, 38.8, 38.3, 38.0, 38.4]
ORC_BEAT_VMG = [2.38, 3.41, 4.17, 4.72, 5.11, 5.34, 5.46, 5.58, 5.57]
ORC_ANGLES = [52, 60, 75, 90, 110, 120, 135, 150]
ORC_BSP = np.array([
    [3.77, 5.24, 6.22, 6.91, 7.34, 7.58, 7.70, 7.81, 7.84],
    [4.08, 5.54, 6.50, 7.15, 7.54, 7.77, 7.90, 8.02, 8.06],
    [4.31, 5.74, 6.70, 7.33, 7.71, 7.95, 8.11, 8.32, 8.45],
    [4.18, 5.63, 6.66, 7.38, 7.75, 8.00, 8.20, 8.54, 8.80],
    [4.09, 5.72, 6.89, 7.62, 8.02, 8.29, 8.51, 8.82, 9.00],
    [3.97, 5.57, 6.73, 7.53, 8.00, 8.33, 8.62, 9.17, 9.45],
    [3.49, 5.04, 6.22, 7.15, 7.77, 8.16, 8.49, 9.19, 9.78],
    [2.91, 4.29, 5.47, 6.45, 7.25, 7.79, 8.16, 8.81, 9.49],
])
ORC_RUN_VMG = [2.52, 3.72, 4.73, 5.60, 6.36, 6.99, 7.50, 8.21, 8.80]
ORC_GYBE_ANGLE = [140.9, 145.5, 150.1, 152.5, 156.5, 162.8, 168.5, 177.7, 177.2]
PINCH_LOSS_PER_DEG = 0.05   # speed lost per degree above the beat angle (VMG must fall off)
DEEP_LOSS_PER_DEG = 0.02    # speed lost per degree below the gybe angle

# Router grid: fine close-hauled and deep, so the bilinear lookup keeps the VMG peaks at the
# certificate's beat and gybe angles (tests/test_physics.mjs asserts it)
OUT_TWA = [30, 34, 36, 38, 40, 42, 44, 46, 48, 52, 60, 75, 90, 110, 120, 135, 145, 150, 153, 156, 159, 162, 165, 168, 171, 174, 177, 180]
OUT_TWS = ORC_TWS

# The generic polar the router used before (Swan 47 x 0.85), kept for the "old" line in charts
SWAN_TWA = [52, 60, 75, 90, 110, 120, 135, 150]
SWAN_TWS = [6, 8, 10, 12, 14, 16, 20]
SWAN_BSP = np.array([
    [5.53, 6.47, 7.06, 7.35, 7.49, 7.57, 7.66], [5.81, 6.73, 7.28, 7.56, 7.70, 7.78, 7.86],
    [6.00, 6.90, 7.44, 7.74, 7.93, 8.03, 8.16], [5.87, 6.83, 7.43, 7.77, 8.01, 8.19, 8.42],
    [5.60, 6.77, 7.51, 7.93, 8.19, 8.39, 8.63], [5.45, 6.63, 7.44, 7.90, 8.22, 8.48, 8.89],
    [4.94, 6.12, 7.03, 7.63, 8.02, 8.33, 8.90], [4.18, 5.33, 6.30, 7.09, 7.64, 8.00, 8.57],
])

# ---- chart palette (dataviz reference palette, validated: ordinal blue ramp + categorical 1-2)
SURFACE, INK, INK2, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#e4e3df'
RAMP5 = ['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#0d366b']   # 5 wind speeds, light -> dark
RAMP4 = ['#86b6ef', '#3987e5', '#1c5cab', '#0d366b']              # 4 wind bands
YOU, ORC_C = '#2a78d6', '#eb6834'                                  # categorical slots 1, 2
BOAT_LENGTH_M = 47 * 0.3048   # Typon, 47 ft
# ORC's wind speeds are at 10 m; ours is measured at the masthead. Cert: BAS 1.547 + P 16.970 +
# ~0.5 headboard/crane = 19.0 m above sheer, + ~1.3 m freeboard (estimate). Power-law profile,
# exponent 0.11 over open water (1/7 over land would give -10%). Uncorrected, every ORC target
# we compared against was for too much wind, flattering ORC by up to ~7 points in light air.
MASTHEAD_M, ORC_REF_M, WIND_SHEAR_ALPHA = 20.3, 10.0, 0.11
TWS_TO_10M = (ORC_REF_M / MASTHEAD_M) ** WIND_SHEAR_ALPHA
BANDS = [(6, 10), (10, 13), (13, 16), (16, 25)]
BAND_NAMES = ['6-10 kn', '10-13 kn', '13-16 kn', '16+ kn']


def signed(a):
    return (a + 180) % 360 - 180


def cosd(a):
    return np.cos(np.deg2rad(a))


# ---------------------------------------------------------------- ORC polar
class Cert:
    """An ORC certificate's "Rated boat velocities" table. Rows at ORC_ANGLES are the same on every
    certificate; beat and gybe angles/VMG are per wind column."""
    def __init__(self, tws, beat_angle, beat_vmg, bsp, run_vmg, gybe_angle):
        self.tws, self.beat_angle, self.beat_vmg = tws, beat_angle, beat_vmg
        self.bsp, self.run_vmg, self.gybe_angle = np.asarray(bsp), run_vmg, gybe_angle


TYPON = Cert(ORC_TWS, ORC_BEAT_ANGLE, ORC_BEAT_VMG, ORC_BSP, ORC_RUN_VMG, ORC_GYBE_ANGLE)


def _orc_column(j, twa, c=TYPON):
    """Rated speed at one certificate wind column. VMG is interpolated linearly between the beat
    angle and 52 deg, and between 150 deg and the gybe angle, so the certificate's optimum angles
    stay the optimum; outside them speed falls off by fixed rates."""
    ba, gv_ang = c.beat_angle[j], c.gybe_angle[j]
    beat_v, run_v = c.beat_vmg[j], c.run_vmg[j]
    beat_bsp = beat_v / cosd(ba)
    if twa < ba:
        return beat_bsp * max(0.0, 1 - PINCH_LOSS_PER_DEG * (ba - twa))
    if twa <= ORC_ANGLES[0]:
        v52 = c.bsp[0, j] * cosd(52)
        return float(np.interp(twa, [ba, 52], [beat_v, v52]) / cosd(twa))
    if twa <= 150:
        return float(np.interp(twa, ORC_ANGLES, c.bsp[:, j]))
    v150 = -c.bsp[-1, j] * cosd(150)
    if gv_ang > 150 and twa <= gv_ang:
        return float(np.interp(twa, [150, gv_ang], [v150, run_v]) / -cosd(twa))
    last_ang, last_bsp = (gv_ang, run_v / -cosd(gv_ang)) if gv_ang > 150 else (150, c.bsp[-1, j])
    return last_bsp * max(0.0, 1 - DEEP_LOSS_PER_DEG * (twa - last_ang))


def orc_speed(twa, tws, c=TYPON):
    tws = min(max(tws, c.tws[0]), c.tws[-1])
    j = min(max(np.searchsorted(c.tws, tws) - 1, 0), len(c.tws) - 2)
    f = (tws - c.tws[j]) / (c.tws[j + 1] - c.tws[j])
    return _orc_column(j, twa, c) * (1 - f) + _orc_column(j + 1, twa, c) * f


def orc_table():
    return np.array([[round(_orc_column(j, A), 2) for j in range(len(ORC_TWS))] for A in OUT_TWA])


def orc_beat(tws, c=TYPON):
    return float(np.interp(tws, c.tws, c.beat_vmg)), float(np.interp(tws, c.tws, c.beat_angle))


def orc_run(tws, c=TYPON):
    return float(np.interp(tws, c.tws, c.run_vmg)), float(np.interp(tws, c.tws, c.gybe_angle))


def old_router(twa, tws):
    if twa < SWAN_TWA[0]:
        return np.nan
    twa, tws = min(twa, 150), min(max(tws, 6), 20)
    row = [np.interp(tws, SWAN_TWS, r) for r in SWAN_BSP]
    return 0.85 * float(np.interp(twa, SWAN_TWA, row))


# ---------------------------------------------------------------- 1. parse
def _checksum_ok(s):
    i = s.rfind('*')
    if i < 0:
        return False
    c = 0
    for ch in s[1:i]:
        c ^= ord(ch)
    try:
        return c == int(s[i + 1:i + 3], 16)
    except ValueError:
        return False


TAGS = ('GPRMC', 'HCHDG', 'YXXDR', 'IIMWV', 'IIVHW', 'AGRSA')


def parse(logdir):
    rows = []
    files = sorted(glob.glob(os.path.join(logdir, 'nmea_*.txt')))
    if not files:
        sys.exit(f'no nmea_*.txt in {logdir}')
    for f in files:
        with open(f, errors='replace') as fh:
            for line in fh:
                if len(line) < 30 or line[0] == '#':
                    continue
                s = line[26:].strip()
                if not s.startswith('$') or s[1:6] not in TAGS or not _checksum_ok(s):
                    continue
                try:
                    t = pd.Timestamp(line[:23]).value / 1e9
                    p = s.split('*')[0].split(',')
                    tag = s[1:6]
                    if tag == 'GPRMC' and p[2] == 'A' and p[7] and p[8]:
                        rows += [(t, 'sog', float(p[7])), (t, 'cog', float(p[8]))]
                    elif tag == 'HCHDG' and p[1]:
                        rows.append((t, 'hdg', (float(p[1]) + VAR_E) % 360))
                    elif tag == 'YXXDR' and len(p) > 12 and p[12] == 'Roll':
                        rows.append((t, 'heel', float(p[10])))
                    elif tag == 'IIMWV' and p[1] and p[3] and p[5] == 'A' and p[2] == 'R':
                        rows += [(t, 'awa', float(p[1])), (t, 'aws', float(p[3]))]
                    elif tag == 'IIVHW' and p[5]:
                        rows.append((t, 'bsp', float(p[5])))
                    elif tag == 'AGRSA' and p[1]:
                        rows.append((t, 'rudder', float(p[1])))
                except (ValueError, IndexError):
                    pass
    print(f'parsed {len(files)} files, {len(rows)} values', file=sys.stderr)
    return pd.DataFrame(rows, columns=['t', 'k', 'v'])


def to_grid(raw):
    """1 Hz grid. Instrument channels update slowly (AWA ~5 s, BSP ~2.6 s), so they are
    interpolated between the moments their value CHANGED rather than forward-filled."""
    raw = raw.sort_values('t')
    raw['s'] = np.floor(raw.t).astype(np.int64)
    idx = np.arange(raw.s.min(), raw.s.max() + 1)
    g = pd.DataFrame(index=idx)
    for k in ['sog', 'heel', 'rudder']:
        g[k] = raw[raw.k == k].groupby('s').v.mean()
    for k in ['cog', 'hdg']:
        d = raw[raw.k == k]
        r = np.deg2rad(d.v)
        m = pd.DataFrame({'s': d.s, 'c': np.cos(r), 'n': np.sin(r)}).groupby('s').mean()
        g[k] = np.rad2deg(np.arctan2(m.n, m.c)) % 360
    for k in ['sog', 'cog']:
        g[k] = g[k].ffill(limit=2)
    tf = idx.astype(float)
    for k, ang, lim in [('awa', True, 8), ('aws', False, 8), ('bsp', False, 4)]:
        alld = raw[raw.k == k]
        d = alld[alld.v.diff() != 0]
        v = np.rad2deg(np.unwrap(np.deg2rad(d.v.values))) if ang else d.v.values
        x = np.interp(tf, d.t.values, v)
        # blank anything further than `lim` s from ANY received sentence (logger gaps) —
        # not from the last change, or a constant value (0 kn at the dock) reads as missing
        ts_all = alld.t.values
        near = np.searchsorted(ts_all, tf)
        gap = np.minimum(np.abs(tf - ts_all[np.clip(near, 0, len(ts_all) - 1)]),
                         np.abs(tf - ts_all[np.clip(near - 1, 0, len(ts_all) - 1)]))
        x[gap > lim] = np.nan
        g[k] = (x % 360) if ang else x
    g['day'] = pd.to_datetime(g.index, unit='s').strftime('%Y-%m-%d')
    g['local'] = pd.to_datetime(g.index, unit='s') + pd.Timedelta(hours=LOCAL_UTC_OFFSET_H)
    return g


# ---------------------------------------------------------------- 2. calibrate
def fit_paddlewheel(g):
    d = g.dropna(subset=['sog', 'cog', 'hdg', 'bsp'])
    d = d[(d.sog > 2.5) & (d.bsp > 2.0)].copy()
    d['V'] = d.sog * np.exp(1j * np.deg2rad(90 - d.cog))
    d['U'] = d.bsp * np.exp(1j * np.deg2rad(90 - d.hdg))
    d['win'] = d.index // 900
    out = {}
    for day, dd in d.groupby('day'):
        w = dd.groupby('win')
        div = w.U.transform(lambda u: np.abs((u / np.abs(u)).mean()))
        dd = dd[div < 0.7]           # need heading diversity inside a window to separate k from current
        if len(dd) < 1800:
            continue
        w = dd.groupby('win')
        vp, up = dd.V - w.V.transform('mean'), dd.U - w.U.transform('mean')
        c = (np.conj(up) * vp).sum() / (np.abs(up) ** 2).sum()
        out[day] = (abs(c), math.degrees(np.angle(c)), len(dd) / 3600)
    return out


def fit_deviation(g):
    """Compass deviation vs heading, from upright sailing (heel < 4 deg, so no leeway): the rotation
    between compass heading and GPS track, current held constant per 10 min, as a 2nd-order Fourier
    series. Half its difference between the two beat headings looks exactly like leeway (docs/polar.md
    §2), so it must come out of the heading before leeway is fitted."""
    d = g.dropna(subset=['sog', 'cog', 'hdg', 'stw', 'heel'])
    d = d[(d.heel.abs() < 4) & (d.sog > 3) & (d.stw > 3)].copy()
    d['win'] = d.index // 600
    r = np.deg2rad(d.hdg)
    div = pd.DataFrame({'c': np.cos(r), 's': np.sin(r), 'w': d.win}).groupby('w').transform('mean')
    d = d[np.hypot(div.c, div.s).values < 0.8]     # heading diversity inside a window, as fit_paddlewheel
    V = d.sog.values * np.exp(1j * np.deg2rad(90 - d.cog.values))
    U = d.stw.values * np.exp(1j * np.deg2rad(90 - d.hdg.values))
    X = (-1j * U)[:, None] * _dev_basis(d.hdg.values)   # true heading = compass + dev
    y = (V - U)[:, None]
    dm = lambda a: a - pd.DataFrame(a).groupby(d.win.values).transform('mean').values
    Xd, yd = dm(X), dm(y)[:, 0]
    coef = np.linalg.lstsq(np.vstack([Xd.real, Xd.imag]), np.concatenate([yd.real, yd.imag]), rcond=None)[0]
    return dict(coef=np.rad2deg(coef), hours=len(d) / 3600, windows=d.win.nunique())


def _dev_basis(hdg):
    h = np.deg2rad(np.asarray(hdg, float))
    return np.stack([np.ones_like(h), np.cos(h), np.sin(h), np.cos(2 * h), np.sin(2 * h)], -1)


def deviation(hdg, coef):
    return _dev_basis(hdg) @ coef


def heel_correct(awa, aws, heel):
    """A heeled masthead vane turns in the tilted plane, so it sees the athwartships wind shrunk by
    cos(heel) and reads NARROW (~2 deg of TWA at 25 deg heel). No heel reading -> NaN, not uncorrected."""
    h = np.deg2rad(np.abs(heel))
    a = np.deg2rad(signed(awa))
    x, y = aws * np.cos(a), aws * np.sin(a) / np.cos(h)
    return np.rad2deg(np.arctan2(y, x)) % 360, np.hypot(x, y)


LEE_MAX = 12.0


def leeway(heel, stw, awa, K):
    """Leeway (deg, >= 0, to leeward) = K * heel / STW^2, the usual keelboat form. Tapered out with
    the upwash term: nothing beyond 90 AWA, where rolling heel is not side force."""
    taper = np.clip((90 - np.abs(signed(awa))) / 60, 0, 1)
    return np.clip(K * np.abs(heel) / np.maximum(stw, 3.0) ** 2, 0, LEE_MAX) * taper


def fit_leeway(g):
    """Heading vs GPS track through the water. Current is held constant per 10 min, and only windows
    with both tacks are used: a compass offset flips sign between tacks, leeway (always to leeward) does
    not, so the two separate. Also fits a constant leeway per heel bin as a model-free check."""
    d = g.dropna(subset=['sog', 'cog', 'hdg', 'stw', 'heel', 'awa'])
    d = d[(d.sog > 2.5) & (d.stw > 2.5) & (np.abs(signed(d.awa)) < 60)].copy()
    d['s'] = np.sign(signed(d.awa))
    d['V'] = d.sog * np.exp(1j * np.deg2rad(90 - d.cog))
    d['win'] = d.index // 600
    c = d.groupby(['win', 's']).size().unstack(fill_value=0)
    d = d[d.win.isin(c[(c.get(1, 0) >= 90) & (c.get(-1, 0) >= 90)].index)]

    def cost(x, L):     # boat moves at heading rotated L to leeward: +L math angle with wind from stbd
        r = x.V - x.stw * np.exp(1j * np.deg2rad(90 - x.hdg + x.s * L))
        return (np.abs(r - r.groupby(x.win).transform('mean')) ** 2).mean()
    Ks = np.arange(0, 40.01, 0.5)
    K = Ks[int(np.argmin([cost(d, leeway(d.heel, d.stw, d.awa, k)) for k in Ks]))]
    bins = []
    for lo, hi in [(0, 15), (15, 20), (20, 25), (25, 40)]:
        x = d[(d.heel.abs() >= lo) & (d.heel.abs() < hi)]
        if len(x) >= 1800:
            L = min(np.arange(-4, 12.01, 0.25), key=lambda L: cost(x, L))
            bins.append((lo, hi, L, float(leeway(x.heel, x.stw, x.awa, K).median()), len(x) / 60))
    return dict(K=K, bins=bins, windows=d.win.nunique(), hours=len(d) / 3600)


def true_wind(awa, aws, stw, off, sym, lee=0):
    """TWA referenced to the course through the water (what ORC's angles mean), not the bow:
    the boat moves `lee` deg to leeward of its heading. TWD is still heading + (twa - side*lee)."""
    s = signed(awa)
    taper = np.clip((90 - np.abs(s)) / 60, 0, 1)   # full correction <=30 AWA, none >=90
    a = np.deg2rad(s - off - sym * taper * np.sign(s))
    L = np.deg2rad(lee) * np.sign(s)
    x, y = aws * np.sin(a) + stw * np.sin(L), aws * np.cos(a) - stw * np.cos(L)
    return np.rad2deg(np.arctan2(x, y)) + np.rad2deg(L), np.hypot(x, y)


def _cmean(x):
    r = np.deg2rad(x)
    return math.degrees(math.atan2(np.sin(r).mean(), np.cos(r).mean())) % 360


def find_tacks(g, d, twa):
    side = np.sign(pd.Series(twa, index=d.index).reindex(g.index).rolling(21, center=True).median())
    return g.index[(side.diff().abs() == 2).values]


def fit_awa(g, K):
    d = g.dropna(subset=['awa', 'aws', 'stw', 'hdg', 'sog', 'heel'])
    d = d[d.sog > 2.0].copy()
    d['lee'] = leeway(d.heel, d.stw, d.awa, K)
    twa0, _ = true_wind(d.awa, d.aws, d.stw, 0, 0)
    tacks, last = [], -1e9
    for t in find_tacks(g, d, twa0):
        if t - last < 120:
            continue
        b, a = d.loc[t - 100:t - 30], d.loc[t + 30:t + 100]
        if len(b) < 50 or len(a) < 50 or b.sog.median() < 3 or a.sog.median() < 3:
            continue
        tb, _ = true_wind(b.awa, b.aws, b.stw, 0, 0)
        ta, _ = true_wind(a.awa, a.aws, a.stw, 0, 0)
        if np.median(np.abs(tb)) > 70 or np.median(np.abs(ta)) > 70:
            continue
        tacks.append((np.sign(np.median(tb)), b, a))
        last = t
    # flatten every tack's before/after segment into arrays; segment 2*i = before, 2*i+1 = after
    parts = [(x.awa.values, x.aws.values, x.stw.values, x.hdg.values, x.lee.values, np.full(len(x), 2 * i + k))
             for i, (sg, b, a) in enumerate(tacks) for k, x in ((0, b), (1, a))]
    awa, aws, stw, hdg, lee, seg = (np.concatenate(c) for c in zip(*parts))
    sgn = np.array([sg for sg, _, _ in tacks])
    nseg = 2 * len(tacks)
    cnt = np.bincount(seg, minlength=nseg)
    best = None
    for off in np.arange(-10, 10.01, 0.25):
        for sym in np.arange(-10, 10.01, 0.5):
            twa, _ = true_wind(awa, aws, stw, off, sym, lee)
            twd = np.deg2rad(hdg + twa - np.sign(twa) * lee)
            mdir = np.rad2deg(np.arctan2(np.bincount(seg, np.sin(twd), nseg), np.bincount(seg, np.cos(twd), nseg)))
            mtwa = np.bincount(seg, np.abs(twa), nseg) / cnt
            J = signed(mdir[0::2] - mdir[1::2]) * sgn
            # the tack a segment is on: 'before' has the tack's sign, 'after' the opposite
            stbd = np.where(sgn > 0, mtwa[0::2], mtwa[1::2])
            port = np.where(sgn > 0, mtwa[1::2], mtwa[0::2])
            jump, asym = np.median(J), np.median(stbd) - np.median(port)
            cost = jump ** 2 + asym ** 2
            if best is None or cost < best[0]:
                best = (cost, off, sym, jump, np.median(stbd), np.median(port))
    return dict(n_tacks=len(tacks), offset=best[1], sym=best[2], jump=best[3],
                twa_stbd=best[4], twa_port=best[5], K=K)


def compass_check(g):
    """Vane-independent check of upwind TWA: half the compass heading change through a tack, plus the
    fitted leeway, against the calibrated vane's TWA on either side of the same tack."""
    d = g.dropna(subset=['twa', 'hdg', 'sog'])
    d = d[d.sog > 2]
    rows, last = [], -1e9
    for t in find_tacks(g, d, g.twa.reindex(d.index).values):
        if t - last < 120:
            continue
        b, a = d.loc[t - 100:t - 30], d.loc[t + 30:t + 100]
        if len(b) < 50 or len(a) < 50 or b.sog.median() < 3 or a.sog.median() < 3:
            continue
        if b.twa.abs().median() > 70 or a.twa.abs().median() > 70:
            continue
        ba = pd.concat([b, a])
        rows.append(dict(tws=ba.tws.median(), compass=abs(signed(_cmean(a.hdg) - _cmean(b.hdg))) / 2 + ba.lee.median(),
                         vane=(b.twa.abs().median() + a.twa.abs().median()) / 2))
        last = t
    c = pd.DataFrame(rows)
    c['band'] = pd.cut(c.tws, [b[0] for b in BANDS] + [BANDS[-1][1]], labels=BAND_NAMES, right=False)
    return c.groupby('band', observed=True).agg(tacks=('tws', 'size'), compass_plus_leeway=('compass', 'median'),
                                                vane=('vane', 'median'))


# ---------------------------------------------------------------- 3. select
def race_mask(g):
    m = np.zeros(len(g), bool)
    for a, b in RACES:
        m |= ((g.local >= pd.Timestamp(a)) & (g.local <= pd.Timestamp(b))).values
    return m


def steady_samples(g):
    hr = np.deg2rad(g.hdg)
    R = np.hypot(np.cos(hr).rolling(61, center=True).mean(), np.sin(hr).rolling(61, center=True).mean())
    hstd = np.rad2deg(np.sqrt(-2 * np.log(R.clip(1e-6, 1))))
    one_side = np.sign(g.twa).rolling(61, center=True).mean().abs()
    ok = ((hstd < 6) & (one_side > 0.99) & (g.sog > 1.5) & (g.stw > 1.5)
          & (g.tws.rolling(61, center=True).std() < 1.5) & race_mask(g))
    s = g.where(ok)
    roll = lambda x: x.rolling(60, center=True, min_periods=45)
    avg = pd.DataFrame(index=g.index)
    for k in ['stw', 'tws', 'rudder']:
        avg[k] = roll(s[k]).mean()
    avg['atwa'] = roll(s.twa.abs()).mean()
    avg['heel'] = roll(s.heel.abs()).mean()
    avg['lee'] = roll(s.lee).mean()
    avg['rud_sd'] = roll(s.rudder).std()        # steering activity
    avg['tack'] = np.sign(g.twa)
    avg['local'] = g.local
    d = avg[ok].dropna(subset=['stw', 'tws', 'atwa'])
    d = d[(d.atwa <= UP_MAX_TWA) | (d.atwa >= DOWN_MIN_TWA)].copy()
    d['sector'] = np.where(d.atwa <= UP_MAX_TWA, 'upwind', 'downwind')
    d['band'] = pd.cut(d.tws, [b[0] for b in BANDS] + [BANDS[-1][1]], labels=BAND_NAMES, right=False)
    sg = np.where(d.sector == 'upwind', 1, -1)
    d['vmg'] = d.stw * cosd(d.atwa) * sg
    d['orc_vmg'] = [orc_beat(s)[0] if sec == 'upwind' else orc_run(s)[0] for s, sec in zip(d.tws, d.sector)]
    d['orc_angle'] = [orc_beat(s)[1] if sec == 'upwind' else orc_run(s)[1] for s, sec in zip(d.tws, d.sector)]
    d['vmg_pct'] = d.vmg / d.orc_vmg
    d['orc_bsp_here'] = [orc_speed(a, s) for a, s in zip(d.atwa, d.tws)]
    # same wind (1 kn) and angle (5 deg) cell -> speed relative to its own cell average
    d['cell'] = np.round(d.tws).astype(int).astype(str) + '_' + (np.round(d.atwa / 5) * 5).astype(int).astype(str)
    big = d.groupby('cell').stw.transform('size') >= 120
    d['ds'] = np.where(big, d.stw - d.groupby('cell').stw.transform('mean'), np.nan)
    return d


# ---------------------------------------------------------------- 4. analyses
def measured_best(d, q=0.9):
    """Best-10% speed at a few nodes, only where there are >= 3 min of data (chart dots)."""
    out = []
    for A, sa in [(35, 3), (40, 3), (45, 3), (135, 8), (150, 8), (165, 8)]:
        for S in [8, 10, 12, 16, 20]:
            w = np.exp(-0.5 * (((d.atwa.values - A) / sa) ** 2 + ((d.tws.values - S) / 1.2) ** 2))
            if w.sum() / 60 >= 3:
                o = np.argsort(d.stw.values)
                c = np.cumsum(w[o])
                out.append((A, S, float(np.interp(q * c[-1], c, d.stw.values[o]))))
    return out


def vs_orc(d):
    rows = []
    for sec in ['upwind', 'downwind']:
        for b in BAND_NAMES:
            x = d[(d.sector == sec) & (d.band == b)]
            if len(x) < 300:
                continue
            q = x.atwa.quantile([.25, .5, .75]).values
            rows.append(dict(sector=sec, band=b, minutes=len(x) / 60, vmg_pct=x.vmg_pct.median(),
                             vmg_pct_best=x.vmg_pct.quantile(.9), angle=q[1], angle_lo=q[0], angle_hi=q[2],
                             orc_angle=x.orc_angle.median(), stw=x.stw.median(),
                             orc_bsp_at_orc_angle=np.median([orc_speed(a, s) for a, s in zip(x.orc_angle, x.tws)]),
                             spd_pct=(x.stw / x.orc_bsp_here).median(), lee=x.lee.median(), heel=x.heel.median(),
                             tws=x.tws.median()))
    return pd.DataFrame(rows)


def vmg_by_angle(d):
    out = {}
    for sec in ['upwind', 'downwind']:
        x = d[d.sector == sec].copy()
        x['ab'] = (np.round(x.atwa / 5) * 5).astype(int)
        for b in BAND_NAMES:
            y = x[x.band == b]
            t = y.groupby('ab').vmg.agg(['median', 'size'])
            t = t[t['size'] >= 180]
            if len(t) >= 2:
                out[(sec, b)] = (t['median'], y.atwa.median(), y.tws.median())
    return out


POINT_SPEED_LOSS = 0.3   # kn: pointing 2-4 deg higher paid while speed fell by less than this (docs/polar.md §3)
CRIB_COLS = [6, 8, 10, 12, 14, 16, 20]


def stretches(d, sector):
    """Moments against the median of their own 10-minute stretch on the same tack (>= 3 min of it),
    so wind strength, tide and sea state are held roughly fixed. Adds bow and d_* columns."""
    x = d[d.sector == sector].copy()
    x['bow'] = x.atwa - x.lee
    x['win'] = (x.index // 600).astype(str) + np.where(x.tack > 0, 's', 'p')
    x = x[x.groupby('win').bow.transform('size') >= 180]
    for k in ['bow', 'stw', 'vmg', 'tws', 'lee', 'atwa', 'heel']:
        x['d_' + k] = x[k] - x.groupby('win')[k].transform('median')
    return x


def speed_floor(st):
    """Per crib-sheet wind column: our normal-groove speed (within 1 deg of the stretch's bow angle,
    TWS within 1 kn of the column; 18-22 for 20) minus POINT_SPEED_LOSS. None under 3 minutes."""
    out = {}
    norm = st[st.d_bow.abs() <= 1]
    for s in CRIB_COLS:
        lo, hi = (18, 22) if s == 20 else (s - 1, s + 1)
        x = norm[(norm.tws >= lo) & (norm.tws < hi)]
        out[s] = (x.stw.median() - POINT_SPEED_LOSS, x.stw.median()) if len(x) >= 180 else None
    return out


def power_mode(st):
    """Per crib-sheet wind column: VMG gained per extra degree of heel inside a stretch (TWS within
    2 kn of the column; 17-25 for 20). More heel paying = short of power; costing = overpowered.
    Gusts leak in (more heel partly means more wind), so this overstates what trim alone gains."""
    out = {}
    s_ = st.dropna(subset=['d_heel'])
    for s in CRIB_COLS:
        lo, hi = (17, 25) if s == 20 else (s - 2, s + 2)
        x = s_[(s_.tws >= lo) & (s_.tws < hi)]
        if len(x) < 600:
            out[s] = None
            continue
        k = np.polyfit(x.d_heel, x.d_vmg, 1)[0]
        out[s] = ('POWER UP' if k > 0.03 else 'depower' if k < -0.01 else 'on the edge', k)
    return out


def sustained_best(d, windows=(300, 600)):
    """Best upwind VMG (% of ORC) held as a rolling mean over each window, per wind band: what we can
    actually sustain, as opposed to a 1-2 minute peak that a lull or the 5 s wind lag can flatter.
    Needs 80% of the window to be steady sailing; the band is the window's mean wind."""
    up = d[d.sector == 'upwind']
    full = up.reindex(np.arange(up.index.min(), up.index.max() + 1))
    out = {}
    for W in windows:
        r = full[['vmg_pct', 'tws']].rolling(W, min_periods=int(W * 0.8), center=True).mean().dropna()
        band = pd.cut(r.tws, [b[0] for b in BANDS] + [BANDS[-1][1]], labels=BAND_NAMES, right=False)
        out[W] = r.vmg_pct.groupby(band, observed=True).max().to_dict()
    return out


def helm_analysis(d):
    up = d[d.sector == 'upwind'].dropna(subset=['rudder']).copy()
    st, pt = up[up.tack > 0].rudder.median(), up[up.tack < 0].rudder.median()
    zero, sgn = (st + pt) / 2, np.sign(st - pt)
    up['whelm'] = (up.rudder - zero) * up.tack * sgn     # + = weather helm (the usual upwind side)
    return up, dict(zero=zero, typical=abs(st - pt) / 2)


def manoeuvres(g):
    rows, last = [], -1e9
    twd = (g.hdg + g.twa_bow) % 360
    side = np.sign(g.twa.rolling(21, center=True).median())
    rm = race_mask(g)
    for t in g.index[(side.diff().abs() == 2).values & rm]:
        if t - last < 90:
            continue
        b, a, after = g.loc[t - 75:t - 15], g.loc[t - 15:t + 60], g.loc[t + 60:t + 120]
        if b.stw.isna().mean() > .2 or a.stw.isna().mean() > .2 or len(b) < 50:
            continue
        atb, ata = np.abs(b.twa).median(), np.abs(after.twa).median()
        kind = 'tack' if atb < 70 and ata < 70 else ('gybe' if atb > 110 and ata > 110 else None)
        if not kind:
            continue
        wd = np.deg2rad(_cmean(twd.loc[t - 75:t - 15].dropna()))
        sg = 1 if kind == 'tack' else -1
        vm = lambda s: (s.stw * np.cos(np.deg2rad(s.hdg) - wd)).fillna(0) * sg
        base = vm(b).median()
        lost_nm = base * 75 / 3600 - vm(a).sum() / 3600
        pre, rec = b.stw.median(), a.stw.values
        recover = next((k for k, v in enumerate(rec) if k > 20 and v >= 0.95 * pre), np.nan) - 15
        rows.append(dict(kind=kind, tws=b.tws.median(), lost_m=lost_nm * 1852,
                         lost_s=lost_nm / (base / 3600) if base > 0 else np.nan,
                         pre=pre, min=np.nanmin(rec), recover_s=recover))
        last = t
    m = pd.DataFrame(rows)
    m['band'] = pd.cut(m.tws, [b[0] for b in BANDS] + [BANDS[-1][1]], labels=BAND_NAMES, right=False)
    return m


# ---------------------------------------------------------------- 5. router table
def write_js(table):
    rows = ',\n'.join('    [' + ', '.join(f'{x:.2f}' for x in r) + ']' for r in table)
    block = (f'const POLAR_TWA = {OUT_TWA};\n'
             + f'const POLAR_TWS = {OUT_TWS};\n'
             + f'const POLAR_BSP = [\n{rows},\n];\n')
    pat = re.compile(r'(// --- Polar Table.*?---\n)(.*?)(// --- end polar ---)', re.S)
    for rel in ['static/js/router.js', 'static/js/route-worker.js']:
        p = os.path.join(REPO, rel)
        src = open(p).read()
        new, n = pat.subn(lambda m: m.group(1) + block + m.group(3), src)
        if n != 1:
            sys.exit(f'{rel}: expected exactly one "// --- Polar Table ... ---" ... "// --- end polar ---" block, found {n}')
        open(p, 'w').write(new)
        print(f'wrote polar table into {rel}')


# ---------------------------------------------------------------- 6. charts
def _style(plt):
    plt.rcParams.update({'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE, 'savefig.facecolor': SURFACE,
                         'axes.edgecolor': GRID, 'axes.labelcolor': INK2, 'xtick.color': INK2, 'ytick.color': INK2,
                         'text.color': INK, 'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.8,
                         'axes.spines.top': False, 'axes.spines.right': False, 'font.size': 10,
                         'axes.titlesize': 11, 'axes.titleweight': 'bold', 'legend.frameon': False,
                         'lines.linewidth': 2})


def chart_polar(d, outdir, plt):
    shown = [8, 10, 12, 16, 20]
    fig = plt.figure(figsize=(9, 8.2))
    ax = fig.add_subplot(111, projection='polar')
    ax.set_theta_zero_location('N'); ax.set_theta_direction(-1); ax.set_thetamin(0); ax.set_thetamax(180)
    th = np.arange(30, 181, 1)
    best = measured_best(d)
    for S, c in zip(shown, RAMP5):
        ax.plot(np.deg2rad(th), [orc_speed(A, S) for A in th], '-', color=c, lw=2, label=f'{S} kn')
        old = [old_router(A, S) for A in th]
        ax.plot(np.deg2rad(th), old, ':', color=c, lw=1.2)
        pts = [(A, v) for A, s, v in best if s == S]
        if pts:
            ax.plot(np.deg2rad([p[0] for p in pts]), [p[1] for p in pts], 'o', color=c, ms=8,
                    mec=SURFACE, mew=2)
        ba = orc_beat(S)[1]; ga = orc_run(S)[1]
        ax.plot(np.deg2rad([ba, ga]), [orc_speed(ba, S), orc_speed(ga, S)], 'D', color=c, ms=6, mec=INK, mew=0.8)
    ax.set_rlim(0, 10); ax.set_rlabel_position(95)
    ax.set_title('Typon polar: ORC certificate (solid) — what the router now uses. Radius = boat speed through water (kn)\n'
                 'dots = our best-10% race speed · ◆ = ORC beat / gybe angle · dotted = old router polar (Swan 47 × 0.85)',
                 fontsize=10, pad=18)
    ax.legend(loc='lower left', bbox_to_anchor=(0.88, 0.02), title='True wind', fontsize=9)
    plt.tight_layout(); p = os.path.join(outdir, 'polar.png'); plt.savefig(p, dpi=120); plt.close(); return p


def chart_vs_orc(v, outdir, plt):
    fig, axs = plt.subplots(1, 3, figsize=(16, 5.2), gridspec_kw={'width_ratios': [1.2, 1, 1]})
    ax = axs[0]
    for sec, c, off in [('upwind', YOU, -0.12), ('downwind', ORC_C, 0.12)]:
        x = v[v.sector == sec]
        pos = [BAND_NAMES.index(b) + off for b in x.band]
        ax.vlines(pos, x.vmg_pct * 100, x.vmg_pct_best * 100, color=c, lw=2)
        ax.plot(pos, x.vmg_pct * 100, 'o', color=c, ms=9, mec=SURFACE, mew=2, label=f'{sec}: median')
        ax.plot(pos, x.vmg_pct_best * 100, 'o', color=SURFACE, ms=8, mec=c, mew=2, label=f'{sec}: best 10%')
        for p_, m in zip(pos, x.vmg_pct):
            ax.text(p_, m * 100 - 2.2, f'{m * 100:.0f}%', ha='center', va='top', fontsize=9, color=INK)
    ax.axhline(100, color=INK2, lw=1)
    ax.text(3.45, 100.6, 'ORC rating', color=INK2, fontsize=9, ha='right')
    ax.set_xticks(range(4), BAND_NAMES); ax.set_ylim(78, 110); ax.set_ylabel('VMG, % of ORC beat / run VMG')
    ax.set_title('How close we sail to the certificate'); ax.legend(fontsize=8, loc='upper right', ncol=2)
    for ax, sec, title in [(axs[1], 'upwind', 'Upwind angle: ours vs ORC optimum (dotted = leeway)'),
                           (axs[2], 'downwind', 'Downwind angle: ours vs ORC optimum')]:
        x = v[v.sector == sec]
        pos = np.array([BAND_NAMES.index(b) for b in x.band])
        ax.vlines(pos, x.angle_lo, x.angle_hi, color=YOU, lw=2)
        ax.plot(pos, x.angle, 'o', color=YOU, ms=9, mec=SURFACE, mew=2, label='we sailed (median, IQR)')
        ax.plot(pos, x.orc_angle, 'D', color=ORC_C, ms=8, mec=SURFACE, mew=1.5, label='ORC optimum')
        if sec == 'upwind':
            ax.plot(pos, x.angle - x.lee, 'o', color=SURFACE, ms=8, mec=YOU, mew=2, label='our bow angle (what a calibrated display shows)')
            ax.vlines(pos, x.angle - x.lee, x.angle, color=YOU, lw=1, linestyles=':')
        for p_, a, o in zip(pos, x.angle, x.orc_angle):
            ax.text(p_ + 0.12, (a + o) / 2, f'{a - o:+.0f}°', fontsize=9, color=INK, va='center')
        ax.set_xticks(range(4), BAND_NAMES); ax.set_ylabel('TWA through the water (deg)'); ax.set_title(title); ax.legend(fontsize=8)
        if sec == 'downwind':
            ax.invert_yaxis()
    axs[2].text(0.02, 0.02, 'higher on the chart = deeper', transform=axs[2].transAxes, color=INK2, fontsize=8)
    plt.tight_layout(); p = os.path.join(outdir, 'vs_orc.png'); plt.savefig(p, dpi=120); plt.close(); return p


def chart_vmg_angle(va, outdir, plt):
    fig, axs = plt.subplots(1, 2, figsize=(14, 5.2))
    for ax, sec, rng in [(axs[0], 'upwind', np.arange(28, 58)), (axs[1], 'downwind', np.arange(128, 181))]:
        sg = 1 if sec == 'upwind' else -1
        for b, c in zip(BAND_NAMES, RAMP4):
            if (sec, b) not in va:
                continue
            t, sailed, tws = va[(sec, b)]
            ax.plot(t.index, t.values, '-o', color=c, ms=6, label=f'{b}: ours')
            # ORC is only defined from the beat angle out to the gybe angle; beyond those the table's
            # fall-off is our router extrapolation, not the certificate, so it is not drawn
            lo_a, hi_a = (orc_beat(tws)[1], rng[-1]) if sec == 'upwind' else (rng[0], orc_run(tws)[1])
            r = rng[(rng >= lo_a) & (rng <= hi_a)]
            ax.plot(r, [orc_speed(A, tws) * cosd(A) * sg for A in r], '--', color=c, lw=1.2)
            ax.plot(sailed, np.interp(sailed, t.index, t.values), '|', color=c, ms=22, mew=2.5)
        ax.set_xlabel('TWA through the water (deg)'); ax.set_ylabel('VMG (kn)')
        ax.set_title(f'{sec.upper()}: VMG we made (solid) vs ORC at the same wind (dashed)\n'
                     'ORC drawn from beat to gybe angle only · | = the angle we sailed most', fontsize=10)
        ax.legend(fontsize=8, loc='lower left' if sec == 'downwind' else 'lower left')
    plt.tight_layout(); p = os.path.join(outdir, 'vmg_by_angle.png'); plt.savefig(p, dpi=120); plt.close(); return p


def chart_heel_helm(up, outdir, plt):
    fig, axs = plt.subplots(1, 4, figsize=(19, 4.8))
    for b, c in zip(BAND_NAMES, RAMP4):
        x = up[up.band == b].dropna(subset=['ds'])
        if len(x) < 300:
            continue
        for ax, col, step, lo in [(axs[0], 'heel', 3, 0), (axs[1], 'whelm', 2, -4), (axs[3], 'rud_sd', 1, 0)]:
            y = x.dropna(subset=[col])
            bb = np.floor((y[col] - lo) / step) * step + lo + step / 2
            h, n = y.groupby(bb).ds.mean(), y.groupby(bb).size()
            h = h[n >= 120]
            ax.plot(h.index, h.values, '-o', color=c, ms=6, label=b)
    hb = np.floor(up.heel / 3) * 3 + 1.5
    h = up.groupby(hb).whelm.median()[up.groupby(hb).size() >= 120]
    axs[2].plot(h.index, h.values, '-o', color=YOU, ms=6)
    for ax, xl, t in [(axs[0], 'heel (deg)', 'Speed vs HEEL'),
                      (axs[1], 'weather helm (deg)', 'Speed vs WEATHER HELM'),
                      (axs[3], 'rudder movement, sd over 60 s (deg)', 'Speed vs STEERING ACTIVITY')]:
        ax.axhline(0, color=INK2, lw=1); ax.set_xlabel(xl); ax.set_title(f'Upwind: {t}')
        ax.set_ylabel('kn vs average at same wind & angle'); ax.legend(fontsize=8, title='True wind')
    axs[2].set_xlabel('heel (deg)'); axs[2].set_ylabel('weather helm (deg)'); axs[2].set_title('Upwind: helm grows with heel')
    plt.tight_layout(); p = os.path.join(outdir, 'heel_helm.png'); plt.savefig(p, dpi=120); plt.close(); return p


def chart_cheatsheet(v, up, m, heel_fast, floor, power, held, cal, hours, outdir, plt):
    """One-page crib sheet: targets from the certificate, habits from our logs."""
    fig = plt.figure(figsize=(11, 33.3))
    fig.patch.set_facecolor(SURFACE)
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis('off'); ax.set_xlim(0, 100); ax.set_ylim(-102.8, 200)
    T = lambda x, y, s, **k: ax.text(x, y, s, **{'color': INK, 'fontsize': 10, 'va': 'top', **k})
    T(5, 197, 'TYPON — speed & angle crib sheet', fontsize=22, fontweight='bold')
    T(5, 192.2, f'Targets: ORC International certificate.  Habits: {hours:.0f} h of steady beats and runs, Big Boat Series 17–20 Sept 2026.',
      fontsize=10, color=INK2)

    # --- targets table
    T(5, 187, 'TARGETS (true wind at 10 m, as ORC; boat speed through water)', fontsize=12, fontweight='bold')
    cols = CRIB_COLS
    x0, dx = 30, 9.5
    for i, s in enumerate(cols):
        T(x0 + i * dx, 183, f'{s} kn', fontweight='bold', ha='center', color=INK2)
        T(x0 + i * dx, 180.6, f'display {s / TWS_TO_10M:.1f}', ha='center', color=INK2, fontsize=7.5)
    band_of = dict(zip(cols, ['6-10 kn', '6-10 kn', '10-13 kn', '10-13 kn', '13-16 kn', '16+ kn', '16+ kn']))
    lee_b = v[v.sector == 'upwind'].set_index('band').lee
    rows = [('Upwind TWA (track)', [f'{orc_beat(s)[1]:.0f}°' for s in cols]),
            ('  on display, our leeway', [f'{orc_beat(s)[1] - lee_b[band_of[s]]:.0f}°' if band_of[s] in lee_b else '–' for s in cols]),
            ('Upwind speed', [f'{orc_beat(s)[0] / cosd(orc_beat(s)[1]):.1f}' for s in cols]),
            ('Upwind VMG', [f'{orc_beat(s)[0]:.1f}' for s in cols]),
            ('Upwind speed floor (ours)', [f'{floor[s][0]:.1f}' if floor.get(s) else '–' for s in cols]),
            ('Upwind power (ours)', [power[s][0] if power.get(s) else '–' for s in cols]),
            ('Downwind TWA', [f'{orc_run(s)[1]:.0f}°' for s in cols]),
            ('Downwind speed', [f'{orc_run(s)[0] / -cosd(orc_run(s)[1]):.1f}' for s in cols]),
            ('Downwind VMG', [f'{orc_run(s)[0]:.1f}' for s in cols]),
            ('Upwind heel (our fastest)', [heel_fast.get(s, '–') for s in cols])]
    y = 178.5
    for k, (name, vals) in enumerate(rows):
        if k in (6, 9):
            ax.add_patch(matplotlib_rect(4, y + 1.4, 92, 0.15, GRID))
        T(5, y, name, fontsize=10.5, color=INK2)
        for i, s in enumerate(vals):
            T(x0 + i * dx, y, s, ha='center', fontsize=9.5 if 'power' in name else 11,
              fontweight='bold' if 'TWA' in name or s == 'POWER UP' else 'normal')
        y -= 4.2
    T(5, y + 0.5, 'Track = through the water, as ORC measures. Display row = ORC angle minus our measured leeway in that band; the display shows the bow.\n'
      f'Speed floor: point as high as you can, but not below this. It is our normal-groove speed at that wind minus {POINT_SPEED_LOSS} kn, where pointing\n'
      'higher stopped paying. Paddlewheel-corrected: the raw display may read up to ~0.3 kn lower. Judge it after a minute, not at once.\n'
      'Power row: whether one more degree of heel gained VMG (POWER UP), didn\'t matter (on the edge) or cost it (depower), in our sailing at that wind.\n'
      'Heel row: median heel in the faster half of our steady upwind sailing, by wind band (6–10 / 10–13 / 13–16 / 16+).',
      fontsize=8, color=INK2, linespacing=1.4)
    y -= 7.5

    # --- upwind playbook: the rules below, in the order they are used on the water
    y -= 1
    T(5, y, 'UPWIND, IN ORDER', fontsize=12, fontweight='bold'); y -= 4.2
    play = [
        'Point up until the jib luff just lifts. Never foot for speed.',
        'Watch speed through the water: stay above the speed-floor row. Below it, bear away 2°, rebuild, then point again.',
        'Heel: power up to 12–15° under ~11 kn. 22–26° over 16 kn. Past ~28°: flatten, traveller down, feather; if that fails, reef.',
        'Tack less over 13 kn: each tack costs ~2–3 boat lengths.',
    ]
    for i, t in enumerate(play):
        T(6, y, f'{i + 1}.', fontsize=10.5, fontweight='bold', color=YOU)
        T(9, y, t, fontsize=10.5)
        y -= 3.4
    y -= 1

    # --- how we sail vs the cert
    y -= 5
    T(5, y, 'HOW WE SAIL vs THE CERTIFICATE (median VMG)', fontsize=12, fontweight='bold'); y -= 4.5
    for sec in ['upwind', 'downwind']:
        x = v[v.sector == sec]
        T(5, y, sec.capitalize(), fontsize=10.5, color=INK2)
        for i, (_, r) in enumerate(x.iterrows()):
            T(30 + i * 17, y, f'{r.band}: {r.vmg_pct * 100:.0f}%  ({r.angle - r.orc_angle:+.0f}°)', fontsize=10.5)
        y -= 4.2
    for W, lab in [(300, 'Upwind best held 5 min'), (600, 'Upwind best held 10 min')]:
        T(5, y, lab, fontsize=10.5, color=INK2)
        for i, b in enumerate(BAND_NAMES):
            T(30 + i * 17, y, f'{b}: {held[W][b] * 100:.0f}%' if b in held[W] else f'{b}: –', fontsize=10.5)
        y -= 4.2
    T(5, y + 0.8, 'In brackets: our median angle minus ORC optimum.  Upwind: + = wider.  Downwind: − = higher (not deep enough).\n'
      'Light air: we have held ORC for 5 min, so the gap is consistency. 13+ kn: ~93% is what we can hold now; the rest is sails and crew weight.\n'
      'Light-air % is the least certain: 1 kn of wind error moves it several points; 16+ kn barely moves.',
      fontsize=8, color=INK2, linespacing=1.4)
    y -= 4.4

    # --- tacks and gybes, in boat lengths
    y -= 5
    T(5, y, 'TACKS & GYBES — distance lost (47 ft boat lengths)', fontsize=12, fontweight='bold'); y -= 4.5
    for i, b in enumerate(BAND_NAMES):
        T(30 + i * 17, y, b, fontweight='bold', color=INK2)
    y -= 4.2
    bl = lambda x: x / BOAT_LENGTH_M
    tacks, gybes = m[m.kind == 'tack'], m[m.kind == 'gybe']
    for name, fn in [
        ('Tack (median)', lambda x: f'{bl(x.lost_m.median()):.1f} BL' + ('  *' if x.band.iloc[0] == BAND_NAMES[0] else '')),
        ('Tack (middle half)', lambda x: f'{bl(x.lost_m.quantile(.25)):.1f} – {bl(x.lost_m.quantile(.75)):.1f} BL'),
        ('Tack: secs to 95% speed', lambda x: f'{x.recover_s.median():.0f} s'),
    ]:
        T(5, y, name, fontsize=10.5, color=INK2)
        for i, b in enumerate(BAND_NAMES):
            x = tacks[tacks.band == b]
            T(30 + i * 17, y, fn(x) if len(x) >= 5 else '–', fontsize=10.5)
        y -= 4.2
    T(5, y, 'Gybe (median)', fontsize=10.5, color=INK2)
    T(30, y, f'about {bl(gybes.lost_m.median()):.1f} BL in every wind band ({len(gybes)} gybes)', fontsize=10.5)
    y -= 4.2
    T(5, y + 0.8, f'{len(tacks)} race tacks.  * light air unreliable: tacking on a shift shows up as a gain.  '
      'Loss = distance behind sailing on at the pre-tack VMG.', fontsize=8, color=INK2)

    # --- rules of thumb
    dn = v[v.sector == 'downwind']; upv = v[v.sector == 'upwind']
    worst_dn = dn.loc[dn.vmg_pct.idxmin()]
    tk = m[m.kind == 'tack']
    tk_heavy = tk[tk.tws >= 13]
    hv = upv[upv.band.isin(BAND_NAMES[2:])]
    rules = [
        ('1', 'SAIL DEEPER DOWNWIND — speed will drop, and that is fine.',
         f'We sailed {dn.angle.min():.0f}–{dn.angle.max():.0f}°; the cert says {dn.orc_angle.min():.0f}–{dn.orc_angle.max():.0f}°. '
         f'Worst in {worst_dn.band}: {worst_dn.vmg_pct * 100:.0f}% of rated VMG; at our angle our speed matched ORC\'s. '
         'Going deeper, VMG still gains while speed stays above: '
         + ', '.join(f'{r.band} {r.orc_angle:.0f}° at {-r.stw * cosd(r.angle) / -cosd(r.orc_angle):.1f} kn (now {r.angle:.0f}° at {r.stw:.1f})'
                     for _, r in dn.iterrows() if r.orc_angle - r.angle > 3)
         + '. Symmetric spinnaker, so ORC\'s 168–178° in 16+ is sailable.'),
        ('2', f'UPWIND IS THE BIGGEST LOSS ({upv.vmg_pct.min() * 100:.0f}–{upv.vmg_pct.max() * 100:.0f}% of rated VMG): slow in heavy air, wide in light.',
         f'Through the water we made {upv.angle.min():.0f}–{upv.angle.max():.0f}° where ORC says {upv.orc_angle.min():.0f}–{upv.orc_angle.max():.0f}° '
         f'({upv.vmg_pct.min() * 100:.0f}–{upv.vmg_pct.max() * 100:.0f}% of rated VMG). By the bow we point '
         f'{(upv.angle - upv.lee).min():.0f}–{(upv.angle - upv.lee).max():.0f}° and slip {upv.lee.min():.0f}–{upv.lee.max():.0f}° sideways. '
         f'Speed at our own angle: {upv.spd_pct.iloc[0] * 100:.0f}% of ORC\'s in {upv.band.iloc[0]}, {hv.spd_pct.min() * 100:.0f}–{hv.spd_pct.max() * 100:.0f}% in 13+ kn. '
         f'Downwind we match ORC\'s speed at our angle ({dn.spd_pct.min() * 100:.0f}–{dn.spd_pct.max() * 100:.0f}%), so hull and bottom look fine: '
         'the gap is upwind sails and trim. Check the main\'s shape first.'),
        ('3', 'DON\'T FOOT. Point as high as the jib allows, down to the speed floor.',
         'Within the same 10-minute stretch, each degree wider by the bow bought only 0.02–0.06 kn; breaking even needs ~0.1. '
         'In 10+ kn, moments 2–4° higher than the stretch made +0.1–0.2 kn more VMG (puffs explain only a small part) as long as speed fell less '
         f'than {POINT_SPEED_LOSS} kn. In 6–10 kn pointing higher was about even: keep the speed. In 16+ that means feathering on the edge of the jib.'),
        ('4', 'UNDER ~11 kn WE ARE SHORT OF POWER: power up.',
         'Below 11 kn each extra degree of heel gained +0.05–0.08 kn VMG (most of 17 Sept, the lulls on 20 Sept); 11–17 kn was on the edge. '
         'Backstay off, jib halyard eased, car forward, fuller main with twist; let her heel to ~15° in 6–10 kn. '
         f'Neutral helm (0–2°) was 0.15–0.3 kn slow; typical helm {up.whelm.median():.1f}°, ~1° more per 4° heel.'),
        ('5', 'OVER ~17 kn WE ARE OVERPOWERED: flatten, traveller down, feather.',
         'Above 17 kn extra heel cost VMG (−0.04 kn per degree) and added leeway. Sweet spot 22–26° heel; past ~28° VMG fell ~0.07 kn '
         'even with 0.8 kn more wind. If traveller, backstay and feathering can\'t hold her under ~28°, that is the reef / change-down '
         'signal (our data only reaches ~21 kn at 10 m). Easing to neutral helm is untested.'),
        ('6', 'LIGHT AIR: quiet helm.',
         'In 6–13 kn, busy steering (rudder moving 3°+) cost 0.15–0.25 kn. Trim to the puffs; '
         'steer only to the telltales. In 13+ kn steering through waves is fine.'),
        ('7', f'TACKS COST ~{tk_heavy.lost_m.median() / BOAT_LENGTH_M:.1f} BOAT LENGTHS (~{tk_heavy.lost_s.median():.0f} s) in 13+ kn.',
         f'Speed bottoms at ~{tk_heavy["min"].median():.1f} kn and takes ~{tk_heavy.recover_s.median():.0f} s to rebuild to 95%. '
         f'~15 tacks a race = ~{15 * tk_heavy.lost_m.median() / BOAT_LENGTH_M:.0f} BL. Our best quarter cost ≤{tk_heavy.lost_m.quantile(.25) / BOAT_LENGTH_M:.0f} BL: '
         'build speed before pointing; tack less in heavy air.'),
    ]
    # biggest loss first: the two upwind rules lead, downwind follows
    rules = [(str(i + 1), h, b) for i, (_, h, b) in enumerate(rules[1:3] + rules[:1] + rules[3:])]
    y -= 5
    T(5, y, 'RULES OF THUMB', fontsize=12, fontweight='bold'); y -= 4.5
    for num, head, body in rules:
        ax.add_patch(matplotlib_circle(6.7, y - 1.3, 1.6, YOU))
        T(6.7, y - 0.25, num, color=SURFACE, fontweight='bold', ha='center', fontsize=11)
        T(10, y, head, fontsize=11, fontweight='bold')
        T(10, y - 2.6, _wrap(body, 105), fontsize=9.5, color=INK2, linespacing=1.35)
        y -= 3.4 + 2.2 * (_wrap(body, 105).count('\n') + 1)

    # --- watch-outs
    y -= 1
    T(5, y, 'WATCH OUT FOR', fontsize=12, fontweight='bold'); y -= 4.2
    watch = [
        f'Calibrated in software only: compass deviation (−4° / +1° on the beat headings), vane heel correction, +{cal["offset"]:.2f}° masthead offset, '
        f'{cal["sym"]:.1f}° upwash, leeway {cal["K"]:.1f}×heel/speed². The display is raw: upwind it reads ~7° WIDE on starboard and ~2° NARROW on port. '
        'Calibrate the instruments before steering to the target angles.',
        'ORC\'s angles are through the water; the display\'s TWA is by the bow. We slide 3–5° (most in heavy air), '
        'so a calibrated display reads narrower than ORC\'s angle: the "on display" target row allows for it.',
        'Leeway is fitted from heading vs GPS track, current removed per 10 min. If it is off by 2°, every upwind angle and VMG moves with it. '
        'Wind speed may be off by up to ±10% (reads ~9% higher downwind than upwind).',
        f'Wind speeds on this sheet are at 10 m, as ORC\'s. The masthead display reads ~{(1 / TWS_TO_10M - 1) * 100:.0f}% more: use the small "display" figure under each column. '
        'Wind data updates only every ~5 s: don\'t chase the numbers after a shift.',
        'Light-air numbers are the least certain: in 6–13 kn the vane reads ~4° wider than the compass through tacks (one upwash number for all winds). '
        'If the compass is right, light-air VMG is several points better than shown.',
        'All of this is from 14 crew (Big Boat Series). With our usual 8 there is ~0.5 t less on the rail: expect to reach the heel limits, '
        '"depower" and the reef point ~1–2 kn earlier (estimate, not measured). The heel numbers themselves still apply; light air gets slightly better.',
        'Power and pointing slopes come from moments inside the same 10-minute stretch; part of "more heel" is a gust arriving, so they overstate what trim alone gains.',
    ]
    for w in watch:
        T(6, y, '▲', color=ORC_C, fontsize=10)
        T(10, y, _wrap(w, 108), fontsize=9.5, color=INK2, linespacing=1.35)
        y -= 2.2 * (_wrap(w, 108).count('\n') + 1) + 0.9
    T(5, -100.7, 'Generated by tools/build_polar.py — docs/polar.md has the method and caveats.', fontsize=8, color=INK2)
    p = os.path.join(outdir, 'crib_sheet.png'); plt.savefig(p, dpi=130); plt.close(); return p


def _wrap(s, n):
    import textwrap
    return '\n'.join(textwrap.wrap(s, n))


def matplotlib_rect(x, y, w, h, c):
    from matplotlib.patches import Rectangle
    return Rectangle((x, y), w, h, color=c, lw=0)


def matplotlib_circle(x, y, r, c):
    from matplotlib.patches import Circle
    return Circle((x, y), r, color=c, lw=0)


def calibrate(g):
    """All the calibration steps, in order, printing each fit. Adds stw, lee, twa (through the
    water), twa_bow and tws to g; corrects hdg, awa and aws in place."""
    g = g.copy()
    pw = fit_paddlewheel(g)
    print('\npaddlewheel scale per day (true STW = k * logged BSP):')
    for day, (k, rot, h) in pw.items():
        print(f'  {day}: k={k:.3f}  compass/track rotation {rot:+.1f} deg  ({h:.1f} h)')
    g['stw'] = g.bsp * g.day.map({dd: vv[0] for dd, vv in pw.items()}).fillna(1.0)
    dv = fit_deviation(g)
    up_h = g[race_mask(g) & (np.abs(signed(g.awa)) < 40) & (g.sog > 3)].dropna(subset=['hdg', 'awa'])
    beat = [_cmean(up_h.hdg[np.sign(signed(up_h.awa)) == sd]) for sd in (1, -1)]
    db = [float(deviation(h, dv['coef'])) for h in beat]
    print(f"\ncompass deviation from {dv['hours']:.1f} h of upright sailing ({dv['windows']} windows): "
          + ', '.join(f'{H}: {float(deviation(H, dv["coef"])):+.1f}' for H in range(0, 360, 45)))
    print(f'  race beat headings {beat[0]:.0f} / {beat[1]:.0f}: deviation {db[0]:+.1f} / {db[1]:+.1f} -> '
          f'{(db[1] - db[0]) / 2:+.1f} deg per side would have read as leeway')
    g['hdg'] = (g.hdg + deviation(g.hdg.fillna(0).values, dv['coef'])).where(g.hdg.notna()) % 360
    pw = fit_paddlewheel(g)
    g['stw'] = g.bsp * g.day.map({dd: vv[0] for dd, vv in pw.items()}).fillna(1.0)
    g['awa'], g['aws'] = heel_correct(g.awa, g.aws, g.heel)
    print(f'\nheel-corrected wind angle: {g.awa.notna().mean() * 100:.0f}% of samples have heel '
          f'({race_mask(g)[g.awa.notna().values].sum() / race_mask(g).sum() * 100:.0f}% of race time)')
    lw = fit_leeway(g)
    print(f"leeway = {lw['K']:.1f} x heel / STW^2 from {lw['windows']} two-tack upwind windows ({lw['hours']:.1f} h); "
          'model-free check, constant leeway per heel bin:')
    for lo, hi, L, Lm, mins in lw['bins']:
        print(f'  heel {lo:2d}-{hi:2d} deg: fitted {L:+.2f} deg, model {Lm:.1f} deg  ({mins:.0f} min)')
    cal = fit_awa(g, lw['K'])
    print(f"\nmasthead unit: offset {cal['offset']:+.2f} deg, symmetric upwash {cal['sym']:+.1f} deg "
          f"from {cal['n_tacks']} tacks -> upwind TWA stbd {cal['twa_stbd']:.1f} / port {cal['twa_port']:.1f}, "
          f"residual TWD jump {cal['jump']:+.1f} deg")
    g['lee'] = leeway(g.heel, g.stw, g.awa, lw['K'])
    g['twa'], g['tws'] = true_wind(g.awa, g.aws, g.stw, cal['offset'], cal['sym'], g.lee)
    g['twa_bow'] = g.twa - np.sign(g.twa) * g.lee
    print('\nupwind TWA through the water, per tack side: compass heading change / 2 + leeway vs the vane')
    print(compass_check(g).round(1).to_string())
    g['tws_mast'] = g.tws
    g['tws'] = g.tws * TWS_TO_10M
    print(f'\ntrue wind converted masthead {MASTHEAD_M:.1f} m -> {ORC_REF_M:.0f} m (ORC reference): x{TWS_TO_10M:.3f} '
          f'(exponent {WIND_SHEAR_ALPHA}; 1/7 would be x{(ORC_REF_M / MASTHEAD_M) ** (1 / 7):.3f}). '
          'All TWS below, bands included, are 10 m wind.')
    return g, cal


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--logs', default=os.path.expanduser('~/Documents/typon-nmea-logs'))
    ap.add_argument('--out', default=os.path.join(REPO, 'docs', 'polar'))
    ap.add_argument('--cache', action='store_true', help='reuse tools/polar_out/grid.pkl instead of re-parsing')
    ap.add_argument('--write-js', action='store_true', help='write the ORC table into router.js and route-worker.js')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    cache = os.path.join(REPO, 'tools', 'polar_out', 'grid.pkl')
    if args.cache and os.path.exists(cache):
        g = pd.read_pickle(cache)
    else:
        g = to_grid(parse(args.logs))
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        g.to_pickle(cache)

    g, cal = calibrate(g)

    d = steady_samples(g)
    print(f'\nsteady race beats + runs: {len(d) / 3600:.2f} h')
    v = vs_orc(d)
    pd.set_option('display.width', 220)
    print('\nVS ORC CERTIFICATE')
    print(v.assign(vmg_pct=(v.vmg_pct * 100).round(0), vmg_pct_best=(v.vmg_pct_best * 100).round(0))
          [['sector', 'band', 'minutes', 'vmg_pct', 'vmg_pct_best', 'angle', 'orc_angle', 'lee', 'stw', 'spd_pct',
            'orc_bsp_at_orc_angle']]
          .round(1).to_string(index=False))
    overall = (d.stw / d.orc_bsp_here).median()
    print(f'\nspeed at the angle sailed vs ORC speed at that angle: median {overall * 100:.0f}%; '
          f'VMG vs ORC: median {d.vmg_pct.median() * 100:.0f}%  -> Performance slider default')

    up, hz = helm_analysis(d)
    print(f"\nrudder sensor zero {hz['zero']:+.1f} deg; typical upwind weather helm {hz['typical']:.1f} deg")
    heel_fast = {}
    for s_col, b in zip(CRIB_COLS, ['6-10 kn', '6-10 kn', '10-13 kn', '10-13 kn', '13-16 kn', '16+ kn', '16+ kn']):
        x = up[(up.band == b) & (up.ds > 0)]
        heel_fast[s_col] = f'{x.heel.median():.0f}°' if len(x) > 120 else '–'
    st = stretches(d, 'upwind')
    hi = st[st.d_bow <= -1.5].copy()
    hi['loss'] = pd.cut(-hi.d_stw, [-9, 0.1, 0.2, 0.3, 0.45, 9], labels=['<0.1', '0.1-0.2', '0.2-0.3', '0.3-0.45', '>0.45'])
    print('\nUPWIND, pointing 1.5+ deg higher than its own 10-min stretch: VMG change (kn) by speed lost (kn)')
    print(hi.groupby(['band', 'loss'], observed=True).agg(min=('d_vmg', lambda x: len(x) / 60), vmg=('d_vmg', 'median'))
          .round(2).unstack('loss').to_string())
    floor = speed_floor(st)
    power = power_mode(st)
    held = sustained_best(d)
    print('upwind best VMG held: ' + '; '.join(f'{W // 60} min ' + ', '.join(f'{b} {v_ * 100:.0f}%' for b, v_ in h.items())
                                          for W, h in held.items()))
    print('power (VMG per extra degree of heel, within stretch): '
          + ', '.join(f'{s} kn: {p_[0]} ({p_[1]:+.3f})' if p_ else f'{s} kn: -' for s, p_ in power.items()))
    print('speed floor (groove - %.1f kn): ' % POINT_SPEED_LOSS
          + ', '.join(f'{s} kn: {f[0]:.2f} (groove {f[1]:.2f})' if f else f'{s} kn: -' for s, f in floor.items()))
    m = manoeuvres(g)
    print('\nMANOEUVRES (race): median loss')
    print(m.groupby(['kind', 'band']).agg(n=('lost_m', 'size'), lost_m=('lost_m', 'median'), lost_s=('lost_s', 'median'),
                                          min_kn=('min', 'median'), recover_s=('recover_s', 'median')).round(1).to_string())

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    _style(plt)
    for p in [chart_polar(d, args.out, plt), chart_vs_orc(v, args.out, plt), chart_vmg_angle(vmg_by_angle(d), args.out, plt),
              chart_heel_helm(up, args.out, plt), chart_cheatsheet(v, up, m, heel_fast, floor, power, held, cal, len(d) / 3600, args.out, plt)]:
        print('chart:', os.path.relpath(p, REPO))
    if args.write_js:
        write_js(orc_table())


if __name__ == '__main__':
    main()
