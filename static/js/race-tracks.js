/**
 * Race tracks coloured by % of each boat's own ORC certificate, for the replay's race picker.
 *
 * Data: static/races/<series>.json, written by tools/race_tracks.py. The scoring (VMG upwind and
 * downwind, speed on a reach) and the whole calibration chain live there, in Python, so the map
 * shows the same numbers as docs/polar.md and the crib sheet. Nothing is recomputed here.
 *
 * The tracks are drawn whole and independent of the NMEA store: a race spans several hourly
 * recordings and the store resets at each one, but these lines do not.
 */
class RaceTracks {
    static get RED_PCT() { return 85; }      // at or below: red
    static get GREEN_PCT() { return 97; }    // at or above: green
    static get UNSCORED() { return '#7f8c8d'; }
    static get GAP_BREAK_S() { return 120; }  // a longer hole in the data breaks the line
    static get SERIES_URL() { return 'races/bbs2026.json'; }
    static get ON_TARGET_DEG() { return 1.5; }
    // How far from a boat's nearest point the live box still reads it: own track is every 5 s,
    // AIS every ~30 s.
    static get NOW_TOL_S() { return { instruments: 10, ais: 45 }; }

    /** Red at 85% and below, green at 97% and above, through yellow in between; grey if unscored. */
    static colorFor(pct) {
        if (pct == null || !Number.isFinite(pct)) return RaceTracks.UNSCORED;
        const f = Math.min(1, Math.max(0, (pct - RaceTracks.RED_PCT) / (RaceTracks.GREEN_PCT - RaceTracks.RED_PCT)));
        return `hsl(${Math.round(120 * f)}, 85%, 48%)`;
    }

    /**
     * Consecutive points of the same colour, as polylines. Each run starts at the previous run's
     * last point so the line has no gaps, except across a hole in the data longer than GAP_BREAK_S.
     * pts rows: [t, lat, lon, pct, ...].
     */
    static segments(pts) {
        const out = [];
        let cur = null, prev = null;
        for (const p of pts) {
            const color = RaceTracks.colorFor(p[3]);
            const broken = prev && p[0] - prev[0] > RaceTracks.GAP_BREAK_S;
            if (!cur || broken || color !== cur.color) {
                cur = { color, latlngs: (prev && !broken) ? [[prev[1], prev[2]]] : [] };
                out.push(cur);
            }
            cur.latlngs.push([p[1], p[2]]);
            prev = p;
        }
        return out.filter(s => s.latlngs.length > 1);
    }

    /**
     * The recording holding moment `ms`: the latest one starting at or before it, and only if it
     * starts within maxSpanMs (a recording is an hour, a logger restart can make it a little more).
     * Filenames are local time; NmeaClient.logStartTime reads them that way.
     */
    static fileForTime(files, ms, maxSpanMs = 2 * 3600 * 1000) {
        let best = null, bestStart = -Infinity;
        for (const f of files || []) {
            const s = NmeaClient.logStartTime(f.name);
            if (s == null || s > ms || ms - s > maxSpanMs) continue;
            if (s > bestStart) { best = f; bestStart = s; }
        }
        return best;
    }

    /** The point nearest a map position, by flat-earth distance (fine at race scale). */
    static nearest(pts, lat, lon) {
        const k = Math.cos(lat * Math.PI / 180);
        let best = null, bd = Infinity;
        for (const p of pts) {
            const d = (p[1] - lat) ** 2 + ((p[2] - lon) * k) ** 2;
            if (d < bd) { bd = d; best = p; }
        }
        return best;
    }

    /**
     * How the angle sailed compares with the ORC target: upwind a smaller TWA is higher (pinching),
     * downwind a smaller TWA is higher (hotter). Within ON_TARGET_DEG it is on target. On a reach
     * the course sets the angle, so there is no verdict. Returns null when there is nothing to say.
     */
    static angleVerdict(p) {
        const twa = p[4], tgt = p[8], mode = p[7];
        if (twa == null || tgt == null || (mode !== 'u' && mode !== 'd')) return null;
        const d = twa - tgt;
        if (Math.abs(d) < RaceTracks.ON_TARGET_DEG) return { deg: d, text: 'on target' };
        const high = d < 0;
        const how = mode === 'u' ? (high ? 'pinching' : 'footing') : (high ? 'hotter' : 'deeper');
        return { deg: d, text: `${Math.abs(d).toFixed(0)}° ${high ? 'high' : 'low'} (${how})` };
    }

