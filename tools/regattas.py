"""Regattas: the one place races are defined (tools/regattas.json), plus local <-> UTC time.

Every analysis tool reads races from here, so a new regatta is one JSON entry, not edits in four
scripts. Times in the file are local wall-clock in the file's timezone; conversion goes through
zoneinfo, so PDT and PST are both right (a fixed -7 h offset was an hour wrong from November).
No heavy imports: build_polar.py imports this.
"""
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'regattas.json')
_doc = None


def _load():
    global _doc
    if _doc is None:
        with open(PATH) as f:
            _doc = json.load(f)
    return _doc


def tz():
    return ZoneInfo(_load()['timezone'])


def regattas():
    return _load()['regattas']


def regatta(rid):
    for r in regattas():
        if r['id'] == rid:
            return r
    raise SystemExit(f'no regatta {rid!r} in {PATH}')


def races(rid=None):
    """(regatta, race) pairs, for one regatta or all of them, in file order."""
    return [(g, r) for g in regattas() if rid in (None, g['id']) for r in g['races']]


def find_race(race_id, rid=None):
    hits = [(g, r) for g, r in races(rid) if r['id'] == race_id]
    if not hits:
        raise SystemExit(f'no race {race_id!r}' + (f' in {rid}' if rid else '') + f' ({PATH})')
    if len(hits) > 1:
        raise SystemExit(f'race {race_id!r} is in several regattas; pass --regatta ({", ".join(g["id"] for g, _ in hits)})')
    return hits[0]


def windows():
    """All sailing windows, local, as (start, end) strings: each regatta's `windows`, or its races."""
    out = []
    for g in regattas():
        if g.get('windows'):
            out += [tuple(w) for w in g['windows']]
        else:
            out += [(f"{r['day']} {r['start']}", f"{r['day']} {r['finish']}") for r in g['races']]
    return out


def local_to_utc_s(day, hms):
    """Epoch seconds for a local wall-clock time, e.g. ('2026-09-19', '13:00:00')."""
    t = datetime.fromisoformat(f'{day} {hms}').replace(tzinfo=tz())
    return int(t.timestamp())


def utc_s_to_local(t):
    """Epoch seconds (scalar, array, Index or Series) -> naive local timestamps, same shape."""
    if pd.api.types.is_scalar(t):
        return pd.Timestamp(t, unit='s', tz='UTC').tz_convert(tz()).tz_localize(None)
    x = pd.to_datetime(t, unit='s', utc=True)
    if isinstance(x, pd.Series):
        return x.dt.tz_convert(tz()).dt.tz_localize(None)
    return pd.DatetimeIndex(x).tz_convert(tz()).tz_localize(None)
