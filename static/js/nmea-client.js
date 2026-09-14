/**
 * NMEA data source client — live WebSocket or file replay.
 */

class NmeaClient {
    constructor(store) {
        this.store = store;
        this.ws = null;
        this._stopped = true;
        this._reconnectTimer = null;
        this._status = 'disconnected';
        this._sentenceCount = 0;
        this._sentenceRate = 0;
        this._rateInterval = null;
        this._onStatus = null;

        this._replayLines = null;
        this._replayIdx = 0;
        this._replaySpeed = 1;
        this._replayPaused = false;
        this._replayRafId = null;
        this._replayStartWall = 0;
        this._replayStartTime = 0;
    }

    setStatusCallback(cb) { this._onStatus = cb; }

    _setStatus(s) {
        this._status = s;
        if (this._onStatus) this._onStatus(s, this._sentenceRate);
    }

    // --- Live WebSocket ---

    connect(wsUrl) {
        this.stopReplay();
        this.store.reset();
        this._stopped = false;
        this._sentenceCount = 0;
        this._sentenceRate = 0;
        this._startRateCounter();
        this._doConnect(wsUrl);
    }

    disconnect() {
        this._stopped = true;
        if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
        if (this._rateInterval) { clearInterval(this._rateInterval); this._rateInterval = null; }
        if (this.ws) { this.ws.close(); this.ws = null; }
        this._setStatus('disconnected');
    }

    _doConnect(wsUrl) {
        if (this._stopped) return;
        let sock;
        try {
            sock = this.ws = new WebSocket(wsUrl);
        } catch (e) {
            this._setStatus('error');
            this._scheduleReconnect(wsUrl);
            return;
        }

        // Every handler checks it is still the current socket. A socket closed by
        // disconnect() still fires onclose afterwards, and without this guard that
        // late event set the status to 'disconnected' *during* a replay — the
        // header read "Disconnected" while the recording was plainly playing.
        const current = () => this.ws === sock && !this._stopped;

        sock.onopen = () => { if (current()) this._setStatus('connected'); };

        sock.onmessage = (event) => {
            if (!current()) return;
            const line = typeof event.data === 'string' ? event.data : '';
            if (line) {
                this._sentenceCount++;
                this.store.ingest(line, Date.now());
            }
        };

        sock.onclose = () => {
            if (!current()) return;
            this._setStatus('disconnected');
            this._scheduleReconnect(wsUrl);
        };

        sock.onerror = () => {
            if (!current()) return;
            this._setStatus('error');
            sock.close();
        };
    }

    _scheduleReconnect(wsUrl) {
        if (this._stopped) return;
        this._reconnectTimer = setTimeout(() => this._doConnect(wsUrl), 5000);
    }

    _startRateCounter() {
        let prevCount = 0;
        this._rateInterval = setInterval(() => {
            this._sentenceRate = this._sentenceCount - prevCount;
            prevCount = this._sentenceCount;
            if (this._onStatus) this._onStatus(this._status, this._sentenceRate);
        }, 1000);
    }

    // --- File Replay ---

    loadFile(file, callback) {
        const reader = new FileReader();
        reader.onload = (e) => this.loadText(e.target.result, callback);
        reader.readAsText(file);
    }

    /** Load a log from already-read text. Shared by loadFile and loadUrl. */
    loadText(text, callback) {
        this._replayLines = text.split('\n').filter(l => l && !l.startsWith('#'));
        this._parseReplayTimestamps();
        if (callback) callback(this._replayLines.length);
    }

    /** Load a log straight off the Pi (/logs/<name>), no download step. */
    loadUrl(url, callback, onError) {
        fetch(url, { cache: 'no-store' })
            .then((r) => {
                if (!r.ok) throw new Error(`${r.status} fetching ${url}`);
                return r.text();
            })
            .then((text) => this.loadText(text, callback))
            .catch((err) => {
                // Surface it. Silently loading nothing would look like an empty
                // recording rather than a failed fetch.
                if (onError) onError(err);
                else this._setStatus('error');
            });
    }