    /**
     * The box/tooltip text for one point, as lines (set with textContent: names are fetched JSON, P19).
     * [headline, angle vs target, speed vs target].
     */
    static lines(boat, p) {
        const what = { u: 'VMG upwind', d: 'VMG downwind', r: 'speed, reaching' }[p[7]] || '';
        const head = p[3] == null ? `${boat.name}: not scored (tack/gybe or no data)`
                                  : `${boat.name}: ${p[3]}% of ORC ${what}`;
        const out = [head];
        const v = RaceTracks.angleVerdict(p);
        if (v) out.push(`TWA ${p[4]}° vs target ${Math.round(p[8])}° → ${v.text}`);
        else if (p[4] != null) out.push(`TWA ${p[4]}°${p[7] === 'r' ? ' (reach: the course sets the angle)' : ''}`);
        const label = boat.source === 'ais' ? 'Speed thru water' : 'STW';
        if (p[6] != null && p[9] != null) {
            const dv = p[6] - p[9];
            out.push(`${label} ${p[6].toFixed(2)} vs target ${p[9].toFixed(2)} kn → ${dv >= 0 ? '+' : '−'}${Math.abs(dv).toFixed(2)} kn`);
        } else if (p[6] != null) {
            out.push(`${label} ${p[6].toFixed(2)} kn`);
        }
        if (p[5] != null) out.push(`TWS ${p[5].toFixed(1)} kn (10 m)${boat.source === 'ais' ? ' · our wind + current' : ''}`);
        return out;
    }

