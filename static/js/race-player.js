/**
 * The crew race page (race.html): a clock that plays a race back, boats moving along their
 * coloured tracks, Typon's instrument strip, and the review text.
 *
 * Data is all static, so this runs on GitHub Pages with no server: static/races/index.json and the
 * regatta files (tracks, scores, targets: the same ones replay's race picker draws with
 * RaceTracks), <race>-fleet.json for the other AIS vessels, and reviews/<race>.md. All written by
 * tools/race_tracks.py. Nothing is scored here.
 */
class RacePlayer {
    static get SPEEDS() { return [1, 5, 10, 30, 60, 120]; }
    static get DEFAULT_SPEED() { return 30; }
    // A boat is drawn between two of its points only if they are this close in time; across a
    // longer hole it is hidden rather than slid in a straight line over land.
    static get MAX_GAP_S() { return { instruments: 30, ais: 150, fleet: 180 }; }
    static get FRAME_MS() { return 100; }          // redraw at 10 Hz while playing
    // A clock time in the review links only if it falls in the race (with this margin): "18:17
    // behind" is a gap, not a moment.
    static get LINK_MARGIN_MS() { return 30 * 60 * 1000; }
    // Follow: re-centre on Typon once it is within this fraction of the view's edge.
    static get FOLLOW_EDGE() { return 0.15; }

