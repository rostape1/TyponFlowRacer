/**
 * Browser-side AIS decoder for raw !AIVDM/!AIVDO sentences.
 * Output matches the shape aisstream.js parseAISStreamMessage() produces.
 */

const AISDecoder = (() => {

    function charToSixBit(c) {
        const v = c.charCodeAt(0);
        if (v >= 48 && v <= 87) return v - 48;
        if (v >= 96 && v <= 119) return v - 56;
        return 0;
    }

    function decodeBits(payload) {
        const bits = [];
        for (let i = 0; i < payload.length; i++) {
            const v = charToSixBit(payload[i]);
            for (let b = 5; b >= 0; b--) bits.push((v >> b) & 1);
        }
        return bits;
    }

    function getUint(bits, start, len) {
        let v = 0;
        for (let i = start; i < start + len && i < bits.length; i++) {
            v = (v << 1) | bits[i];
        }
        return v;
    }

    function getInt(bits, start, len) {
        let v = getUint(bits, start, len);
        if (bits[start] === 1) v -= (1 << len);
        return v;
    }

    function getText(bits, start, len) {
        const chars = [];
        for (let i = start; i < start + len; i += 6) {
            let c = getUint(bits, i, 6);
            if (c < 32) c += 64;
            chars.push(String.fromCharCode(c));
        }
        return chars.join('').replace(/@+$/, '').trim();
    }

    function decodeType123(bits) {
        const mmsi = getUint(bits, 8, 30);
        const status = getUint(bits, 38, 4);
        const rot = getInt(bits, 42, 8);
        const sog = getUint(bits, 50, 10) / 10;
        const lon = getInt(bits, 61, 28) / 600000;
        const lat = getInt(bits, 89, 27) / 600000;
        const cog = getUint(bits, 116, 12) / 10;
        const heading = getUint(bits, 128, 9);

        if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
        if (lat === 0 && lon === 0) return null;

        return {
            mmsi, msg_type: getUint(bits, 0, 6),
            lat: Math.round(lat * 1e6) / 1e6,
            lon: Math.round(lon * 1e6) / 1e6,
            sog: sog < 102.3 ? Math.round(sog * 10) / 10 : null,
            cog: cog < 360 ? Math.round(cog * 10) / 10 : null,
            heading: heading < 360 ? heading : null,
            nav_status: status,
        };
    }

    function decodeType5(bits) {
        const mmsi = getUint(bits, 8, 30);
        const name = getText(bits, 112, 120);
        const shipType = getUint(bits, 232, 8);
        const dimA = getUint(bits, 240, 9);
        const dimB = getUint(bits, 249, 9);
        const dimC = getUint(bits, 258, 6);
        const dimD = getUint(bits, 264, 6);
        const dest = getText(bits, 302, 120);

        const result = { mmsi, msg_type: 5, name: name || null };
        if (shipType) {
            result.ship_type = shipType;
            result.ship_category = getShipCategoryLocal(shipType);
        }
        if (dimA + dimB > 0) result.length = dimA + dimB;
        if (dimC + dimD > 0) result.beam = dimC + dimD;
        if (dest) result.destination = dest;
        return result;
    }

    function decodeType18(bits) {
        const mmsi = getUint(bits, 8, 30);
        const sog = getUint(bits, 46, 10) / 10;
        const lon = getInt(bits, 57, 28) / 600000;
        const lat = getInt(bits, 85, 27) / 600000;
        const cog = getUint(bits, 112, 12) / 10;
        const heading = getUint(bits, 124, 9);

        if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
        if (lat === 0 && lon === 0) return null;

        return {
            mmsi, msg_type: 18,
            lat: Math.round(lat * 1e6) / 1e6,
            lon: Math.round(lon * 1e6) / 1e6,
            sog: sog < 102.3 ? Math.round(sog * 10) / 10 : null,
            cog: cog < 360 ? Math.round(cog * 10) / 10 : null,
            heading: heading < 360 ? heading : null,
        };
    }

    function decodeType19(bits) {
        const result = decodeType18(bits);
        if (!result) return null;
        result.msg_type = 19;
        result.name = getText(bits, 143, 120) || null;
        const shipType = getUint(bits, 263, 8);
        if (shipType) {
            result.ship_type = shipType;
            result.ship_category = getShipCategoryLocal(shipType);
        }
        return result;
    }

    function decodeType24(bits) {
        const mmsi = getUint(bits, 8, 30);
        const partNo = getUint(bits, 38, 2);
        if (partNo === 0) {
            return { mmsi, msg_type: 24, name: getText(bits, 40, 120) || null };
        } else if (partNo === 1) {
            const shipType = getUint(bits, 40, 8);
            const result = { mmsi, msg_type: 24 };
            if (shipType) {
                result.ship_type = shipType;
                result.ship_category = getShipCategoryLocal(shipType);
            }
            return result;
        }
        return null;
    }

    const SHIP_TYPE_MAP = [
        [20, 30, 'Wing in Ground'], [30, 36, 'Fishing/Towing/Dredging'],
        [36, 40, 'Sailing/Pleasure'], [40, 50, 'High Speed Craft'],
        [50, 60, 'Special Craft'], [60, 70, 'Passenger'],
        [70, 80, 'Cargo'], [80, 90, 'Tanker'], [90, 100, 'Other'],
    ];

    function getShipCategoryLocal(t) {
        for (const [lo, hi, cat] of SHIP_TYPE_MAP) {
            if (t >= lo && t < hi) return cat;
        }
        return 'Other';
    }

    const DECODERS = {
        1: decodeType123, 2: decodeType123, 3: decodeType123,
        5: decodeType5, 18: decodeType18, 19: decodeType19, 24: decodeType24,
    };

    const fragmentBuffer = new Map();
    const FRAGMENT_TIMEOUT = 10000;

    function processSentence(sentence) {
        const starIdx = sentence.indexOf('*');
        const body = starIdx >= 0 ? sentence.slice(1, starIdx) : sentence.slice(1);
        const fields = body.split(',');

        // fields[0] = talker (AIVDM/AIVDO), rest follow standard order
        const fragCount = parseInt(fields[1]);
        const fragNum = parseInt(fields[2]);
        const seqId = fields[3] || '';
        const channel = fields[4] || '';
        const payload = fields[5] || '';
        const fillBits = parseInt(fields[6]) || 0;

        if (fragCount === 1) {
            return decode(payload, fillBits);
        }

        const key = `${channel}:${seqId}`;
        if (fragNum === 1) {
            fragmentBuffer.set(key, { parts: [payload], count: fragCount, time: Date.now() });
        } else {
            const buf = fragmentBuffer.get(key);
            if (!buf) return null;
            buf.parts.push(payload);
            if (buf.parts.length === buf.count) {
                fragmentBuffer.delete(key);
                return decode(buf.parts.join(''), fillBits);
            }
        }

        for (const [k, v] of fragmentBuffer) {
            if (Date.now() - v.time > FRAGMENT_TIMEOUT) fragmentBuffer.delete(k);
        }

        return null;
    }

    function decode(payload, fillBits) {
        const bits = decodeBits(payload);
        const msgType = getUint(bits, 0, 6);
        const decoder = DECODERS[msgType];
        if (!decoder) return null;
        const result = decoder(bits);
        if (result) {
            result.timestamp = new Date().toISOString();
            result.is_own_vessel = false;
        }
        return result;
    }

    return { processSentence, decode };
})();
