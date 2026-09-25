#!/usr/bin/env python3
"""Sea state from the boat's own motion, and whether it explains our upwind % of ORC.

    python3 tools/sea_state.py            # parses pitch once into tools/polar_out/pitch.pkl
    python3 tools/sea_state.py --cache    # reuse it

There is no wave sensor on board, but the heading unit logs attitude ($YXXDR Yaw/Pitch/Roll) at
~20 Hz. Pitch is the wave signal: roll mixes in gust heel and steering, pitch mostly doesn't.
Per window:
  * pitch spread = std of pitch after removing a 20 s rolling mean (trim changes, heel-induced
    pitch), degrees. A proxy for wave steepness as we meet it, not a height in metres: it depends
    on our heading to the waves and our speed;
  * peak period = the strongest period between 1.5 and 12 s in the pitch spectrum. It is the
    ENCOUNTER period: upwind into chop it is shorter than the waves' own period.

The test (docs/polar.md §8): chop grows with wind, and our % of ORC already falls in heavy air, so
a raw correlation would blame the waves for the wind. So upwind 5-min windows are compared at the
same wind (within a band, high vs low pitch; and a least-squares fit of % on TWS and pitch together).
Wowla, from static/races/bbs2026.json (AIS, scored the same way), is the control: same water,
same minutes. If chop costs us and not them, it is us, not the sea.
"""
import argparse, glob, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_polar as bp
import race_tracks as rt

FS = 10                     # resample rate, Hz
DETREND_S = 20
WINDOW_S = 300
PERIOD_BAND_S = (1.5, 12.0)
CACHE = os.path.join(bp.REPO, 'tools', 'polar_out', 'pitch.pkl')
LOGS = os.path.expanduser('~/Documents/typon-nmea-logs')


def parse_pitch():
    """10 Hz pitch for every race window in race_tracks.RACES (epoch-s index)."""
    out = []
    for rid, _, _, day, start, finish, _ in rt.RACES:
        t0, t1 = rt._utc_s(day, start), rt._utc_s(day, finish)
        rows = []
        for f in sorted(glob.glob(os.path.join(LOGS, f'nmea_{day}_*.txt'))):
            for line in open(f, errors='replace'):
                if '$YXXDR' not in line or 'Pitch' not in line:
                    continue
                try:
                    t = pd.Timestamp(line[:23]).value / 1e9
                    if t < t0 - 60 or t > t1 + 60:
                        continue
                    p = line[26:].split('*')[0].split(',')
                    rows.append((t, float(p[6])))
                except (ValueError, IndexError):
                    pass
        d = pd.DataFrame(rows, columns=['t', 'pitch']).sort_values('t').drop_duplicates('t')
        d = d[d.pitch.diff() != 0]            # the unit repeats readings; keep the changes
        tt = np.arange(t0, t1, 1 / FS)
        out.append(pd.DataFrame({'t': tt, 'pitch': np.interp(tt, d.t.values, d.pitch.values), 'race': rid}))
        print(f'{rid}: {len(d)} pitch readings -> {len(tt)} samples at {FS} Hz', file=sys.stderr)
    return pd.concat(out, ignore_index=True)


def sea_state(p):
    """(pitch spread deg, peak encounter period s) for a 10 Hz pitch array; NaN if too short."""
    if len(p) < 60 * FS:
        return np.nan, np.nan
    d = p - pd.Series(p).rolling(DETREND_S * FS, center=True, min_periods=1).mean().values
    n = len(d)
    f = np.fft.rfftfreq(n, 1 / FS)
    S = np.abs(np.fft.rfft(d * np.hanning(n))) ** 2
    band = (f > 1 / PERIOD_BAND_S[1]) & (f < 1 / PERIOD_BAND_S[0])
    # Smooth the periodogram a little so one bin of noise isn't the "peak".
    Sb = pd.Series(S[band]).rolling(5, center=True, min_periods=1).mean().values
    return float(d.std()), float(1 / f[band][np.argmax(Sb)])


