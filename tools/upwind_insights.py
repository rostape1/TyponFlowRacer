#!/usr/bin/env python3
"""Upwind insights: the follow-up analyses behind docs/polar.md §4 and §9, rerunnable on any regatta.

    python3 tools/upwind_insights.py                  # all sections, every regatta in tools/regattas.json
    python3 tools/upwind_insights.py --only gusts,best
    python3 tools/upwind_insights.py --control Wowla  # rival used as the same-water control

Needs tools/polar_out/grid.pkl (build_polar.py), static/races/*.json (race_tracks.py) and, for the
sea-state sections, tools/polar_out/pitch.pkl (sea_state.py). Sections:

  leeway   leeway fitted per heel range without the formula, with a bootstrap range, vs the model;
           and what each extra degree of heel costs
  helm     heel / weather helm / VMG by wind band; within 16+ kn by helm and by heel
  gusts    upwind VMG %, speed and heel in gusts vs lulls vs steady, per wind band (10 s wind
           against its own 3-min base; not within 45 s of a tack)
  tacks    time lost per tack vs the sea state before it, at the same wind
  gate     upwind % of ORC inside vs outside the Golden Gate (GATE_LON), for us and the control
  best     best vs worst 2-min upwind blocks, each compared only with the same tack and wind band:
           speed, angle, heel, helm, steering, sea state, location; plus the best moments to replay
  sides    port vs starboard for us and the control: a gap both share is our wind/current
           reference, not our sailing
  wind     masthead wind speed, downwind vs upwind, from both mark types: an instrument error flips
           sign between windward and leeward roundings, a building breeze does not

The Gate longitude and the wind bands are SF Bay specifics; everything else is general.
"""
import argparse, contextlib, io, math, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_polar as bp
import race_review as rr
import race_tracks as rt
import sea_state as ss

GATE_LON = -122.478          # Golden Gate Bridge: west of it = outside
BLOCK_S = 120
BANDS4 = ([6, 10, 13, 16, 25], ['6-10', '10-13', '13-16', '16+'])
pd.set_option('display.width', 240)


def band(x):
    return pd.cut(x, BANDS4[0], labels=BANDS4[1], right=False)


def calibrated():
    g = bp.load_grid()
    with contextlib.redirect_stdout(io.StringIO()):
        g, cal = bp.calibrate(g)
    return g, cal


