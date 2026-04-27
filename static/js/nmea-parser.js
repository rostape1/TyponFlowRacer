/**
 * Pure NMEA 0183 sentence parser — stateless, no I/O.
 */

const NmeaParser = (() => {

    function validateChecksum(sentence) {
        const starIdx = sentence.indexOf('*');
        if (starIdx < 0) return false;
        const body = sentence.slice(1, starIdx);
        let xor = 0;
        for (let i = 0; i < body.length; i++) xor ^= body.charCodeAt(i);
        return xor === parseInt(sentence.slice(starIdx + 1, starIdx + 3), 16);
    }

    function parseLatLon(latStr, ns, lonStr, ew) {
        if (!latStr || !lonStr) return null;
        const latDeg = parseInt(latStr.slice(0, 2));
        const latMin = parseFloat(latStr.slice(2));
        let lat = latDeg + latMin / 60;
        if (ns === 'S') lat = -lat;

        const lonDeg = parseInt(lonStr.slice(0, 3));
        const lonMin = parseFloat(lonStr.slice(3));
        let lon = lonDeg + lonMin / 60;
        if (ew === 'W') lon = -lon;
        return { lat: Math.round(lat * 1e6) / 1e6, lon: Math.round(lon * 1e6) / 1e6 };
    }

    function pf(v) { const n = parseFloat(v); return isNaN(n) ? null : n; }

    function parseGGA(f) {
        const pos = parseLatLon(f[2], f[3], f[4], f[5]);
        if (!pos) return null;
        return { type: 'GGA', ...pos, fix: parseInt(f[6]) || 0, satellites: parseInt(f[7]) || 0,
                 hdop: pf(f[8]), alt: pf(f[9]) };
    }

    function parseRMC(f) {
        if (f[2] === 'V' && !f[3]) return null;
        const pos = parseLatLon(f[3], f[4], f[5], f[6]);
        return { type: 'RMC', ...(pos || {}), sog: pf(f[7]), cog: pf(f[8]),
                 valid: f[2] === 'A' };
    }

    function parseHDG(f) {
        const h = pf(f[1]);
        if (h === null) return null;
        return { type: 'HDG', heading: h, deviation: pf(f[2]), variation: pf(f[4]) };
    }

    function parseMWV(f) {
        if (f[5] !== 'A') return null;
        const angle = pf(f[1]);
        const speed = pf(f[3]);
        if (angle === null || speed === null) return null;
        const ref = f[2];
        let speedKn = speed;
        if (f[4] === 'M') speedKn = speed * 1.94384;
        else if (f[4] === 'K') speedKn = speed * 0.539957;
        return { type: 'MWV', angle, reference: ref, speed: speedKn };
    }

    function parseMWD(f) {
        const dirTrue = pf(f[1]);
        const speedKn = pf(f[5]);
        const speedMs = pf(f[7]);
        if (dirTrue === null && speedKn === null && speedMs === null) return null;
        return { type: 'MWD', dirTrue, dirMag: pf(f[3]),
                 speedKn: speedKn != null ? speedKn : (speedMs != null ? speedMs * 1.94384 : null),
                 speedMs: speedMs != null ? speedMs : (speedKn != null ? speedKn * 0.514444 : null) };
    }

    function parseVHW(f) {
        const bsp = pf(f[5]);
        if (bsp === null) return null;
        return { type: 'VHW', headingTrue: pf(f[1]), headingMag: pf(f[3]), bsp };
    }

    function parseDPT(f) {
        const d = pf(f[1]);
        if (d === null) return null;
        return { type: 'DPT', depth: d, offset: pf(f[2]) };
    }

    function parseVTG(f) {
        return { type: 'VTG', cogTrue: pf(f[1]), sogKn: pf(f[5]) };
    }

    function parseROT(f) {
        if (f[2] !== 'A') return null;
        const rate = pf(f[1]);
        if (rate === null) return null;
        return { type: 'ROT', rate };
    }

    function parseXDR(f) {
        const result = { type: 'XDR' };
        for (let i = 1; i + 3 < f.length; i += 4) {
            const val = pf(f[i + 1]);
            const label = (f[i + 3] || '').toLowerCase();
            if (label === 'yaw') result.yaw = val;
            else if (label === 'pitch') result.pitch = val;
            else if (label === 'roll') result.roll = val;
        }
        return result;
    }

    const PARSERS = {
        'GPGGA': parseGGA, 'GNGGA': parseGGA,
        'GPRMC': parseRMC, 'GNRMC': parseRMC,
        'HCHDG': parseHDG, 'IIHDG': parseHDG,
        'IIMWV': parseMWV,
        'IIMWD': parseMWD,
        'IIVHW': parseVHW,
        'IIDPT': parseDPT, 'SDDPT': parseDPT,
        'IIVTG': parseVTG, 'GPVTG': parseVTG,
        'TIROT': parseROT,
        'YXXDR': parseXDR,
    };

    const LOG_LINE_RE = /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+Z?)\s+(?:\[.*?\]\s+)?(.+)$/;

    function parseLine(line) {
        if (!line || line.startsWith('#')) return null;

        let timestamp = null;
        let sentence = line;

        const m = LOG_LINE_RE.exec(line);
        if (m) {
            timestamp = new Date(m[1].replace(' ', 'T').replace(',', '.') + (m[1].endsWith('Z') ? '' : 'Z'));
            sentence = m[2].trim();
        }

        if (sentence.startsWith('!')) {
            return { timestamp, sentence, isAIS: true };
        }

        if (!sentence.startsWith('$')) return null;
        if (!validateChecksum(sentence)) return null;

        const starIdx = sentence.indexOf('*');
        const body = sentence.slice(1, starIdx);
        const fields = body.split(',');
        const sentenceId = fields[0];

        const parser = PARSERS[sentenceId];
        if (!parser) return null;

        const result = parser(fields);
        if (!result) return null;

        result.timestamp = timestamp;
        result.sentence = sentence;
        return result;
    }

    return { parseLine, validateChecksum, parseLatLon };
})();
