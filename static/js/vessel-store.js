/**
 * In-memory vessel store — replaces the SQLite database for the static PWA.
 *
 * Maintains vessel state and track history entirely in the browser.
 * Optionally persists to localStorage for survival across page refreshes.
 */

class VesselStore {
    constructor({ staleMinutes = 10, maxTrackPoints = 500, persist = true } = {}) {
        this.vessels = new Map();       // mmsi → vessel data
        this.tracks = new Map();        // mmsi → { points: [{lat, lon, time, sog, cog}], ... }
        this.staleMinutes = staleMinutes;
        this.maxTrackPoints = maxTrackPoints;
        this.persist = persist;
        this.messageCount = 0;

        // Replay mode. Replayed contacts are historical: they must not be
        // written to localStorage (they would come back on reload as live
        // traffic), and prune() must not measure their age against the wall
        // clock (a June race is hours "stale" the instant it is ingested, so
        // every target would be deleted immediately). Time in replay comes
        // from the log itself — see `now()`.
        this.replayMode = false;
        this._replayClock = null;

        // Restore from localStorage if available
        if (persist) this._restore();

        // Periodic prune of stale vessels
        this._pruneInterval = setInterval(() => this.prune(), 60000);
    }

    /** The clock this store reasons about: wall clock live, log time in replay. */
    now() {
        return this.replayMode && this._replayClock != null
            ? this._replayClock : Date.now();
    }

    /**
     * Enter or leave replay. Clears the store in BOTH directions: the two modes
     * use incompatible time bases, so contacts can never legitimately survive
     * the switch. Clearing on entry only, and trusting the caller to clean up on
     * exit, is what let a replayed fleet leak onto the live chart.
     */
    setReplayMode(on) {
        this.replayMode = !!on;
        this._replayClock = null;
        this.clearAll();
    }

    /** Advance the replay clock, so staleness is measured in log time. */
    setReplayClock(ms) {
        if (Number.isFinite(ms)) this._replayClock = ms;
    }

    /**
     * Drop every vessel and track. Used when entering replay, when seeking
     * (state is rebuilt from the start of the log), and when returning to Live
     * — otherwise replayed contacts stay on the map as current traffic.
     */
    clearAll() {
        const mmsis = Array.from(this.vessels.keys());
        this.vessels.clear();
        this.tracks.clear();
        return mmsis;
    }

    /**
     * Merge an incoming AIS message with existing vessel data.
     * Returns the merged vessel object.
     */
    upsert(msg) {
        const existing = this.vessels.get(msg.mmsi) || {};
        // _reportedAt is set by nmea-store from the sentence's own timestamp, so
        // in replay this is log time. Falls back to the wall clock for the
        // cloud-AIS path, which has no per-message timestamp.
        const seenAt = Number.isFinite(msg._reportedAt) ? msg._reportedAt : this.now();
        const merged = { ...existing, ...msg, _lastUpdate: seenAt };
        this.vessels.set(msg.mmsi, merged);
        this.messageCount++;

        // Add to track if position data present
        if (msg.lat != null && msg.lon != null) {
            let track = this.tracks.get(msg.mmsi);
            if (!track) {
                track = { points: [] };
                this.tracks.set(msg.mmsi, track);
            }

            track.points.push({
                lat: msg.lat,
                lon: msg.lon,
                time: seenAt,
                sog: msg.sog || 0,
                cog: msg.cog || 0,
            });

            // Trim old points
            if (track.points.length > this.maxTrackPoints) {
                track.points = track.points.slice(-this.maxTrackPoints);
            }
        }

        return merged;
    }

    /**
     * Get vessel data by MMSI.
     */
    get(mmsi) {
        return this.vessels.get(mmsi);
    }

    /**
     * Get all vessels as an array.
     */
    getAll() {
        return Array.from(this.vessels.values());
    }

    /**
     * Get track points for a vessel, optionally filtered by hours.
     */
    getTrack(mmsi, hours = 2) {
        const track = this.tracks.get(mmsi);
        if (!track) return [];

        // this.now(), not Date.now(): in replay every point is hours old in
        // wall-clock terms, so the wall clock would return an empty track and
        // the radar trails would vanish.
        const cutoff = this.now() - hours * 3600000;
        return track.points.filter(p => p.time >= cutoff);
    }

    /**
     * Compute rolling average speed from recent track points.
     */
    getAvgSpeed(mmsi) {
        const points = this.getTrack(mmsi, 0.5); // Last 30 min
        if (points.length < 2) return null;

        const sogValues = points.map(p => p.sog).filter(s => s > 0);
        if (sogValues.length === 0) return null;

        return Math.round(sogValues.reduce((a, b) => a + b, 0) / sogValues.length * 10) / 10;
    }

    /**
     * Remove vessels not seen in staleMinutes.
     * Returns array of pruned MMSIs.
     */
    prune() {
        const nowMs = this.now();
        const cutoff = nowMs - this.staleMinutes * 60000;
        const pruned = [];

        for (const [mmsi, vessel] of this.vessels) {
            if ((vessel._lastUpdate || 0) < cutoff) {
                this.vessels.delete(mmsi);
                this.tracks.delete(mmsi);
                pruned.push(mmsi);
            }
        }

        // Also trim track history older than 2 hours
        const trackCutoff = nowMs - 2 * 3600000;
        for (const [mmsi, track] of this.tracks) {
            track.points = track.points.filter(p => p.time >= trackCutoff);
            if (track.points.length === 0) this.tracks.delete(mmsi);
        }

        // Never persist in replay: a reload would restore historical contacts
        // and present them as live traffic.
        if (this.persist && !this.replayMode && pruned.length > 0) this._save();

        return pruned;
    }

    /**
     * Save current state to localStorage.
     */
    _save() {
        try {
            const data = {
                vessels: Object.fromEntries(this.vessels),
                tracks: Object.fromEntries(
                    Array.from(this.tracks.entries()).map(([k, v]) => [k, v.points.slice(-50)])
                ),
                savedAt: Date.now(),
            };
            localStorage.setItem('vesselStore', JSON.stringify(data));
        } catch (e) {
            // localStorage full or unavailable
        }
    }

    /**
     * Restore state from localStorage.
     */
    _restore() {
        try {
            const raw = localStorage.getItem('vesselStore');
            if (!raw) return;

            const data = JSON.parse(raw);

            // Only restore if saved less than staleMinutes ago
            if (Date.now() - data.savedAt > this.staleMinutes * 60000) {
                localStorage.removeItem('vesselStore');
                return;
            }

            if (data.vessels) {
                for (const [mmsi, vessel] of Object.entries(data.vessels)) {
                    this.vessels.set(parseInt(mmsi), vessel);
                }
            }
            if (data.tracks) {
                for (const [mmsi, points] of Object.entries(data.tracks)) {
                    this.tracks.set(parseInt(mmsi), { points });
                }
            }
        } catch (e) {
            // Corrupt data, ignore
        }
    }

    /**
     * Periodically save to localStorage (call from app on a timer).
     */
    saveIfNeeded() {
        if (this.persist && !this.replayMode) this._save();
    }

    destroy() {
        clearInterval(this._pruneInterval);
    }
}