# ---------------------------------------------------------------- leeway per heel range
def sec_leeway(g, cal):
    print('\n=== LEEWAY per heel range (heading vs GPS track, both tacks, current per 10 min) ===')
    # the fit needs the heading and paddlewheel corrected but not the leeway: redo calibrate()'s first steps
    g2 = bp.load_grid()
    with contextlib.redirect_stdout(io.StringIO()):
        pw = bp.fit_paddlewheel(g2); g2['stw'] = g2.bsp * g2.day.map({d: v[0] for d, v in pw.items()}).fillna(1.0)
        dv = bp.fit_deviation(g2)
        g2['hdg'] = (g2.hdg + bp.deviation(g2.hdg.fillna(0).values, dv['coef'])).where(g2.hdg.notna()) % 360
        pw = bp.fit_paddlewheel(g2); g2['stw'] = g2.bsp * g2.day.map({d: v[0] for d, v in pw.items()}).fillna(1.0)
        g2['awa'], g2['aws'] = bp.heel_correct(g2.awa, g2.aws, g2.heel)
    d = g2.dropna(subset=['sog', 'cog', 'hdg', 'stw', 'heel', 'awa'])
    d = d[(d.sog > 2.5) & (d.stw > 2.5) & (np.abs(bp.signed(d.awa)) < 60)].copy()
    d['s'] = np.sign(bp.signed(d.awa)); d['V'] = d.sog * np.exp(1j * np.deg2rad(90 - d.cog)); d['win'] = d.index // 600
    c = d.groupby(['win', 's']).size().unstack(fill_value=0)
    d = d[d.win.isin(c[(c.get(1, 0) >= 90) & (c.get(-1, 0) >= 90)].index)]

    def cost(x, L):
        r = x.V - x.stw * np.exp(1j * np.deg2rad(90 - x.hdg + x.s * L))
        return (np.abs(r - r.groupby(x.win).transform('mean')) ** 2).mean()

    def fitbin(x):
        return min(np.arange(-4, 14.01, 0.25), key=lambda L: cost(x, L))

    K = cal['K']
    print(f'model: leeway = {bp.leeway_str(K)}')
    print('heel     min  fitted  model   STW   bootstrap 10-90%')
    rng = np.random.default_rng(0)
    for lo, hi in [(0, 15), (15, 20), (20, 23), (23, 26), (26, 28), (28, 30), (30, 34)]:
        x = d[(d.heel.abs() >= lo) & (d.heel.abs() < hi)]
        if len(x) < 900:
            print(f'{lo:2d}-{hi:2d}°  {len(x) / 60:4.0f}  too few minutes'); continue
        L = fitbin(x); m = float(bp.leeway(x.heel, x.stw, x.awa, K).median())
        wins = x.win.unique()
        bs = [fitbin(pd.concat([x[x.win == w] for w in rng.choice(wins, len(wins))])) for _ in range(30)]
        print(f'{lo:2d}-{hi:2d}°  {len(x) / 60:4.0f}  {L:+5.2f}°  {m:4.1f}°  {x.stw.median():.2f}  {np.percentile(bs, 10):+.1f}..{np.percentile(bs, 90):+.1f}°')
    print('each extra degree of heel (model, 6.4 kn, TWA 40):')
    for h, L, dL, dv in bp.leeway_per_degree(K, heels=(15, 20, 22, 25, 28, 30, 32)):
        print(f'  at {h}°: leeway {L:.1f}°, +1° heel -> +{dL:.2f}° leeway = {-dv:+.3f} kn VMG')


# ---------------------------------------------------------------- heel and helm
def sec_helm(g, cal):
    print('\n=== HEEL / HELM / VMG, steady upwind race sailing ===')
    with contextlib.redirect_stdout(io.StringIO()):
        d = bp.steady_samples(g)
    up, hz = bp.helm_analysis(d)
    u = up.copy(); u['heel'] = u.heel.abs()
    print(f'rudder zero {hz["zero"]:+.1f}°')
    print(u.groupby('band', observed=True).agg(min=('stw', lambda v: len(v) / 60), vmg_pct=('vmg_pct', lambda v: v.median() * 100),
          heel=('heel', 'median'), over28_pct=('heel', lambda v: (v > 28).mean() * 100), helm=('whelm', 'median'),
          atwa=('atwa', 'median'), orc_angle=('orc_angle', 'median')).round(1).to_string())
    h = u[u.band == bp.BAND_NAMES[-1]].copy()
    if len(h) > 300:
        h['helm_q'] = pd.qcut(h.whelm, 3, labels=['light helm', 'mid', 'heavy helm'])
        h['heel_q'] = pd.cut(h.heel, [0, 22, 26, 28, 90], labels=['<22', '22-26', '26-28', '>28'])
        for k in ('helm_q', 'heel_q'):
            print(f'\n{bp.BAND_NAMES[-1]} by {k[:-2]}:')
            print(h.groupby(k, observed=True).agg(min=('stw', lambda v: len(v) / 60), helm=('whelm', 'median'), heel=('heel', 'median'),
                  vmg_pct=('vmg_pct', lambda v: v.median() * 100), stw=('stw', 'median'), atwa=('atwa', 'median'),
                  lee=('lee', 'median')).round(2).to_string())