    _parseReplayTimestamps() {
        const re = /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+Z?)/;
        this._replayTimestamps = this._replayLines.map(line => {
            const m = re.exec(line);
            if (!m) return null;
            return new Date(m[1].replace(' ', 'T').replace(',', '.') + (m[1].endsWith('Z') ? '' : 'Z')).getTime();
        });
    }

    startReplay(speed) {
        this.disconnect();
        // Cancel any tick chain from a previous recording, or two self-sustaining
        // rAF loops end up running on one playhead.
        if (this._replayRafId) {
            cancelAnimationFrame(this._replayRafId);
            this._replayRafId = null;
        }
        this.store.reset();
        if (this.onReplayReset) this.onReplayReset();
        this._replaySpeed = speed || 1;
        this._replayIdx = 0;
        this._replayPaused = false;
        this._sentenceCount = 0;
        this._startRateCounter();

        const firstTs = this._replayTimestamps.find(t => t != null);
        if (!firstTs) return;
        this._replayStartTime = firstTs;
        this._replayStartWall = performance.now();

        this._setStatus('replaying');
        this._replayTick();
    }

    _replayTick() {
        if (this._replayPaused) return;
        if (this._replayIdx >= this._replayLines.length) {
            this._setStatus('replay-done');
            return;
        }

        const now = performance.now();
        const elapsed = (now - this._replayStartWall) * this._replaySpeed;
        const targetTime = this._replayStartTime + elapsed;

        let emitted = 0;
        const maxPerFrame = this._replaySpeed === 0 ? this._replayLines.length : 500;

        // Coalesce this frame's batch, exactly as seek() does. Without it every
        // sentence moves the map immediately: a stationary boat's GPS noise and
        // its coarser AIS position alternate 10+ times a second, so the own-ship
        // icon visibly shakes — and at 60x it is hundreds of redraws per second.
        // One update per animation frame is all the display can show anyway.
        const bulk = typeof this.store.beginBulk === 'function';
        if (bulk) this.store.beginBulk();
        try {
            while (this._replayIdx < this._replayLines.length && emitted < maxPerFrame) {
                const lineTs = this._replayTimestamps[this._replayIdx];
                if (this._replaySpeed !== 0 && lineTs != null && lineTs > targetTime) break;

                this.store.ingest(this._replayLines[this._replayIdx], lineTs || Date.now());
                this._sentenceCount++;
                this._replayIdx++;
                emitted++;
            }
        } finally {
            if (bulk) this.store.endBulk();
        }

        this._publishReplayClock();

        if (this._replayIdx >= this._replayLines.length) {
            this._setStatus('replay-done');
            return;
        }

        this._replayRafId = requestAnimationFrame(() => this._replayTick());
    }

    /**
     * Move the playhead to `idx`, rebuilding store state to match.
     *
     * Instrument state accumulates — position, heading, wind, AIS targets all
     * persist until the next sentence overwrites them. So a seek cannot just
     * move the index: it resets the store and re-ingests everything up to the
     * target. Otherwise scrubbing backwards would show the boat at 14:05 still
     * carrying readings from 14:50.
     *
     * Re-ingest runs in the store's bulk mode, so AIS decoding happens for every
     * sentence but the map/panel refresh is coalesced into one event. An hour of
     * NMEA is ~12k lines; the largest logs are ~150k. Callers should still seek
     * on a slider's `change`, not `input`.
     */
    seek(idx) {
        if (!this._replayLines) return;
        const target = Math.max(0, Math.min(idx | 0, this._replayLines.length));

        if (this._replayRafId) {
            cancelAnimationFrame(this._replayRafId);
            this._replayRafId = null;
        }

        this.store.reset();
        if (this.onReplayReset) this.onReplayReset();
        this._sentenceCount = 0;

        // Bulk mode: AIS still decodes per sentence, but the map/panel refresh
        // is coalesced into one event at the end. Without it, re-ingesting
        // 100k lines fires 100k Leaflet marker updates and panel rebuilds and
        // the tab hangs for minutes on every scrub.
        const bulk = typeof this.store.beginBulk === 'function';
        if (bulk) this.store.beginBulk();
        try {
            for (let i = 0; i < target; i++) {
                this.store.ingest(this._replayLines[i], this._replayTimestamps[i] || Date.now());
                this._sentenceCount++;
            }
        } finally {
            if (bulk) this.store.endBulk();
        }
        this._replayIdx = target;
        this._rebaseReplayClock();
        this._publishReplayClock();

        // Resume only if we were already running; seeking must not un-pause.
        // Deferred to the next frame so that when seek() returns, the playhead
        // is exactly where it was asked to be — a synchronous tick would emit
        // the next sentence first and land one line past the target.
        if (!this._replayPaused && target < this._replayLines.length) {
            this._setStatus('replaying');
            this._replayRafId = requestAnimationFrame(() => this._replayTick());
        }
    }

    /** Tell listeners what time it is *in the recording*. */
    _publishReplayClock() {
        if (!this.onReplayClock) return;
        const r = this.getReplayTimeRange();
        if (r) this.onReplayClock(r.current);
    }

    /** Anchor wall-clock timing to the current playhead. */
    _rebaseReplayClock() {
        const lineTs = this._replayTimestamps
            ? this._replayTimestamps[Math.min(this._replayIdx, this._replayTimestamps.length - 1)]
            : null;
        if (lineTs != null) {
            this._replayStartTime = lineTs;
            this._replayStartWall = performance.now();
        }
    }

    /** {start, end, current} epoch ms for the scrubber's clock readout. */
    getReplayTimeRange() {
        if (!this._replayTimestamps || !this._replayTimestamps.length) return null;
        const stamped = this._replayTimestamps.filter(t => t != null);
        if (!stamped.length) return null;
        const idx = Math.min(this._replayIdx, this._replayTimestamps.length - 1);
        let current = this._replayTimestamps[idx];
        if (current == null) current = stamped[0];
        return { start: stamped[0], end: stamped[stamped.length - 1], current };
    }

    pauseReplay() {
        this._replayPaused = true;
        this._setStatus('replay-paused');
    }

    resumeReplay() {
        if (!this._replayPaused) return;
        this._replayPaused = false;
        const lineTs = this._replayTimestamps[this._replayIdx];
        if (lineTs) {
            this._replayStartTime = lineTs;
            this._replayStartWall = performance.now();
        }
        this._setStatus('replaying');
        this._replayTick();
    }

    setReplaySpeed(speed) {
        const lineTs = this._replayTimestamps[this._replayIdx];
        if (lineTs) {
            this._replayStartTime = lineTs;
            this._replayStartWall = performance.now();
        }
        this._replaySpeed = speed;
    }

    stopReplay() {
        if (this._replayRafId) { cancelAnimationFrame(this._replayRafId); this._replayRafId = null; }
        if (this._rateInterval) { clearInterval(this._rateInterval); this._rateInterval = null; }
        this._replayLines = null;
        this._replayPaused = false;
        this._setStatus('disconnected');
    }

    getReplayProgress() {
        if (!this._replayLines) return null;
        return { current: this._replayIdx, total: this._replayLines.length,
                 pct: Math.round(this._replayIdx / this._replayLines.length * 100) };
    }
}