    /** The point at moment `ms` (epoch ms), or null if the nearest is further than tolS away. */
    static atTime(pts, ms, tolS) {
        const t = ms / 1000;
        let lo = 0, hi = pts.length - 1;
        if (hi < 0) return null;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (pts[mid][0] < t) lo = mid + 1; else hi = mid;
        }
        const cands = [pts[lo], pts[lo - 1]].filter(Boolean);
        const best = cands.reduce((a, b) => Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a);
        return Math.abs(best[0] - t) <= tolS ? best : null;
    }

    /** Median % over a boat's scored points, or null. */
    static median(pts) {
        const v = pts.map(p => p[3]).filter(x => x != null).sort((a, b) => a - b);
        return v.length ? v[Math.floor(v.length / 2)] : null;
    }

    /** Legend. Built with textContent: boat names come from fetched JSON (P19). */
    _legend(race) {
        const ctl = L.control({ position: 'topleft' });
        ctl.onAdd = () => {
            const div = L.DomUtil.create('div', 'race-legend');
            const el = (tag, cls, text) => {
                const e = document.createElement(tag);
                if (cls) e.className = cls;
                if (text != null) e.textContent = text;
                return e;
            };
            div.appendChild(el('div', 'race-legend-title', race.label));
            div.appendChild(el('div', 'race-legend-ramp'));
            const scale = el('div', 'race-legend-scale');
            scale.appendChild(el('span', null, `≤${RaceTracks.RED_PCT}%`));
            scale.appendChild(el('span', null, `${(RaceTracks.RED_PCT + RaceTracks.GREEN_PCT) / 2}%`));
            scale.appendChild(el('span', null, `≥${RaceTracks.GREEN_PCT}%`));
            div.appendChild(scale);
            for (const b of race.boats) {
                const m = RaceTracks.median(b.pts);
                const row = el('div', 'race-legend-boat');
                row.appendChild(el('span', b.name === 'Typon' ? 'race-legend-line own' : 'race-legend-line'));
                row.appendChild(el('span', null, `${b.name}: median ${m == null ? '–' : m + '%'}` +
                                                  (b.source === 'ais' ? ' (AIS)' : '')));
                div.appendChild(row);
            }
            // Live: what each boat is doing at the replay clock. Filled by update().
            const now = el('div', 'race-now');
            now.appendChild(el('div', 'race-now-clock', 'Replay: –'));
            this._nowEls = [];
            for (const b of race.boats) {
                const box = el('div', 'race-now-boat');
                now.appendChild(box);
                this._nowEls.push({ boat: b, box });
            }
            div.appendChild(now);
            this._nowClock = now.firstChild;
            div.appendChild(el('div', 'race-legend-note',
                '% of own ORC cert: VMG upwind/downwind, speed on reaches. Targets are the cert\'s beat/gybe ' +
                'angle and speed. Grey = the turn of a tack/gybe, or no data; the speed build after it is scored. ' +
                'AIS boats use our wind and current and report every ~30 s. Hover for detail, click to jump there.'));
            L.DomEvent.disableClickPropagation(div);
            return div;
        };
        return ctl;
    }

    constructor(map, onPick) {
        this.map = map;
        this.onPick = onPick;          // (epochMs) => void: click on a track seeks the replay there
        this.data = null;
        this.layers = [];
        this.legend = null;
        this.renderer = L.canvas({ padding: 0.5 });
    }

    load(url = RaceTracks.SERIES_URL) {
        if (this.data) return Promise.resolve(this.data);
        return fetch(url, { cache: 'no-cache' })
            .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(d => (this.data = d));
    }

    race(id) {
        return this.data && this.data.races.find(r => r.id === id);
    }

    show(race) {
        this.clear();
        // Rivals first so Typon draws on top.
        const boats = [...race.boats].sort((a, b) => (a.name === 'Typon') - (b.name === 'Typon'));
        const bounds = L.latLngBounds([]);
        for (const boat of boats) {
            const own = boat.name === 'Typon';
            const group = L.featureGroup();
            for (const s of RaceTracks.segments(boat.pts)) {
                L.polyline(s.latlngs, {
                    color: s.color, weight: own ? 5 : 3, opacity: own ? 0.95 : 0.85,
                    renderer: this.renderer, interactive: true,
                }).addTo(group);
            }
            group.bindTooltip('', { sticky: true, className: 'race-track-tip' });
            group.on('mousemove', (e) => {
                const p = RaceTracks.nearest(boat.pts, e.latlng.lat, e.latlng.lng);
                if (!p) return;
                const tip = document.createElement('div');
                const t = new Date(p[0] * 1000).toLocaleTimeString([], { hour12: false });
                for (const line of [t, ...RaceTracks.lines(boat, p)]) {
                    const d = document.createElement('div');
                    d.textContent = line;
                    tip.appendChild(d);
                }
                group.setTooltipContent(tip);
            });
            group.on('click', (e) => {
                const p = RaceTracks.nearest(boat.pts, e.latlng.lat, e.latlng.lng);
                if (p && this.onPick) this.onPick(p[0] * 1000);
            });
            group.addTo(this.map);
            this.layers.push(group);
            if (own && group.getLayers().length) bounds.extend(group.getBounds());
        }
        this.legend = this._legend(race).addTo(this.map);
        // Frame on our own GPS track only. A single corrupt AIS fix in a rival's
        // track (longitude -1.4 in the 2026 data) once zoomed the map out to half
        // the world, where Leaflet laid out z10 local tiles for all of it and the
        // tab froze.
        if (bounds.isValid()) this.map.fitBounds(bounds, { padding: [30, 30] });
    }

    /** Refresh the live box for replay moment `ms` (epoch ms); null = no replay clock. */
    update(ms) {
        if (!this._nowEls || !this.legend) return;
        if (ms === this._lastNow) return;
        this._lastNow = ms;
        this._nowClock.textContent = ms == null ? 'Replay: –'
            : 'Replay ' + new Date(ms).toLocaleTimeString([], { hour12: false });
        for (const { boat, box } of this._nowEls) {
            const tol = RaceTracks.NOW_TOL_S[boat.source] || 10;
            const p = ms == null ? null : RaceTracks.atTime(boat.pts, ms, tol);
            const lines = p ? RaceTracks.lines(boat, p)
                            : [`${boat.name}: ${ms == null ? '–' : 'no data at this moment'}`];
            box.textContent = '';
            lines.forEach((line, i) => {
                const d = document.createElement('div');
                d.textContent = line;
                if (i === 0) {
                    d.className = 'race-now-head';
                    if (p) d.style.borderLeftColor = RaceTracks.colorFor(p[3]);
                }
                box.appendChild(d);
            });
        }
    }

    clear() {
        this._nowEls = null;
        this._lastNow = undefined;
        for (const l of this.layers) this.map.removeLayer(l);
        this.layers = [];
        if (this.legend) { this.legend.remove(); this.legend = null; }
    }
}

if (typeof window !== 'undefined') window.RaceTracks = RaceTracks;