# ---------------------------------------------------------------- gusts
def sec_gusts(g, cal):
    print('\n=== GUSTS vs LULLS, upwind (not within 45 s of a tack) ===')
    x = g[bp.race_mask(g)].copy(); x['a'] = x.twa.abs()
    side = np.sign(x.twa.rolling(21, center=True).median())
    near = (side.diff().abs() == 2).astype(int).rolling(90, center=True, min_periods=1).max() > 0
    x['ref'] = x.tws.rolling(180, center=True, min_periods=60).median()
    x['gust'] = x.tws.rolling(10, center=True, min_periods=5).mean() - x.ref
    u = x[(x.a <= 55) & (x.stw > 2) & ~near].dropna(subset=['stw', 'tws', 'ref', 'gust', 'heel'])
    u['vmg_pct'] = [s * bp.cosd(a) / bp.orc_beat(w)[0] * 100 for s, a, w in zip(u.stw, u.a, u.tws)]
    u['band'] = band(u.ref); u['heel'] = u.heel.abs()
    u['state'] = pd.cut(u.gust, [-20, -1.5, 1.5, 20], labels=['lull', 'steady', 'gust'])
    print(u.groupby(['band', 'state'], observed=True).agg(min=('stw', lambda v: len(v) / 60), vmg_pct=('vmg_pct', 'median'),
          stw=('stw', 'median'), twa=('a', 'median'), heel=('heel', 'median'), over28_pct=('heel', lambda v: (v > 28).mean() * 100))
          .round(1).to_string())
    print('(gust/lull VMG % is biased by inertia: the boat has not yet sped up / slowed down; heel is not)')


# ---------------------------------------------------------------- tacks vs waves
def sec_tacks(g, cal, P):
    print('\n=== TACK LOSS vs SEA STATE (3 min before the tack), same wind ===')
    rows, last = [], -1e9
    twd = (g.hdg + g.twa_bow) % 360
    side = np.sign(g.twa.rolling(21, center=True).median())
    for t in g.index[(side.diff().abs() == 2).values & bp.race_mask(g)]:
        if t - last < 90: continue
        b, a, after = g.loc[t - 75:t - 15], g.loc[t - 15:t + 60], g.loc[t + 60:t + 120]
        if b.stw.isna().mean() > .2 or a.stw.isna().mean() > .2 or len(b) < 50: continue
        if not (np.abs(b.twa).median() < 70 and np.abs(after.twa).median() < 70): continue
        wd = np.deg2rad(bp._cmean(twd.loc[t - 75:t - 15].dropna()))
        vm = lambda s: (s.stw * np.cos(np.deg2rad(s.hdg) - wd)).fillna(0)
        base = vm(b).median()
        if base <= 0: continue
        lost_nm = base * 75 / 3600 - vm(a).sum() / 3600
        spread, period = ss.leg_state(P, t - 180, t - 10)
        rows.append(dict(t=t, tws=b.tws.median(), lost_s=lost_nm / (base / 3600), pitch=spread))
        last = t
    T = pd.DataFrame(rows).dropna(subset=['pitch'])
    if len(T) < 10:
        print('too few tacks with sea state'); return
    T['band'] = band(T.tws)
    for bnd, d in T.groupby('band', observed=True):
        if len(d) < 6: continue
        m = d.pitch.median(); lo, hi = d[d.pitch <= m], d[d.pitch > m]
        print(f'  {bnd} kn: calmer {lo.pitch.median():.2f}° -> {lo.lost_s.median():.0f} s | rougher {hi.pitch.median():.2f}° -> {hi.lost_s.median():.0f} s  (n {len(lo)}/{len(hi)})')
    c, *_ = np.linalg.lstsq(np.c_[np.ones(len(T)), T.tws, T.pitch], T.lost_s.values, rcond=None)
    print(f'fit ({len(T)} tacks): lost_s = {c[0]:.0f} {c[1]:+.2f}/kn {c[2]:+.1f}/deg pitch spread')


