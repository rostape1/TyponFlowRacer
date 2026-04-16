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

        // Restore from localStorage if available
        if (persist) this._restore();

        // Periodic prune of stale vessels
        this._pruneInterval = setInterval(() => this.prune(), 60000);
    }

    /**
     * Merge an incoming AIS message with existing vessel data.
     * Returns the merged vessel object.
     */
    upsert(msg) {
        const existing = this.vessels.get(msg.mmsi) || {};
        const merged = { ...existing, ...msg, _lastUpdate: Date.now() };
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
                time: Date.now(),
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

        const cutoff = Date.now() - hours * 3600000;
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
        const cutoff = Date.now() - this.staleMinutes * 60000;
        const pruned = [];

        for (const [mmsi, vessel] of this.vessels) {
            if ((vessel._lastUpdate || 0) < cutoff) {
                this.vessels.delete(mmsi);
                this.tracks.delete(mmsi);
                pruned.push(mmsi);
            }
        }

        // Also trim track history older than 2 hours
        const trackCutoff = Date.now() - 2 * 3600000;
        for (const [mmsi, track] of this.tracks) {
            track.points = track.points.filter(p => p.time >= trackCutoff);
            if (track.points.length === 0) this.tracks.delete(mmsi);
        }

        if (this.persist && pruned.length > 0) this._save();

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
        if (this.persist) this._save();
    }

    destroy() {
        clearInterval(this._pruneInterval);
    }
}
