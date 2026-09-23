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
        this._replayTimestamps = null;
        this._replayFirstTs = null;
        this._replayLastTs = null;
        this._loadGen = 0;
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

    /**
     * The one place the replay reaches its end, so cleanup cannot diverge.
     *
     * `reason` distinguishes playback genuinely running out ('end') from the user
     * dragging the scrubber to the end ('seek'). Auto-advance must only follow the
     * first: having the next hour launch itself because you scrubbed to the end is
     * not a feature.
     */
    _finishReplay(reason) {
        this._stopRateCounter();
        this._setStatus('replay-done');
        if (this.onReplayEnd) this.onReplayEnd(reason || 'end');
    }

    _stopRateCounter() {
        if (this._rateInterval) { clearInterval(this._rateInterval); this._rateInterval = null; }
    }

    _startRateCounter() {
        this._stopRateCounter();          // never stack two
        let prevCount = 0;
        this._rateInterval = setInterval(() => {
            this._sentenceRate = this._sentenceCount - prevCount;
            prevCount = this._sentenceCount;
            if (this._onStatus) this._onStatus(this._status, this._sentenceRate);
        }, 1000);
    }

    // --- File Replay ---

    /**
     * How far apart two recordings may be and still count as one continuous run.
     *
     * Generous enough to cover a capture restart (the logger respawns on a 10s
     * backoff, and a Pi reboot takes well under a minute), tight enough that a
     * different day's sailing is never treated as a continuation.
     */
    static get MAX_LOG_GAP_MS() { return 15 * 60 * 1000; }

    /**
     * Epoch ms for a log filename, e.g. 'nmea_2026-09-17_110000.txt'.
     *
     * Filenames are **local** time — see docs/logging-and-playback.md, "store UTC,
     * display local". Sentences inside carry UTC with a trailing Z; the filename is
     * the local half of that rule, so it is built from local date components here.
     * Returns null for anything unparseable, never NaN: NaN is not null, so it
     * slips through null checks and then compares false against every threshold.
     */
    static logStartTime(name) {
        const m = /(\d{4})-(\d{2})-(\d{2})[_T](\d{2})(\d{2})(\d{2})/.exec(String(name || ''));
        if (!m) return null;
        const y = +m[1], mo = +m[2], d = +m[3], h = +m[4], mi = +m[5], s = +m[6];
        const dt = new Date(y, mo - 1, d, h, mi, s, 0);
        // A shape-valid but impossible date (2026-13-45) silently rolls over into a
        // real one, so reject anything the Date did not preserve exactly.
        if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d ||
            dt.getHours() !== h || dt.getMinutes() !== mi || dt.getSeconds() !== s) return null;
        const t = dt.getTime();
        return Number.isFinite(t) ? t : null;
    }

    /**
     * The recording that continues `currentUrl`, if there is one.
     *
     * `files` is /api/logs' array ({name, url, bytes}), which arrives **newest
     * first**. Nothing here trusts that order: "the next option in the picker" is
     * an hour *earlier*, so stepping through the list as given plays a race
     * backwards. Sort by the filename's own timestamp instead.
     *
     * Contiguity is measured against the current log's **actual last sentence**,
     * not an assumed hour, because the logger does not only rotate on the hour — a
     * restart produces nmea_..._021420.txt right after nmea_..._020000.txt, and
     * "next = +1h" rejects a pair that is in truth one second apart.
     *
     * Returns {ok:true, file, gapMs} or {ok:false, reason, file?, gapMs?} where
     * reason is 'end' (nothing newer), 'gap' (a successor exists but too far off —
     * reported so the UI can say how far), or 'unknown' (the current recording is
     * not in the list, or has no usable end timestamp). A rejected successor must
     * never play: rolling a Saturday race into an unrelated Tuesday delivery would
     * look perfectly continuous and be wrong.
     */
    static nextContiguousLog(files, currentUrl, currentEndTs, maxGapMs = NmeaClient.MAX_LOG_GAP_MS) {
        const list = (files || [])
            .map(f => ({ file: f, ts: NmeaClient.logStartTime(f && f.name) }))
            .filter(e => e.ts !== null)
            .sort((a, b) => a.ts - b.ts);

        const curIdx = list.findIndex(e => e.file.url === currentUrl);
        if (curIdx < 0 || !Number.isFinite(currentEndTs)) return { ok: false, reason: 'unknown' };

        const next = list[curIdx + 1];
        if (!next) return { ok: false, reason: 'end' };

        // Symmetric tolerance: a small negative gap means the files overlap by a
        // few seconds, which is still one continuous run. A large one either way
        // is not.
        const gapMs = next.ts - currentEndTs;
        if (gapMs > maxGapMs || gapMs < -maxGapMs) {
            return { ok: false, reason: 'gap', file: next.file, gapMs };
        }
        return { ok: true, file: next.file, gapMs };
    }

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

    /**
     * Load a log straight off the Pi (/logs/<name>), no download step.
     *
     * Two guards, both necessary on boat WiFi:
     *  - A timeout. A half-open TCP connection at the edge of range never
     *    resolves and never rejects, so without this the clock sat on
     *    "loading…" forever while the PREVIOUS recording kept playing — you
     *    would be watching A while the picker said B.
     *  - A generation token. Two quick selections race, and whichever resolves
     *    last used to win. Pick a 40 MB morning log then a 2 MB afternoon one
     *    and the small one starts, then the big one replaces it underneath.
     */
    loadUrl(url, callback, onError, timeoutMs = 30000) {
        const gen = ++this._loadGen;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);

        fetch(url, { cache: 'no-store', signal: controller.signal })
            .then((r) => {
                if (!r.ok) throw new Error(`${r.status} fetching ${url}`);
                return r.text();
            })
            .then((text) => {
                if (gen !== this._loadGen) return;   // superseded; drop it
                this.loadText(text, callback);
            })
            .catch((err) => {
                if (gen !== this._loadGen) return;   // superseded; its error is moot
                // Surface it. Silently loading nothing would look like an empty
                // recording rather than a failed fetch, and leaving the previous
                // recording running would be worse still.
                this.stopReplay();
                const e = err && err.name === 'AbortError'
                    ? new Error(`timed out after ${timeoutMs / 1000}s fetching ${url}`)
                    : err;
                if (onError) onError(e);
                else this._setStatus('error');
            })
            .finally(() => clearTimeout(timer));
    }

    _parseReplayTimestamps() {
        const re = /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+Z?)/;
        this._replayTimestamps = this._replayLines.map(line => {
            const m = re.exec(line);
            if (!m) return null;
            const t = new Date(
                m[1].replace(' ', 'T').replace(',', '.') + (m[1].endsWith('Z') ? '' : 'Z')
            ).getTime();
            // A shape-valid but impossible date (2026-13-45…) yields NaN, which is
            // not null, so it slipped through every null check. _replayStartTime
            // became NaN, every `lineTs > targetTime` compared false, and the rest
            // of the log flushed at 500 lines a frame — a replay silently running
            // at max speed.
            return Number.isFinite(t) ? t : null;
        });

        // Cache the bounds. getReplayTimeRange() is called from a 250 ms timer and
        // on every scrubber input event; filtering ~150k entries each time to read
        // two numbers that never change was pure GC churn.
        const stamped = this._replayTimestamps.filter(t => t != null);
        this._replayFirstTs = stamped.length ? stamped[0] : null;
        this._replayLastTs = stamped.length ? stamped[stamped.length - 1] : null;
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

        // No parseable timestamps at all (a raw AIVDM dump, or an empty file the
        // rotation just created). Previously this returned before setting a
        // status, so the transport sat at 0% / --:--:-- with a Pause button,
        // forever, with nothing saying the recording was unusable.
        const firstTs = this._replayFirstTs;
        if (!firstTs) { this._setStatus('replay-empty'); return; }
        this._replayStartTime = firstTs;
        this._replayStartWall = performance.now();

        this._setStatus('replaying');
        this._replayTick();
    }

    _replayTick() {
        if (this._replayPaused) return;
        if (this._replayIdx >= this._replayLines.length) {
            this._finishReplay('end');
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
            this._finishReplay('end');
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
        } else if (target >= this._replayLines.length) {
            // Dragging the scrubber to max is trivially easy, and used to leave
            // the status on 'replaying' with the rate counter still ticking.
            this._finishReplay('seek');
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
        if (this._replayFirstTs == null) return null;
        const idx = Math.min(this._replayIdx, this._replayTimestamps.length - 1);
        let current = this._replayTimestamps[idx];
        if (current == null) current = this._replayFirstTs;
        return { start: this._replayFirstTs, end: this._replayLastTs, current };
    }

    pauseReplay() {
        if (!this._replayLines) return;
        this._replayPaused = true;
        this._setStatus('replay-paused');
    }

    resumeReplay() {
        if (!this._replayLines || !this._replayTimestamps) return;
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
        // The transport bar shows speed/play before a recording is picked, so
        // these are reachable with no log loaded. They used to throw a TypeError
        // indexing _replayTimestamps, leaving the bar visibly dead.
        if (!this._replayTimestamps) { this._replaySpeed = speed; return; }
        const lineTs = this._replayTimestamps[this._replayIdx];
        if (lineTs) {
            this._replayStartTime = lineTs;
            this._replayStartWall = performance.now();
        }
        this._replaySpeed = speed;
    }

    stopReplay() {
        if (this._replayRafId) { cancelAnimationFrame(this._replayRafId); this._replayRafId = null; }
        this._stopRateCounter();
        // Clear the parsed log too, not just the lines: a leftover timestamp array
        // would let getReplayTimeRange() keep reporting a range for a recording
        // that is no longer loaded, and the transport would show its clock.
        this._replayLines = null;
        this._replayTimestamps = null;
        this._replayFirstTs = null;
        this._replayLastTs = null;
        this._replayIdx = 0;
        this._replayPaused = false;
        // Invalidate any in-flight loadUrl, so a slow fetch cannot resurrect a
        // replay after the user has left it.
        this._loadGen++;
        this._setStatus('disconnected');
    }

    getReplayProgress() {
        if (!this._replayLines) return null;
        return { current: this._replayIdx, total: this._replayLines.length,
                 pct: Math.round(this._replayIdx / this._replayLines.length * 100) };
    }
}
