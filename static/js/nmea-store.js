/**
 * Central NMEA state manager with time-series history.
 * Extends EventTarget: dispatches 'update', 'position', 'ais' events.
 */

class NmeaStore extends EventTarget {
    constructor() {
        super();
        this.state = {
            lat: null, lon: null, fix: 0,
            sog: null, bsp: null,
            heading: null, cog: null,
            awa: null, aws: null,
            twa: null, twd: null, tws: null,
            depth: null,
            heel: null, pitch: null,
            rot: null,
            lastUpdate: null,
        };

        this._history = new Map();
        const fields = ['twa', 'twd', 'tws', 'bsp', 'awa', 'aws', 'sog', 'heading', 'depth'];
        for (const f of fields) this._history.set(f, []);

        this._maxHistory = 3600;
        this._lastHistoryTime = new Map();
        this._dirty = false;
        this._positionDirty = false;
        this._ownMmsi = 338361814;

        this._startUpdateLoop();
    }

    reset() {
        for (const k of Object.keys(this.state)) this.state[k] = null;
        this.state.fix = 0;
        for (const [, arr] of this._history) arr.length = 0;
        this._lastHistoryTime.clear();
        this._dirty = false;
        this._positionDirty = false;
    }

    ingest(line, wallTime) {
        const parsed = NmeaParser.parseLine(line);
        if (!parsed) return;

        const ts = parsed.timestamp || new Date(wallTime);
        const t = ts instanceof Date ? ts.getTime() : new Date(ts).getTime();

        if (parsed.isAIS) {
            const vessel = AISDecoder.processSentence(parsed.sentence);
            if (vessel) {
                if (parsed.sentence.startsWith('!AIVDO') || vessel.mmsi === this._ownMmsi) {
                    vessel.is_own_vessel = true;
                }
                this.dispatchEvent(new CustomEvent('ais', { detail: vessel }));
            }
            return;
        }

        switch (parsed.type) {
            case 'GGA':
                this.state.lat = parsed.lat;
                this.state.lon = parsed.lon;
                this.state.fix = parsed.fix;
                this._positionDirty = true;
                break;

            case 'RMC':
                if (parsed.lat != null) {
                    this.state.lat = parsed.lat;
                    this.state.lon = parsed.lon;
                    this._positionDirty = true;
                }
                if (parsed.sog != null) this._update('sog', parsed.sog, t);
                if (parsed.cog != null) this.state.cog = parsed.cog;
                break;

            case 'HDG':
                this.state.heading = parsed.heading;
                this._recordHistory('heading', parsed.heading, t);
                break;

            case 'MWV':
                if (parsed.reference === 'R') {
                    this.state.awa = parsed.angle;
                    this.state.aws = parsed.speed;
                    this._recordHistory('awa', parsed.angle, t);
                    this._recordHistory('aws', parsed.speed, t);
                    this._computeTrueWind(t);
                } else if (parsed.reference === 'T') {
                    if (this.state.twa === null) {
                        this.state.twa = parsed.angle;
                        this._recordHistory('twa', parsed.angle, t);
                    }
                    if (this.state.tws === null) {
                        this.state.tws = parsed.speed;
                        this._recordHistory('tws', parsed.speed, t);
                    }
                }
                break;

            case 'MWD':
                if (parsed.dirTrue != null) {
                    this.state.twd = parsed.dirTrue;
                    this._recordHistory('twd', parsed.dirTrue, t);
                }
                if (parsed.speedKn != null) {
                    this.state.tws = parsed.speedKn;
                    this._recordHistory('tws', parsed.speedKn, t);
                }
                break;

            case 'VHW':
                this._update('bsp', parsed.bsp, t);
                break;

            case 'DPT':
                this._update('depth', parsed.depth, t);
                break;

            case 'VTG':
                if (parsed.sogKn != null) this._update('sog', parsed.sogKn, t);
                if (parsed.cogTrue != null) this.state.cog = parsed.cogTrue;
                break;

            case 'ROT':
                this.state.rot = parsed.rate;
                break;

            case 'XDR':
                if (parsed.roll != null) this.state.heel = Math.abs(parsed.roll);
                if (parsed.pitch != null) this.state.pitch = parsed.pitch;
                break;
        }

        this.state.lastUpdate = t;
        this._dirty = true;
    }

    _update(field, value, t) {
        this.state[field] = value;
        this._recordHistory(field, value, t);
    }

    _recordHistory(field, value, t) {
        if (value === null) return;
        const last = this._lastHistoryTime.get(field) || 0;
        if (t - last < 1000) return;
        this._lastHistoryTime.set(field, t);

        const arr = this._history.get(field);
        if (!arr) return;
        arr.push({ t, v: value });
        if (arr.length > this._maxHistory) arr.shift();
    }

    _computeTrueWind(t) {
        const { awa, aws, bsp, heading } = this.state;
        if (awa === null || aws === null) return;

        const effectiveBsp = (bsp !== null && bsp > 0) ? bsp : 0;
        const awaRad = awa * Math.PI / 180;

        let tws, twa;
        if (effectiveBsp === 0) {
            tws = aws;
            twa = awa;
        } else {
            tws = Math.sqrt(aws * aws + effectiveBsp * effectiveBsp - 2 * aws * effectiveBsp * Math.cos(awaRad));
            const twaRad = Math.atan2(aws * Math.sin(awaRad), aws * Math.cos(awaRad) - effectiveBsp);
            twa = twaRad * 180 / Math.PI;
            if (twa < 0) twa += 360;
        }

        this.state.tws = Math.round(tws * 10) / 10;
        this.state.twa = Math.round(twa * 10) / 10;
        this._recordHistory('tws', this.state.tws, t);
        this._recordHistory('twa', this.state.twa, t);

        if (heading !== null) {
            this.state.twd = (heading + twa + 360) % 360;
            this.state.twd = Math.round(this.state.twd * 10) / 10;
            this._recordHistory('twd', this.state.twd, t);
        }
    }

    getHistory(field, windowMs) {
        const arr = this._history.get(field);
        if (!arr || arr.length === 0) return [];
        if (!windowMs) return arr.slice();
        const cutoff = arr[arr.length - 1].t - windowMs;
        const startIdx = arr.findIndex(p => p.t >= cutoff);
        return startIdx >= 0 ? arr.slice(startIdx) : [];
    }

    getState() {
        return { ...this.state };
    }

    _startUpdateLoop() {
        const tick = () => {
            if (this._dirty) {
                this._dirty = false;
                this.dispatchEvent(new CustomEvent('update', { detail: this.state }));
            }
            if (this._positionDirty) {
                this._positionDirty = false;
                this.dispatchEvent(new CustomEvent('position', {
                    detail: { lat: this.state.lat, lon: this.state.lon }
                }));
            }
            requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    }
}