# ---------------------------------------------------------------- blocks from the race tracks
def track_blocks(races, P):
    """2-min upwind blocks per boat from static/races (what the replay shows)."""
    out = []
    for race in races:
        x = P[P.race == race['id']] if P is not None else None
        for b in race['boats']:
            d = pd.DataFrame(b['pts'], columns=rt.FIELDS)
            d = d[(d['mode'] == 'u') & d.pct.notna()]
            d['blk'] = (d.t // BLOCK_S).astype(int)
            for blk, s in d.groupby('blk'):
                if len(s) < (12 if b['source'] == 'instruments' else 2): continue
                row = dict(race=race['id'], boat=b['name'], zone='outside' if s.lon.median() < GATE_LON else 'inside',
                           pct=s.pct.median(), tws=s.tws.median(), twa=s.twa.median(), tgt=s.tgt_twa.median(),
                           spd=s.spd.median(), tgt_spd=s.tgt_spd.median(), t=blk * BLOCK_S)
                if b['name'] == 'Typon' and x is not None:
                    row['pitch'], row['period'] = ss.sea_state(x[(x.t >= blk * BLOCK_S - 60) & (x.t < blk * BLOCK_S + BLOCK_S + 60)].pitch.values)
                out.append(row)
    return pd.DataFrame(out)


def sec_gate(races, P, control):
    print(f'\n=== INSIDE vs OUTSIDE the Gate (lon {GATE_LON}), upwind 2-min blocks ===')
    D = track_blocks(races, P)
    if control:
        D = D[D.boat.isin(['Typon', control])]
    agg = D.groupby(['boat', 'zone']).agg(min=('t', lambda v: len(v) * BLOCK_S / 60), tws=('tws', 'median'), pct=('pct', 'median'),
          twa=('twa', 'median'), tgt=('tgt', 'median'), spd=('spd', 'median'), tgt_spd=('tgt_spd', 'median'),
          pitch=('pitch', 'median'))
    print(agg.round(1).to_string())
    F = D[D.boat == 'Typon'].copy(); F['band'] = band(F.tws)
    print('\nTypon, same wind band:')
    print(F.groupby(['band', 'zone'], observed=True).agg(min=('t', lambda v: len(v) * 2), pct=('pct', 'median'), pitch=('pitch', 'median'),
          twa=('twa', 'median'), tgt=('tgt', 'median'), spd=('spd', 'median'), tgt_spd=('tgt_spd', 'median')).round(1).to_string())


# ---------------------------------------------------------------- best vs worst blocks
def sec_best(g, cal, P):
    print('\n=== BEST vs WORST upwind 2-min blocks (vs the same tack and wind band) ===')
    with contextlib.redirect_stdout(io.StringIO()):
        d = bp.steady_samples(g)
    up, _ = bp.helm_analysis(d)
    g = g.copy(); g['twd'] = (g.hdg + g.twa_bow) % 360
    g['lift'] = bp.signed(g.twd - rt._circ_roll(g.twd.dropna(), 601).reindex(g.index)) * np.sign(g.twa)
    up = up.join(g[['lift', 'hdg']]); up['bow'] = up.atwa - up.lee
    pos = []
    for r in rt.load_tracks():
        pos += [(p[0], p[1], p[2], r['id']) for p in r['boats'][0]['pts']]
    pos = pd.DataFrame(pos, columns=['t', 'lat', 'lon', 'race']).set_index('t').sort_index()
    up['blk'] = (up.index // BLOCK_S) * BLOCK_S
    B = up.groupby('blk').agg(n=('stw', 'size'), vmg_pct=('vmg_pct', 'median'), tws=('tws', 'median'), stw=('stw', 'median'),
        atwa=('atwa', 'median'), bow=('bow', 'median'), lee=('lee', 'median'), heel=('heel', 'median'), helm=('whelm', 'median'),
        rud_sd=('rud_sd', 'median'), tack=('tack', 'median'), lift=('lift', 'median'), local=('local', 'first'))
    B = B[B.n >= 60].copy(); B['vmg_pct'] *= 100
    ix = pos.index.values; k = np.clip(np.searchsorted(ix, B.index.values + 60), 0, len(ix) - 1)
    B['lon'], B['race'] = pos.lon.values[k], pos.race.values[k]
    B['zone'] = np.where(B.lon < GATE_LON, 'outside', 'inside')
    B['pitch'] = [ss.sea_state(P[(P.t >= b) & (P.t < b + BLOCK_S)].pitch.values)[0] if P is not None else np.nan for b in B.index]
    B['tackn'] = np.where(B.tack > 0, 'stbd', 'port')
    B['grp'] = band(B.tws).astype(str) + B.tackn
    B['rel'] = B.vmg_pct - B.groupby('grp').vmg_pct.transform('median')
    cols = ['stw', 'atwa', 'bow', 'lee', 'heel', 'helm', 'rud_sd', 'pitch', 'lift', 'tws']
    for c in cols:
        B[c + '_r'] = B[c] - B.groupby('grp')[c].transform('median')
    B['wind'] = np.where(B.tws < 13, 'under 13 kn', '13+ kn')
    rows = []
    for w, x in B.groupby('wind'):
        q1, q3 = x.rel.quantile([.25, .75])
        for name, y in (('BEST 25%', x[x.rel >= q3]), ('WORST 25%', x[x.rel <= q1])):
            r = dict(wind=w, group=name, blocks=len(y), vmg_vs_same=y.rel.median(), outside_pct=(y.zone == 'outside').mean() * 100)
            r.update({c: y[c + '_r'].median() for c in cols})
            rows.append(r)
    print(f'{len(B)} blocks; columns are differences from the median block of the same tack and wind band')
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    best = B.sort_values('rel', ascending=False).head(12).copy()
    best['time'] = best.local.dt.strftime('%a %d %b %H:%M')
    print('\nbest moments (replay: pick the race, click the track at that time):')
    print(best[['time', 'race', 'tackn', 'zone', 'tws', 'vmg_pct', 'stw', 'atwa', 'bow', 'heel', 'helm']].round(1).to_string(index=False))


# ---------------------------------------------------------------- port vs starboard
def sec_sides(g, cal, control):
    print('\n=== PORT vs STARBOARD, us and the control (both scored against our wind) ===')
    gg = rt.own_frame(g)
    rows = []
    for R in rt.race_list():
        day = R['day']; t0, t1 = rt._utc_s(day, R['start']), rt._utc_s(day, R['finish'])
        name = control or next(iter(R['rivals']), None)
        if name and name in R['rivals']:
            mmsi, fin = R['rivals'][name]
            O, A = rr.read_positions(day, {mmsi})
            t_fin = rt._utc_s(day, fin) if fin else t1
            cert = rr.CERTS[name.upper()]
            r = A[(A.mmsi == mmsi) & A.sog.notna()].copy(); r['s'] = np.floor(r.t).astype(np.int64)
            for s, a in r[(r.s >= t0) & (r.s <= t_fin)].groupby('s').first().iterrows():
                if s not in gg.index or a.sog < 2: continue
                o = gg.loc[s]
                if not (np.isfinite(o.cur_e) and np.isfinite(o.twd_s) and np.isfinite(o.tws_s)) or o.tws_s < 4: continue
                near = O.index[np.clip(O.index.searchsorted(s), 0, len(O) - 1)]
                if rr._nm(a.lat, a.lon, O.at[near, 'lat'], O.at[near, 'lon']) > rt.AIS_MAX_FROM_US_NM: continue
                c = math.radians(a.cog); e, n = a.sog * math.sin(c) - o.cur_e, a.sog * math.cos(c) - o.cur_n
                twa = float(bp.signed(o.twd_s - math.degrees(math.atan2(e, n)) % 360))
                if abs(twa) > 55: continue
                rows.append(dict(boat=name, tack='stbd' if twa > 0 else 'port', tws=o.tws_s, twa=abs(twa),
                                 pct=math.hypot(e, n) * bp.cosd(abs(twa)) / bp.orc_beat(o.tws_s, cert)[0] * 100))
        x = gg.loc[t0:t1]; x = x[(x.twa.abs() <= 55) & (x.stw > 2) & x.tws.notna()]
        for s, o in x.iloc[::10].iterrows():
            rows.append(dict(boat='Typon', tack='stbd' if o.twa > 0 else 'port', tws=o.tws, twa=abs(o.twa),
                             pct=o.stw * bp.cosd(abs(o.twa)) / bp.orc_beat(o.tws)[0] * 100))
    D = pd.DataFrame(rows); D['band'] = pd.cut(D.tws, [4, 10, 13, 16, 25], labels=['<10', '10-13', '13-16', '16+'], right=False)
    print(D.groupby(['band', 'boat', 'tack'], observed=True).agg(n=('pct', 'size'), pct=('pct', 'median'), twa=('twa', 'median'))
          .unstack('tack').round(1).to_string())


# ---------------------------------------------------------------- wind-speed asymmetry
def sec_wind(g, cal):
    print('\n=== MASTHEAD WIND SPEED: downwind vs upwind, from both mark types ===')
    rows = []
    for R in rt.race_list():
        t0, t1 = rt._utc_s(R['day'], R['start']), rt._utc_s(R['day'], R['finish'])
        x = g.loc[t0:t1].dropna(subset=['stw', 'twa', 'tws'])
        legs = rr.split_legs(x)
        for a, b in zip(legs[:-1], legs[1:]):
            t = b.index[0]
            pre, post = g.loc[t - 150:t - 30], g.loc[t + 45:t + 165]
            if pre.tws.notna().sum() < 60 or post.tws.notna().sum() < 60: continue
            tb, ta = pre.twa.abs().median(), post.twa.abs().median()
            kind = 'windward' if tb < 60 and ta > 120 else 'leeward' if tb > 120 and ta < 60 else None
            if kind:
                rows.append(dict(race=R['id'], kind=kind, jump=(post.tws.median() / pre.tws.median() - 1) * 100,
                                 heel_up=(pre if kind == 'windward' else post).heel.abs().median()))
    D = pd.DataFrame(rows)
    w, l = D[D.kind == 'windward'].jump, D[D.kind == 'leeward'].jump
    if len(w) < 3 or len(l) < 3:
        print('too few roundings of each kind'); return
    print(f'beat->run: n={len(w)} median {w.median():+.1f}%   run->beat: n={len(l)} median {l.median():+.1f}%')
    print(f'-> downwind reads {(w.median() - l.median()) / 2:+.1f}% vs upwind (instrument); breeze trend {(w.median() + l.median()) / 2:+.1f}% per rounding')
    rng = np.random.default_rng(1)
    bs = [(np.median(rng.choice(w.values, len(w))) - np.median(rng.choice(l.values, len(l)))) / 2 for _ in range(2000)]
    print(f'   80% range {np.percentile(bs, 10):+.1f}..{np.percentile(bs, 90):+.1f}%')
    D['dv'] = np.where(D.kind == 'windward', D.jump, -D.jump)
    D['heel_b'] = pd.cut(D.heel_up, [0, 15, 22, 40], labels=['<15°', '15-22°', '>22°'])
    print('by upwind heel (a heel-driven under-read upwind would rise with heel):')
    print(D.groupby('heel_b', observed=True).dv.agg(['size', 'median']).round(1).to_string())


SECTIONS = ['leeway', 'helm', 'gusts', 'tacks', 'gate', 'best', 'sides', 'wind']


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', help='comma-separated sections: ' + ','.join(SECTIONS))
    ap.add_argument('--control', help='rival for gate/sides/tacks comparisons (default: each race\'s first AIS rival)')
    args = ap.parse_args()
    want = args.only.split(',') if args.only else SECTIONS
    g, cal = calibrated()
    P = pd.read_pickle(ss.CACHE) if os.path.exists(ss.CACHE) else None
    if P is None and {'tacks', 'gate', 'best'} & set(want):
        print('(no tools/polar_out/pitch.pkl: run tools/sea_state.py first for the sea-state columns)')
    races = rt.load_tracks()
    for s in want:
        if s == 'leeway': sec_leeway(g, cal)
        elif s == 'helm': sec_helm(g, cal)
        elif s == 'gusts': sec_gusts(g, cal)
        elif s == 'tacks' and P is not None: sec_tacks(g, cal, P)
        elif s == 'gate': sec_gate(races, P, args.control)
        elif s == 'best': sec_best(g, cal, P)
        elif s == 'sides': sec_sides(g, cal, args.control)
        elif s == 'wind': sec_wind(g, cal)


if __name__ == '__main__':
    main()