    /** Index of the last point with t <= tSec, or -1. pts rows start with t (epoch s). */
    static indexBefore(pts, tSec) {
        let lo = 0, hi = pts.length - 1, ans = -1;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (pts[mid][0] <= tSec) { ans = mid; lo = mid + 1; } else hi = mid - 1;
        }
        return ans;
    }

    /** Index of the point nearest tSec, or -1 if none is within tolS. */
    static nearestIndex(pts, tSec, tolS) {
        const i = RacePlayer.indexBefore(pts, tSec);
        let best = -1, bd = Infinity;
        for (const j of [i, i + 1]) {
            if (j < 0 || j >= pts.length) continue;
            const d = Math.abs(pts[j][0] - tSec);
            if (d < bd) { bd = d; best = j; }
        }
        return bd <= tolS ? best : -1;
    }

    /**
     * Where a boat is at tSec: linear between the bracketing points; at its first or last point
     * when tSec is within maxGapS outside them; otherwise null (before its track, after it, or
     * inside a hole longer than maxGapS). Returns {lat, lon, i, f}: i the point before, f the
     * fraction of the way to the next.
     */
    static position(pts, tSec, maxGapS) {
        if (!pts || !pts.length) return null;
        const i = RacePlayer.indexBefore(pts, tSec);
        if (i < 0) {
            const a = pts[0];
            return a[0] - tSec <= maxGapS ? { lat: a[1], lon: a[2], i: 0, f: 0 } : null;
        }
        const a = pts[i];
        if (i === pts.length - 1) {
            return tSec - a[0] <= maxGapS ? { lat: a[1], lon: a[2], i, f: 0 } : null;
        }
        const b = pts[i + 1];
        if (b[0] - a[0] > maxGapS) return null;
        const f = (tSec - a[0]) / (b[0] - a[0]);
        return { lat: a[1] + (b[1] - a[1]) * f, lon: a[2] + (b[2] - a[2]) * f, i, f };
    }

    /** Initial bearing from point a to b ([t, lat, lon] rows), degrees true; null if they coincide. */
    static bearing(a, b) {
        const k = Math.cos(a[1] * Math.PI / 180);
        const dx = (b[2] - a[2]) * k, dy = b[1] - a[1];
        if (dx === 0 && dy === 0) return null;
        return (Math.atan2(dx, dy) * 180 / Math.PI + 360) % 360;
    }

    /** Next clock value: dtMs of real time at `speed`, clamped to [start, end]. */
    static advance(ms, dtMs, speed, start, end) {
        return Math.min(end, Math.max(start, ms + dtMs * speed));
    }

    /** Seconds since local midnight of epoch ms, in timeZone. */
    static secondsOfDay(ms, timeZone) {
        const parts = new Intl.DateTimeFormat('en-GB', {
            timeZone, hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
        }).formatToParts(new Date(ms));
        const get = (t) => +parts.find(p => p.type === t).value;
        return (get('hour') % 24) * 3600 + get('minute') * 60 + get('second');
    }

    /**
     * The moment a local clock time (as written in the review) means on the race's day, or null if
     * it falls outside the race +- LINK_MARGIN_MS.
     */
    static timeOnRaceDay(race, timeZone, h, m, s = 0) {
        const t = race.start + ((h * 3600 + m * 60 + s) - RacePlayer.secondsOfDay(race.start, timeZone)) * 1000;
        const pad = RacePlayer.LINK_MARGIN_MS;
        return (t >= race.start - pad && t <= race.finish + pad) ? t : null;
    }

    /** HH:MM:SS of epoch ms in timeZone. */
    static clock(ms, timeZone) {
        return new Date(ms).toLocaleTimeString('en-GB', { hour12: false, timeZone });
    }

    /**
     * Typon's instrument strip at a point: [[label, value], ...]. p is a FIELDS row, inst its
     * [heel, hdg]; either may be missing.
     */
    static strip(p, inst) {
        const f = (v, d, unit) => v == null ? '–' : v.toFixed(d) + unit;
        const heel = inst && inst[0] != null ? Math.abs(inst[0]) : null;
        const hdg = inst && inst[1] != null ? inst[1] : null;
        return [
            ['TWS', p ? f(p[5], 1, '') : '–'],
            ['TWA', p ? f(p[4], 0, '°') : '–'],
            ['STW', p ? f(p[6], 2, '') : '–'],
            ['Heel', f(heel, 0, '°')],
            ['HDG', hdg == null ? '–' : String(Math.round(hdg)).padStart(3, '0') + '°'],
            ['ORC', p && p[3] != null ? p[3] + '%' : '–'],
        ];
    }

    constructor(map, ui) {
        this.map = map;
        this.ui = ui;                  // the page's elements, see race.html
        this.tracks = new RaceTracks(map, (ms) => this.seek(ms));
        this.race = null;
        this.timeZone = undefined;
        this.ms = null;
        this.playing = false;
        this.speed = RacePlayer.DEFAULT_SPEED;
        this.boatMarkers = [];
        this.fleetMarkers = [];
        this.fleetLayer = L.layerGroup().addTo(map);
        this.fleetRenderer = L.canvas({ padding: 0.5 });
        this._raf = null;
        this._lastFrame = null;
        this._lastDraw = 0;
        this._loadSeq = 0;
        this.follow = true;
        // Panning by hand means "let me look": stop dragging the view back to Typon.
        map.on('dragstart', () => this.setFollow(false));
        map.on('zoomend', () => { if (this.follow) this.draw(); });   // a zoom about the centre can lose Typon
    }

    setFollow(on) {
        this.follow = on;
        if (this.ui.following) this.ui.following(on);
        if (on) this.draw();
    }

    load() {
        return this.tracks.load().then(data => {
            this.timeZone = this.tracks.timeZone = data.timezone;
            return data;
        });
    }

    /** Show race `id`: tracks, markers, fleet (fetched), clock at the start. */
    select(id) {
        const race = this.tracks.race(id);
        if (!race) return false;
        this.pause();
        this.race = race;
        this.tracks.show(race);
        this._clearMarkers();
        this.boatMarkers = race.boats.map(b => this._boatMarker(b));
        const seq = ++this._loadSeq;
        this.fleet = [];
        if (race.fleet) {
            fetch('races/' + race.fleet, { cache: 'no-cache' })
                .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
                .then(doc => {
                    if (seq !== this._loadSeq) return;       // another race was picked meanwhile
                    this.fleet = doc.vessels || [];
                    this.fleetMarkers = this.fleet.map(v => this._fleetMarker(v));
                    this.ui.status(`${this.fleet.length} other boats on AIS`);
                    this.draw(true);
                })
                .catch(err => this.ui.status(`Other boats could not be loaded (${err.message})`, true));
        }
        const ui = this.ui;
        ui.scrub.min = race.start;
        ui.scrub.max = race.finish;
        this.seek(race.start);
        return true;
    }

    _clearMarkers() {
        for (const m of this.boatMarkers) m.marker.remove();
        this.fleetLayer.clearLayers();
        this.boatMarkers = [];
        this.fleetMarkers = [];
    }

    _boatMarker(boat) {
        const own = boat.name === 'Typon';
        const wrap = document.createElement('div');
        wrap.className = 'rp-boat' + (own ? ' own' : '');
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '-10 -10 20 20');
        svg.setAttribute('class', 'rp-hull');
        const hull = document.createElementNS(svgNS, 'path');
        hull.setAttribute('d', 'M0,-9 C4,-3 4,5 3,8 L-3,8 C-4,5 -4,-3 0,-9 Z');
        svg.appendChild(hull);
        const label = document.createElement('span');
        label.className = 'rp-label';
        label.textContent = boat.name;            // fetched JSON: textContent (P19)
        wrap.appendChild(svg);
        wrap.appendChild(label);
        const size = own ? 26 : 22;
        const marker = L.marker([boat.pts[0][1], boat.pts[0][2]], {
            icon: L.divIcon({ html: wrap, className: 'rp-boat-icon', iconSize: [size, size], iconAnchor: [size / 2, size / 2] }),
            interactive: false, keyboard: false, zIndexOffset: own ? 1000 : 500,
        });
        return { boat, marker, svg, hull, shown: false };
    }

    _fleetMarker(v) {
        const m = L.circleMarker([v.pts[0][1], v.pts[0][2]], {
            radius: 4, color: '#dfe6ee', weight: 1, fillColor: '#8395a7', fillOpacity: 0.9,
            renderer: this.fleetRenderer,
        });
        const tip = document.createElement('div');
        tip.textContent = v.name || `MMSI ${v.mmsi}`;  // P19
        m.bindTooltip(tip, { direction: 'top', offset: [0, -4], className: 'rp-fleet-tip' });
        return { v, m, shown: false };
    }

    seek(ms) {
        if (!this.race) return;
        this.ms = Math.min(this.race.finish, Math.max(this.race.start, ms));
        this.draw(true);
    }

    play() {
        if (!this.race || this.playing) return;
        if (this.ms >= this.race.finish) this.ms = this.race.start;
        this.playing = true;
        this._lastFrame = null;
        this.ui.playing(true);
        const step = (now) => {
            if (!this.playing) return;
            if (this._lastFrame != null) {
                this.ms = RacePlayer.advance(this.ms, now - this._lastFrame, this.speed, this.race.start, this.race.finish);
            }
            this._lastFrame = now;
            if (now - this._lastDraw >= RacePlayer.FRAME_MS) this.draw(false);
            if (this.ms >= this.race.finish) { this.draw(true); this.pause(); return; }
            this._raf = requestAnimationFrame(step);
        };
        this._raf = requestAnimationFrame(step);
    }

    pause() {
        this.playing = false;
        if (this._raf) cancelAnimationFrame(this._raf);
        this._raf = null;
        this.ui.playing(false);
    }

    toggle() { this.playing ? this.pause() : this.play(); }

    setSpeed(x) { this.speed = x; }

    /** Put everything at this.ms. */
    draw() {
        this._lastDraw = performance.now();
        const race = this.race, ms = this.ms, t = ms / 1000;
        if (!race) return;
        this.ui.scrub.value = ms;
        this.ui.clock(RacePlayer.clock(ms, this.timeZone), RacePlayer.clock(race.start, this.timeZone),
                      Math.round((ms - race.start) / 60000));
        this.tracks.update(ms);
        const gap = RacePlayer.MAX_GAP_S;
        for (const bm of this.boatMarkers) {
            const b = bm.boat;
            const pos = RacePlayer.position(b.pts, t, gap[b.source] || gap.ais);
            if (!pos) {
                if (bm.shown) { bm.marker.remove(); bm.shown = false; }
                continue;
            }
            bm.marker.setLatLng([pos.lat, pos.lon]);
            if (!bm.shown) { bm.marker.addTo(this.map); bm.shown = true; }
            const p = b.pts[pos.f < 0.5 ? pos.i : Math.min(pos.i + 1, b.pts.length - 1)];
            let hdg = b.inst && b.inst[pos.i] ? b.inst[pos.i][1] : null;
            if (hdg == null && pos.i + 1 < b.pts.length) hdg = RacePlayer.bearing(b.pts[pos.i], b.pts[pos.i + 1]);
            if (hdg != null) bm.svg.style.transform = `rotate(${hdg}deg)`;
            if (this.follow && b.name === 'Typon') this._keepInView(L.latLng(pos.lat, pos.lon));
            bm.hull.setAttribute('fill', RaceTracks.colorFor(p[3]));
        }
        for (const fm of this.fleetMarkers) {
            const pos = RacePlayer.position(fm.v.pts, t, gap.fleet);
            if (!pos) {
                if (fm.shown) { this.fleetLayer.removeLayer(fm.m); fm.shown = false; }
                continue;
            }
            fm.m.setLatLng([pos.lat, pos.lon]);
            if (!fm.shown) { this.fleetLayer.addLayer(fm.m); fm.shown = true; }
        }
        const own = race.boats.find(b => b.name === 'Typon');
        let p = null, inst = null;
        if (own) {
            const j = RacePlayer.nearestIndex(own.pts, t, RaceTracks.NOW_TOL_S.instruments);
            if (j >= 0) {
                p = own.pts[j];
                inst = own.inst ? own.inst[j] : null;
            }
        }
        this.ui.strip(RacePlayer.strip(p, inst), p ? RaceTracks.colorFor(p[3]) : null);
    }

    /**
     * Follow: pan when Typon nears the edge of the view or goes under the legend box, putting it
     * back in the middle of the chart that is left uncovered.
     */
    _keepInView(ll) {
        const size = this.map.getSize();
        const p = this.map.latLngToContainerPoint(ll);
        const m = RacePlayer.FOLLOW_EDGE, pad = 20;
        const leg = this.tracks.legend && this.tracks.legend.getContainer();
        const box = leg && leg.offsetWidth ? {
            x0: leg.offsetLeft - pad, x1: leg.offsetLeft + leg.offsetWidth + pad,
            y0: leg.offsetTop - pad, y1: leg.offsetTop + leg.offsetHeight + pad,
        } : null;
        const covered = box && p.x >= box.x0 && p.x <= box.x1 && p.y >= box.y0 && p.y <= box.y1;
        const inside = p.x >= size.x * m && p.x <= size.x * (1 - m) && p.y >= size.y * m && p.y <= size.y * (1 - m);
        if (inside && !covered) return;
        // A tall legend (a laptop) takes a strip down the left: centre in what is right of it.
        const left = box && box.y1 - box.y0 > size.y * 0.4 ? Math.max(0, box.x1) : 0;
        this.map.panBy([p.x - (left + size.x) / 2, p.y - size.y / 2], { animate: false });
    }

    /** The race's review, as DOM, with its clock times linked to seek(). Null if it has none. */
    review() {
        const race = this.race;
        if (!race || !race.review) return Promise.resolve(null);
        return fetch(race.review, { cache: 'no-cache' })
            .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
            .then(md => ReviewText.render(md, {
                linkTime: (h, m, s) => RacePlayer.timeOnRaceDay(race, this.timeZone, h, m, s),
                onTime: (ms) => { this.pause(); this.seek(ms); this.ui.reviewJumped(); },
            }));
    }
}

if (typeof window !== 'undefined') window.RacePlayer = RacePlayer;