def leg_state(P, t0, t1):
    """Sea state over [t0, t1] (epoch s) from the parsed pitch table."""
    x = P[(P.t >= t0) & (P.t < t1)]
    return sea_state(x.pitch.values)


def windows(P, races):
    """Upwind 5-min windows: sea state, TWS, Typon and Wowla median upwind % of ORC."""
    rows = []
    for race in races:
        rid = race['id'].split('-')[-1]
        boats = {b['name']: pd.DataFrame(b['pts'], columns=rt.FIELDS) for b in race['boats']}
        x = P[P.race == rid]
        for w0 in np.arange(race['start'] / 1000, race['finish'] / 1000 - WINDOW_S, WINDOW_S):
            w1 = w0 + WINDOW_S
            ty = boats['Typon']
            ty = ty[(ty.t >= w0) & (ty.t < w1)]
            up = ty[(ty['mode'] == 'u') & ty.pct.notna()]
            if len(up) < 0.6 * len(ty) or len(up) < 20:   # mostly upwind, mostly scored
                continue
            spread, period = sea_state(x[(x.t >= w0) & (x.t < w1)].pitch.values)
            wo = boats.get('Wowla')
            wu = None
            if wo is not None:
                wo = wo[(wo.t >= w0) & (wo.t < w1) & (wo['mode'] == 'u') & wo.pct.notna()]
                wu = wo.pct.median() if len(wo) >= 4 else None
            rows.append(dict(race=rid, t=w0, tws=up.tws.median(), pitch=spread, period=period,
                             typon=up.pct.median(), wowla=wu))
    return pd.DataFrame(rows)


def fit(W, col):
    """% = a + b*TWS + c*pitch, least squares; returns (b, c, n)."""
    d = W.dropna(subset=[col, 'tws', 'pitch'])
    if len(d) < 8:
        return np.nan, np.nan, len(d)
    A = np.c_[np.ones(len(d)), d.tws, d.pitch]
    coef, *_ = np.linalg.lstsq(A, d[col].values.astype(float), rcond=None)
    return coef[1], coef[2], len(d)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', action='store_true')
    args = ap.parse_args()
    if args.cache and os.path.exists(CACHE):
        P = pd.read_pickle(CACHE)
    else:
        P = parse_pitch()
        P.to_pickle(CACHE)
    import json
    races = json.load(open(rt.OUT))['races']
    W = windows(P, races)
    pd.set_option('display.width', 200)
    W['band'] = pd.cut(W.tws, [b[0] for b in bp.BANDS] + [bp.BANDS[-1][1]], labels=bp.BAND_NAMES, right=False)

    print(f'\nUPWIND 5-min windows: {len(W)} ({len(W) * WINDOW_S / 3600:.1f} h), wind at 10 m')
    print('\nsea state by race (median over upwind windows):')
    print(W.groupby('race').agg(windows=('t', 'size'), tws=('tws', 'median'), pitch=('pitch', 'median'),
                                period=('period', 'median'), typon=('typon', 'median'), wowla=('wowla', 'median'))
          .round(1).to_string())

    print('\nsame wind band, calmer vs rougher half (split at the band\'s median pitch):')
    rows = []
    for b, d in W.groupby('band', observed=True):
        if len(d) < 6:
            continue
        m = d.pitch.median()
        for half, h in (('calmer', d[d.pitch <= m]), ('rougher', d[d.pitch > m])):
            rows.append(dict(band=b, half=half, windows=len(h), pitch=h.pitch.median(), period=h.period.median(),
                             typon=h.typon.median(), wowla=h.wowla.median(), wowla_n=h.wowla.notna().sum()))
    print(pd.DataFrame(rows).round(1).to_string(index=False))

    for col in ('typon', 'wowla'):
        b, c, n = fit(W, col)
        print(f'\n{col}: % of ORC upwind = ... {b:+.2f} per kn TWS {c:+.1f} per degree of pitch spread  (n={n} windows)')
    print('\ncorrelation of pitch spread with TWS: '
          f'{W[["pitch", "tws"]].corr().iloc[0, 1]:+.2f} (high = the two are hard to separate)')


if __name__ == '__main__':
    main()
