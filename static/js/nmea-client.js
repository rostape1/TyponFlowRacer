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
        if (location.protocol === 'https:' && wsUrl.startsWith('ws://')) {
            this._setStatus('error');
            console.warn('Cannot connect ws:// from HTTPS page. Use the boat URL (HTTP) for live NMEA.');
            return;
        }
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
        try {
            this.ws = new WebSocket(wsUrl);
        } catch (e) {
            this._setStatus('error');
            this._scheduleReconnect(wsUrl);
            return;
        }

        this.ws.onopen = () => this._setStatus('connected');

        this.ws.onmessage = (event) => {
            const line = typeof event.data === 'string' ? event.data : '';
            if (line) {
                this._sentenceCount++;
                this.store.ingest(line, Date.now());
            }
        };

        this.ws.onclose = () => {
            this._setStatus('disconnected');
            this._scheduleReconnect(wsUrl);
        };

        this.ws.onerror = () => {
            this._setStatus('error');
            if (this.ws) this.ws.close();
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
        reader.onload = (e) => {
            const text = e.target.result;
            this._replayLines = text.split('\n').filter(l => l && !l.startsWith('#'));
            this._parseReplayTimestamps();
            if (callback) callback(this._replayLines.length);
        };
        reader.readAsText(file);
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
        this.store.reset();
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

        while (this._replayIdx < this._replayLines.length && emitted < maxPerFrame) {
            const lineTs = this._replayTimestamps[this._replayIdx];
            if (this._replaySpeed !== 0 && lineTs != null && lineTs > targetTime) break;

            this.store.ingest(this._replayLines[this._replayIdx], lineTs || Date.now());
            this._sentenceCount++;
            this._replayIdx++;
            emitted++;
        }

        if (this._replayIdx >= this._replayLines.length) {
            this._setStatus('replay-done');
            return;
        }

        this._replayRafId = requestAnimationFrame(() => this._replayTick());
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
