/**
 * AISstream.io direct browser WebSocket client.
 *
 * Connects to AISstream.io, parses vessel messages, and calls back
 * with parsed vessel data in the same format the old backend WebSocket used.
 */

// AIS ship type ranges → human-readable categories
const SHIP_TYPE_MAP = [
    { range: [20, 30], category: 'Wing in Ground' },
    { range: [30, 36], category: 'Fishing/Towing/Dredging' },
    { range: [36, 40], category: 'Sailing/Pleasure' },
    { range: [40, 50], category: 'High Speed Craft' },
    { range: [50, 60], category: 'Special Craft' },
    { range: [60, 70], category: 'Passenger' },
    { range: [70, 80], category: 'Cargo' },
    { range: [80, 90], category: 'Tanker' },
    { range: [90, 100], category: 'Other' },
];

function getShipCategory(shipType) {
    if (shipType == null) return 'Unknown';
    for (const { range, category } of SHIP_TYPE_MAP) {
        if (shipType >= range[0] && shipType < range[1]) return category;
    }
    return 'Other';
}

// AISstream message type → AIS msg_type number
const MSG_TYPE_MAP = {
    PositionReport: 1,
    StandardClassBPositionReport: 18,
    ExtendedClassBPositionReport: 19,
    StaticDataReport: 5,
    ShipStaticData: 5,
    StandardSearchAndRescueAircraftReport: 9,
    AidsToNavigationReport: 21,
};

/**
 * Parse an AISstream.io JSON message into our internal vessel format.
 * Returns null if the message can't be parsed.
 */
function parseAISStreamMessage(raw, ownMmsi) {
    try {
        const meta = raw.MetaData || {};
        const msgTypeName = raw.MessageType || '';
        const message = raw.Message || {};

        const mmsi = meta.MMSI;
        if (!mmsi) return null;

        const result = {
            mmsi: mmsi,
            msg_type: MSG_TYPE_MAP[msgTypeName] || 0,
            is_own_vessel: mmsi === ownMmsi,
            timestamp: new Date().toISOString(),
            name: (meta.ShipName || '').trim() || null,
        };

        // Get the actual message payload (nested under the message type key)
        const payload = message[msgTypeName] || {};

        // Position data
        const lat = payload.Latitude;
        const lon = payload.Longitude;
        if (lat != null && lon != null && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
            result.lat = Math.round(lat * 1e6) / 1e6;
            result.lon = Math.round(lon * 1e6) / 1e6;
        }

        const sog = payload.Sog;
        if (sog != null && sog < 102.3) {
            result.sog = Math.round(sog * 10) / 10;
        }

        const cog = payload.Cog;
        if (cog != null && cog < 360.0) {
            result.cog = Math.round(cog * 10) / 10;
        }

        const heading = payload.TrueHeading;
        if (heading != null && heading < 360) {
            result.heading = heading;
        }

        // Static data
        const shipType = payload.Type;
        if (shipType != null) {
            result.ship_type = shipType;
            result.ship_category = getShipCategory(shipType);
        }

        const dest = payload.Destination;
        if (dest && dest.trim()) {
            result.destination = dest.trim();
        }

        const shipname = payload.ShipName;
        if (shipname && shipname.trim()) {
            result.shipname = shipname.trim();
        }

        // Dimensions
        const dim = payload.Dimension || {};
        if (dim) {
            const a = dim.A || 0;
            const b = dim.B || 0;
            const c = dim.C || 0;
            const d = dim.D || 0;
            if (a + b > 0) {
                result.length = a + b;
                result.to_bow = a;
                result.to_stern = b;
            }
            if (c + d > 0) {
                result.beam = c + d;
                result.to_port = c;
                result.to_starboard = d;
            }
        }

        return result;
    } catch (e) {
        console.debug('Failed to parse AISstream message:', e);
        return null;
    }
}

/**
 * AISstream WebSocket client.
 *
 * Usage:
 *   const client = new AISStreamClient({
 *     apiKey: 'your-key',
 *     bbox: [[37.4, -122.8], [38.2, -122.0]],
 *     ownMmsi: 338361814,
 *     onMessage: (vesselData) => { ... },
 *     onStatus: (status) => { ... },  // 'connected', 'disconnected', 'error'
 *   });
 *   client.connect();
 */
class AISStreamClient {
    constructor({ apiKey, bbox, ownMmsi, onMessage, onStatus }) {
        this.apiKey = apiKey;
        this.bbox = bbox || [[37.4, -122.8], [38.2, -122.0]];
        this.ownMmsi = ownMmsi;
        this.onMessage = onMessage;
        this.onStatus = onStatus || (() => {});
        this.ws = null;
        this._reconnectTimer = null;
        this._stopped = false;
    }

    connect() {
        this._stopped = false;
        this._doConnect();
    }

    disconnect() {
        this._stopped = true;
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    _doConnect() {
        if (this._stopped) return;

        try {
            this.ws = new WebSocket('wss://stream.aisstream.io/v0/stream');
        } catch (e) {
            console.error('WebSocket creation failed:', e);
            this._scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            const sub = JSON.stringify({
                APIKey: this.apiKey,
                BoundingBoxes: [this.bbox],
            });
            console.log('[AIS] WebSocket open, sending subscription');
            this.ws.send(sub);
            this.onStatus('connected');
        };

        this.ws.onmessage = (event) => {
            try {
                const raw = JSON.parse(event.data);
                const parsed = parseAISStreamMessage(raw, this.ownMmsi);
                if (parsed) {
                    this.onMessage(parsed);
                } else {
                    console.warn('[AIS] Message parsed to null:', raw.MessageType);
                }
            } catch (e) {
                console.error('[AIS] onmessage error:', e, event.data?.slice?.(0, 200));
            }
        };

        this.ws.onclose = (e) => {
            console.log('[AIS] WebSocket closed:', e.code, e.reason);
            this.onStatus('disconnected');
            this._scheduleReconnect();
        };

        this.ws.onerror = (e) => {
            console.error('[AIS] WebSocket error:', e);
            this.onStatus('error');
            this.ws.close();
        };
    }

    _scheduleReconnect() {
        if (this._stopped) return;
        this._reconnectTimer = setTimeout(() => this._doConnect(), 5000);
    }
}
