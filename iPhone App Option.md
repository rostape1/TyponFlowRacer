# AIS Tracker — iPhone App Plan

## Overview

Native iOS app that replicates the full AIS Tracker web app. Uses a **WKWebView hybrid** architecture: the existing HTML/JS/CSS frontend runs unchanged inside a native shell, with Swift modules replacing the Python backend. Everything runs on the phone — no server to maintain.

## Architecture

```
┌────────────────────────────────────────────────────────┐
│  WKWebView                                             │
│  ┌──────────────────────────────────────────────────┐  │
│  │  index.html + app.js + style.css                 │  │
│  │  tidal-flow.js (canvas particles)                │  │
│  │  wind-overlay.js (canvas particles)              │  │
│  │  Leaflet map, popups, panels, timeline           │  │
│  └──────────────┬───────────────────────────────────┘  │
│                 │                                       │
│    window.webkit.messageHandlers.native.postMessage()  │
│    ← evaluateJavaScript() callbacks                    │
│                 │                                       │
├─────────────────┼──────────────────────────────────────┤
│  Swift Native   │                                      │
│  ┌──────────────┴───────────────────────────────────┐  │
│  │  NativeBridge (WKScriptMessageHandler)           │  │
│  │  Routes messages to correct module               │  │
│  └──┬──────┬──────┬──────┬──────┬──────────────────┘  │
│     │      │      │      │      │                      │
│  DataFetch SFBOFS  AIS   AIS   SQLite                  │
│  (NOAA)   Loader  Socket Decode  DB                    │
│     │      │      │      │      │                      │
│  URLSession NWConnection  CoreData/SQLite.swift        │
└────────────────────────────────────────────────────────┘
```

## Why WKWebView Hybrid (Not Full SwiftUI Rewrite)

1. **Keeps all existing UI** — Leaflet map, glassmorphism panels, Canvas particle animations, responsive layout, popups, search, forecast timeline. Zero UI rewrite.
2. **Unfinished UI changes carry forward** — any CSS/JS work in progress just works.
3. **Canvas animations perform well in WKWebView** — tidal-flow.js (2000-3000 particles) and wind-overlay.js (800 particles) run at 60fps on modern iPhones.
4. **Only 4-5 Swift modules needed** — replacing the Python backend, not the frontend.
5. **A full SwiftUI + Metal rewrite would take months** — particle animations alone would be weeks of Metal shader work.

---

## Module 1: NativeBridge (JS ↔ Swift)

### What It Replaces
The `fetch('/api/...')` calls and WebSocket connection in app.js.

### JS-Side Changes (~50 lines)

Replace all `fetch('/api/...')` calls with bridge messages. The existing calls are:

| Current JS Call | Bridge Message | Response Callback |
|----------------|---------------|-------------------|
| `fetch('/api/vessels')` | `{type: 'vessels'}` | `window.nativeCallback.vessels(json)` |
| `fetch('/api/vessels/{mmsi}/track?hours=2')` | `{type: 'track', mmsi, hours}` | `window.nativeCallback.track(json)` |
| `fetch('/api/stats')` | `{type: 'stats'}` | `window.nativeCallback.stats(json)` |
| `fetch('/api/currents?time=N')` | `{type: 'currents', time: N}` | `window.nativeCallback.currents(json)` |
| `fetch('/api/current-field?time=N')` | `{type: 'currentField', time: N}` | `window.nativeCallback.currentField(json)` |
| `fetch('/api/wind-field?time=N')` | `{type: 'windField', time: N}` | `window.nativeCallback.windField(json)` |
| `fetch('/api/tide-height?time=N')` | `{type: 'tideHeight', time: N}` | `window.nativeCallback.tideHeight(json)` |
| `new WebSocket('/ws')` | `{type: 'startAIS'}` | `window.nativeCallback.vesselUpdate(json)` (called per update) |

### Swift-Side Implementation

```swift
class NativeBridge: NSObject, WKScriptMessageHandler {
    func userContentController(_ controller: WKUserContentController,
                                didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }
        
        switch type {
        case "vessels":     dataFetcher.getAllVessels { self.callback("vessels", $0) }
        case "track":       dataFetcher.getTrack(mmsi, hours) { ... }
        case "currents":    dataFetcher.getCurrents(time) { ... }
        case "currentField": sfbofsLoader.getField(time) { ... }
        case "windField":   dataFetcher.getWindField(time) { ... }
        case "tideHeight":  dataFetcher.getTideHeight(time) { ... }
        case "startAIS":    aisSocket.start()
        default: break
        }
    }
    
    func callback(_ name: String, _ json: String) {
        webView.evaluateJavaScript("window.nativeCallback.\(name)(\(json))")
    }
}
```

### Offline/Cache Header Simulation

The web app checks the `X-From-Cache` response header to show offline banners and data freshness indicators. In the native bridge, the Swift side should include a `fromCache` boolean in every response. The JS callback wrapper translates this to the same UI behavior (yellow dot, "2h 30m ago" label, offline banner).

---

## Module 2: SFBOFSLoader (Current Field — The Complex One)

### What It Replaces
`sfbofs.py` — downloads NOAA SFBOFS hydrodynamic model data and regrids to a regular lat/lon grid for the particle animation.

### Why This Is Special
The SFBOFS data is published as 57MB NetCDF files on AWS S3. The phone only needs 4 arrays totaling 1.6MB. We use **HTTP Range requests** to extract just those bytes — no NetCDF library needed.

### The Data

SFBOFS is an unstructured FVCOM (Finite Volume Community Ocean Model) grid with 102,264 triangular cells covering SF Bay at ~200m resolution. Each file contains 3D ocean data (20 depth layers, temperature, salinity, etc.) but we only need surface current velocity.

**What we extract:**

| Variable | Array Size | Bytes | Purpose |
|----------|-----------|-------|---------|
| `latc` | 102,264 float32 | 399 KB | Cell center latitudes |
| `lonc` | 102,264 float32 | 399 KB | Cell center longitudes |
| `u[0][0][:]` | 102,264 float32 | 399 KB | Surface eastward velocity (m/s) |
| `v[0][0][:]` | 102,264 float32 | 399 KB | Surface northward velocity (m/s) |
| **Total** | | **1.6 MB** | **2.9% of the 57MB file** |

### S3 File Layout & Byte Offsets

The files are NetCDF4 (which is HDF5 internally). The data is stored as **uncompressed, contiguous float32 arrays** — meaning raw bytes on disk are directly readable as IEEE 754 floats. No decompression, no decoding — just `Data` → `[Float]`.

Through analysis of multiple SFBOFS files across different dates, model runs, and forecast hours, the byte offsets are **identical in every file**:

```
File: sfbofs.t{HH}z.{YYYYMMDD}.fields.n{FFF}.nc
Total size: 56,605,561 bytes (always)

latc:       offset 2,597,959   length 409,056 bytes (102,264 × 4)
lonc:       offset 2,188,903   length 409,056 bytes
u chunk 1:  offset 26,940,241  length 204,528 bytes (51,132 × 4)
u chunk 2:  offset 28,985,521  length 204,528 bytes (51,132 × 4)
v chunk 1:  offset 35,124,497  length 204,528 bytes (51,132 × 4)
v chunk 2:  offset 37,169,777  length 204,528 bytes (51,132 × 4)
```

**Why u and v have two chunks each:**
The `u` array shape is `(1, 20, 102264)` — 1 timestep × 20 sigma depth layers × 102,264 cells. HDF5 stores it in chunks of `[1, 10, 51132]`. So the surface layer (sigma=0) data is split: the first 51,132 cells in chunk 1, the remaining 51,132 in chunk 2. Between the chunks is data for sigma layers 1-9 (which we skip). Same for `v`.

**Verified identical across:**
- `sfbofs.t09z.20260412.fields.n000.nc` (Apr 12, 09z run, nowcast)
- `sfbofs.t21z.20260411.fields.n000.nc` (Apr 11, 21z run, nowcast)  
- `sfbofs.t09z.20260412.fields.n006.nc` (Apr 12, 09z run, 6h forecast)
- All three: file size 56,605,561, identical offsets

This stability makes sense: NOAA generates every file with the same FVCOM output pipeline on the same mesh. The grid geometry never changes — only the velocity values differ.

### Fetch Strategy (Three-Tier)

```
Tier 1: Range Requests (800 KB, ~1 second)
    ↓ validation fails?
Tier 2: Full File Download + libhdf5 (57 MB, ~30 seconds on WiFi)
    ↓ can't parse?
Tier 3: Error state (show user message, retry later)
```

**Important: Station IDW is NOT a fallback.** The full 200m resolution grid is required.

#### Tier 1: Range Requests (Primary Path)

```swift
class SFBOFSLoader {
    // Hardcoded offsets — verified stable across all SFBOFS files
    static let LATC_OFFSET:  UInt64 = 2_597_959
    static let LONC_OFFSET:  UInt64 = 2_188_903
    static let U_CHUNK1:     UInt64 = 26_940_241
    static let U_CHUNK2:     UInt64 = 28_985_521
    static let V_CHUNK1:     UInt64 = 35_124_497
    static let V_CHUNK2:     UInt64 = 37_169_777
    static let CELL_COUNT:   Int    = 102_264
    static let CHUNK_CELLS:  Int    = 51_132
    static let FLOAT_SIZE:   Int    = 4
    
    // Known first 5 latc values for validation
    static let LATC_VALIDATION: [Float] = [37.993706, 37.99439, 37.99197, 37.991085, 37.98969]
    
    func fetchSurfaceCurrents(date: String, runHour: String, forecastHour: Int) async throws -> SFBOFSGrid {
        let url = buildS3URL(date: date, runHour: runHour, forecastHour: forecastHour)
        
        // Step 1: Fetch latc with validation (or use bundled mesh)
        let latc = try await fetchRange(url: url, offset: Self.LATC_OFFSET, 
                                         count: Self.CELL_COUNT)
        
        // Validate: first 5 values must match known mesh coordinates
        guard latc.prefix(5).enumerated().allSatisfy({ 
            abs($0.element - Self.LATC_VALIDATION[$0.offset]) < 0.0001 
        }) else {
            throw SFBOFSError.offsetsChanged  // Triggers Tier 2 fallback
        }
        
        // Step 2: Fetch lonc, u, v in parallel
        async let lonc = fetchRange(url: url, offset: Self.LONC_OFFSET, count: Self.CELL_COUNT)
        async let u1 = fetchRange(url: url, offset: Self.U_CHUNK1, count: Self.CHUNK_CELLS)
        async let u2 = fetchRange(url: url, offset: Self.U_CHUNK2, count: Self.CHUNK_CELLS)
        async let v1 = fetchRange(url: url, offset: Self.V_CHUNK1, count: Self.CHUNK_CELLS)
        async let v2 = fetchRange(url: url, offset: Self.V_CHUNK2, count: Self.CHUNK_CELLS)
        
        let uSurface = try await u1 + u2  // Concatenate chunks
        let vSurface = try await v1 + v2
        
        // Step 3: Regrid to regular lat/lon grid
        return regrid(latc: latc, lonc: try await lonc, u: uSurface, v: vSurface)
    }
    
    func fetchRange(url: URL, offset: UInt64, count: Int) async throws -> [Float] {
        var request = URLRequest(url: url)
        let end = offset + UInt64(count * Self.FLOAT_SIZE) - 1
        request.setValue("bytes=\(offset)-\(end)", forHTTPHeaderField: "Range")
        
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 206 else {
            throw SFBOFSError.rangeRequestFailed
        }
        
        // Raw bytes → Float array (no library needed)
        return data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
    }
}
```

#### Tier 2: Full File Fallback

If validation fails (NOAA changed their pipeline), download the full 57MB file and parse with libhdf5:

```swift
func fetchFullFile(url: URL) async throws -> SFBOFSGrid {
    // Download to temporary file
    let (tempURL, _) = try await URLSession.shared.download(from: url)
    
    // Parse with libhdf5 (linked via Swift Package Manager)
    let file = try HDF5File(path: tempURL.path)
    let latc = try file.readDataset("latc", as: [Float].self)
    let lonc = try file.readDataset("lonc", as: [Float].self)
    let u = try file.readDataset("u", as: [[[Float]]].self)  // (1, 20, 102264)
    let v = try file.readDataset("v", as: [[[Float]]].self)
    
    let uSurface = u[0][0]  // First timestep, first sigma layer
    let vSurface = v[0][0]
    
    // Log new offsets so we can update the app
    logNewOffsets(file: file)
    
    return regrid(latc: latc, lonc: lonc, u: uSurface, v: vSurface)
}
```

**libhdf5 on iOS:** The C library compiles for iOS via Xcode. Wrap it with a thin Swift bridging header. Only used as fallback — not in the hot path. Several Swift packages exist (e.g., `SwiftHDF5`). The app should link it regardless so the fallback works without a separate download.

#### Mesh Optimization

The `latc` and `lonc` arrays are the FVCOM mesh coordinates — they **never change** between files. The mesh is a fixed geometry of SF Bay.

**Bundle the mesh in the app binary:**
- Save `latc` (399 KB) and `lonc` (399 KB) as raw float data in the app bundle
- On each fetch, only download u and v (4 Range requests, 800 KB total)
- Validate by checking file size still equals 56,605,561 bytes before using Range offsets
- If file size changes → mesh might have changed → Tier 2 full download

This cuts the per-update download to **800 KB on cellular**.

### S3 URL Construction

```
https://noaa-nos-ofs-pds.s3.amazonaws.com/sfbofs/netcdf/{YYYY}/{MM}/{DD}/sfbofs.t{HH}z.{YYYYMMDD}.fields.n{FFF}.nc

Where:
  YYYY/MM/DD = model run date
  HH = model run hour: 03, 09, 15, 21 (four runs per day)
  FFF = forecast hour: 000-048

Model runs take ~3 hours to process, so:
  - Check most recent run first (e.g., t21z, t15z, t09z, t03z)
  - Skip runs that haven't had time to publish yet
  - Fall back to yesterday if today's runs aren't available
  
Same logic as current sfbofs.py _find_latest_run() (lines 75-103)
```

### Regridding (Delaunay Triangulation)

The FVCOM mesh is unstructured (102,264 irregular triangles). The frontend particle animation expects a regular grid. Regridding converts from irregular → regular.

**Input:** 102,264 points with (lat, lon, u, v) at irregular positions
**Output:** 276 × 325 regular grid (0.002° spacing ≈ 200m) with interpolated u, v

**Algorithm (same as scipy.griddata with method='linear'):**

1. **Build Delaunay triangulation** of the 102,264 input points
   - One-time operation (mesh never changes) — can be precomputed and bundled
   - Swift implementations: `DelaunayTriangulation` packages, or Apple's `GameplayKit.GKMeshGraph`
   
2. **For each output grid cell** (276 × 325 = 89,700 cells):
   - Find which Delaunay triangle contains this point
   - Compute barycentric coordinates (3 weights that sum to 1.0)
   - Interpolate: `u_out = w1*u1 + w2*u2 + w3*u3`

**Performance on iPhone:**
- Delaunay of 102K points: ~50ms (one-time, can be pre-bundled)
- Point location + interpolation for 89,700 grid cells: ~30ms
- Total: <100ms per update

**Precomputation optimization:**
Since the mesh and output grid are fixed, the triangle assignments and barycentric weights are also fixed. Precompute once, bundle in app:
```swift
struct InterpolationWeight {
    let triangleIndex: Int
    let w1: Float, w2: Float, w3: Float
    let i1: Int, i2: Int, i3: Int  // indices into latc/lonc arrays
}
// 89,700 weights × 28 bytes = ~2.5 MB bundled data
// Then each update is just: for each cell, u_out = w1*u[i1] + w2*u[i2] + w3*u[i3]
// That's a single vectorized multiply-add — Accelerate framework does it in <5ms
```

### Bounding Box & Coordinate Notes

```swift
let bounds = (south: 37.40, north: 38.05, west: -122.65, east: -122.10)
let gridSpacing = 0.002  // degrees (~200m)
let nx = 276  // (east - west) / gridSpacing + 1
let ny = 325  // (north - south) / gridSpacing + 1
```

**Longitude conversion:** SFBOFS files store longitude as 0-360 (e.g., 236.97 instead of -122.03). Convert: `if lon > 180 { lon -= 360 }`. The bundled lonc array should already be converted.

**Units:** SFBOFS velocities are in m/s. Convert to knots: `× 1.94384`

### Caching

- **Memory cache:** Dictionary keyed by forecast_hour (0-48), same as Python `_grid_cache`
- **Disk cache:** Save regridded JSON to app's Documents directory
- **TTL:** 6 hours (same as Python), then re-fetch
- **Startup:** Load disk cache immediately so forecast mode works without waiting for download
- **Background App Refresh:** iOS can wake the app periodically to refresh data on WiFi

---

## Module 3: DataFetcher (NOAA APIs — The Easy Ones)

### Wind Field
Replaces `wind.py`. All plain JSON REST APIs.

**HRRR Grid (Open-Meteo):**
```
GET https://api.open-meteo.com/v1/forecast
    ?latitude={lat}&longitude={lon}
    &current=wind_speed_10m,wind_direction_10m,wind_gusts_10m
    &models=gfs_seamless
    &wind_speed_unit=kn
```
- 9×8 grid = 72 API calls (can batch with comma-separated coordinates)
- No auth needed
- 30-minute cache TTL

**NDBC Stations (real-time buoy observations):**
```
GET https://www.ndbc.noaa.gov/data/realtime2/{STATION}.txt
```
- 9 stations: FTPC1, SFXC1, RCMC1, AAMC1, OKXC1, PPXC1, 46026, TIBC1, 46012
- Plain text, parse header + data line
- Wind speed in m/s → convert to knots (× 1.94384)
- 10-minute cache TTL

**Wind direction convention:** Meteorological "from" direction. Convert to u/v components:
```swift
let dirRad = direction * .pi / 180
let u = -speedKn * sin(dirRad)
let v = -speedKn * cos(dirRad)
```

### Tidal Currents
Replaces `currents.py`. NOAA CO-OPS JSON API.

```
GET https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
    ?date=today&station={ID}&product=currents_predictions
    &units=english&time_zone=gmt&format=json&interval=6
```
- 6 stations: SFB1201 (Golden Gate), SFB1203 (Alcatraz N), SFB1204 (Alcatraz S), SFB1205 (Angel Island E), SFB1206 (Raccoon Strait), SFB1211 (Bay Bridge)
- Response: `{"current_predictions": {"cp": [{"Time": "...", "Velocity_Major": 0.45, "meanFloodDir": 45, "meanEbbDir": 225}]}}`
- Linear interpolation between 6-hour prediction intervals
- Flood (positive velocity) → use meanFloodDir; Ebb (negative) → use meanEbbDir
- 6-hour cache TTL

### Tide Heights
Replaces `tides.py`. Same NOAA CO-OPS API.

```
GET https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
    ?begin_date={YYYYMMDD}&end_date={YYYYMMDD}
    &station={ID}&product=predictions&datum=MLLW
    &units=english&time_zone=gmt&format=json&interval=6
```
- 14 stations across SF Bay
- Response: `{"predictions": [{"t": "2024-01-15 12:00", "v": "3.45"}]}`
- Linear interpolation between prediction points
- Extrema detection (next high/low tide): compare consecutive values
- 6-hour cache TTL

### Forecast Time Parameter

All environmental APIs accept a `time` parameter (minutes offset from now). The frontend forecast timeline sends `time=0` through `time=2880` (48 hours).

For NOAA APIs: add the offset to the current time when making the request, then interpolate predictions at the target time.

For wind: use Open-Meteo's `forecast_hours` parameter to get the right future hour.

For SFBOFS: the forecast hour maps to a different S3 file (`n000.nc` = nowcast, `n006.nc` = 6h ahead, etc.).

---

## Module 4: AISSocket (Local AIS Hardware)

### What It Replaces
`ais_listener.py` — TCP/UDP connection to local AIS receiver on boat WiFi.

### Why This Must Be Native
Browsers cannot open raw TCP sockets. This is the #1 reason the app needs to be native. The current web app requires the Python backend to connect to the AIS hardware and relay data via WebSocket.

### Implementation

```swift
import Network

class AISSocket {
    let host = NWEndpoint.Host("192.168.47.10")
    let port = NWEndpoint.Port(integerLiteral: 10110)
    var connection: NWConnection?
    var onNMEA: ((String) -> Void)?
    
    func start() {
        // Try TCP first (like Python auto-detect)
        let tcp = NWConnection(host: host, port: port, using: .tcp)
        tcp.stateUpdateHandler = { state in
            switch state {
            case .ready:
                self.connection = tcp
                self.startReading()
            case .failed:
                self.tryUDP()  // Fallback to UDP
            default: break
            }
        }
        tcp.start(queue: .global())
    }
    
    func tryUDP() {
        let udp = NWConnection(host: host, port: port, using: .udp)
        // ... similar setup
    }
    
    func startReading() {
        connection?.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, _, _ in
            if let data = data, let text = String(data: data, encoding: .ascii) {
                // NMEA sentences are line-delimited
                text.split(separator: "\n").forEach { line in
                    self.onNMEA?(String(line))
                }
            }
            self.startReading()  // Continue reading
        }
    }
}
```

### iOS Local Network Permission
- Requires `NSLocalNetworkUsageDescription` in Info.plist
- User prompted once: "AIS Tracker wants to find and connect to devices on your local network"
- After approval, TCP to any LAN IP works

### AISstream.io Fallback
When not on boat WiFi, connect to AISstream.io cloud API via WebSocket (same as current `aisstream_listener.py`):
```
wss://stream.aisstream.io/v0/stream
```
- Requires API key (stored in app settings / Keychain)
- Sends subscription with SF Bay bounding box
- Receives JSON-encoded AIS messages
- 50-vessel cap for clean display

### Auto-Detection Logic
Same as Python `main.py`:
1. Try TCP to `192.168.47.10:10110` with 5-second timeout
2. If fails, try UDP
3. If fails, try AISstream.io with API key
4. If no API key, enter demo mode

---

## Module 5: AISDecoder (NMEA → Structured Data)

### What It Replaces
`ais_decoder.py` + `pyais` library.

### AIS Message Parsing

AIS messages are 6-bit ASCII-armored binary payloads inside NMEA sentences:
```
!AIVDM,1,1,,A,15N4cJ`005Jrek0H@9n`DW5608EP,0*13
       │ │ │ │ └─ 6-bit payload
       │ │ │ └─ channel
       │ │ └─ sequence ID
       │ └─ fragment number
       └─ total fragments
```

**Message types to support:**

| Type | Content | Fields |
|------|---------|--------|
| 1, 2, 3 | Class A position | mmsi, lat, lon, sog, cog, heading |
| 5 | Class A static | mmsi, name, destination, ship_type, length, beam |
| 18 | Class B position | mmsi, lat, lon, sog, cog |
| 19 | Extended Class B | mmsi, lat, lon, sog, cog, heading, name, ship_type |
| 24 | Class B static | mmsi, name, ship_type, length, beam |

**Multi-part message buffering:**
Type 5 messages are always 2 fragments. Buffer by (sentence_type, sequence_id), combine payloads, then decode.

### Swift Implementation Options

1. **Port pyais logic directly** — ~500 lines of bit manipulation. The core is extracting fields at specific bit positions from the binary payload. Straightforward but tedious.

2. **Use libais (C++)** — Mature C++ AIS decoder. Link via bridging header. Well-tested against edge cases.

3. **Use an existing Swift AIS package** — Several exist on GitHub (search "AIS NMEA Swift").

**Recommendation:** Option 1 (direct port). The bit extraction is simple and you avoid C++ bridging complexity. The ship_type categorization map is just a dictionary lookup.

### Ship Type Categories
```swift
let shipTypeMap: [ClosedRange<Int>: String] = [
    20...29: "Wing in Ground",
    30...35: "Fishing/Towing/Dredging",
    36...39: "Sailing/Pleasure",
    40...49: "High Speed Craft",
    50...59: "Special Craft",
    60...69: "Passenger",
    70...79: "Cargo",
    80...89: "Tanker",
    90...99: "Other",
]
```

---

## Module 6: Local Database (SQLite)

### What It Replaces
`database.py` — SQLite with WAL mode and async locking.

### Schema (Identical to Python)

```sql
CREATE TABLE vessels (
    mmsi INTEGER PRIMARY KEY,
    name TEXT,
    ship_type INTEGER,
    ship_category TEXT,
    destination TEXT,
    length INTEGER,
    beam INTEGER,
    is_own_vessel INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    sog REAL,
    cog REAL,
    heading INTEGER,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
);

CREATE INDEX idx_positions_mmsi ON positions(mmsi);
CREATE INDEX idx_positions_timestamp ON positions(timestamp);
CREATE INDEX idx_positions_mmsi_ts ON positions(mmsi, timestamp);

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

### Swift Implementation
Use **GRDB.swift** (best Swift SQLite library) or Apple's raw `sqlite3` C API. GRDB provides:
- WAL mode support
- Async/await database access
- Type-safe record mapping
- Migrations

### Functions to Port

| Python Function | Purpose | Complexity |
|----------------|---------|------------|
| `init_db()` | Create tables, indexes, WAL mode | Simple |
| `upsert_vessel()` | Insert/update vessel metadata | Simple |
| `insert_position()` | Append position record | Simple |
| `get_all_vessels()` | All vessels + latest position (JOIN) | Medium |
| `get_vessel_detail()` | Single vessel lookup | Simple |
| `get_vessel_track()` | Position history (default 2h) | Simple |
| `get_avg_speed()` | AVG(sog) over time window | Simple |
| `get_stats()` | Count vessels/positions | Simple |

---

## Background Data Refresh

### What It Replaces
`main.py` `refresh_environmental_data()` — the background loop that fetches all 48h of environmental data every 30 minutes.

### iOS Implementation

**BGAppRefreshTask:**
```swift
// Register in AppDelegate
BGTaskScheduler.shared.register(forTaskWithIdentifier: "com.aistracker.refresh",
                                 using: nil) { task in
    self.handleRefresh(task: task as! BGAppRefreshTask)
}

func handleRefresh(task: BGAppRefreshTask) {
    // Fetch all environmental data
    Task {
        await dataFetcher.refreshAll()  // Wind, tides, currents
        await sfbofsLoader.refreshAll() // SFBOFS 0-48h
        task.setTaskCompleted(success: true)
    }
    // Schedule next refresh
    scheduleRefresh()
}
```

**Refresh cycle (mirrors Python):**
1. Wind: current + 48 forecast hours (Open-Meteo)
2. Tides: 14 stations, today + tomorrow
3. Currents: 6 stations, today + tomorrow
4. SFBOFS: 49 files (hour 0-48), 800 KB each = ~38 MB total per cycle

**WiFi-only mode for SFBOFS:** Use `NWPathMonitor` to check connection type. Only fetch SFBOFS on WiFi (38 MB per cycle). Tides/currents/wind are small enough for cellular.

### Disk Caching

All fetched data saved to app's Documents directory, same JSON format as Python's `static/data/` files:
```
Documents/
  data/
    wind_forecasts.json
    sfbofs_forecasts.json
    tides/{station_id}.json    (14 files)
    currents/{station_id}.json (6 files)
```

On app launch: load disk cache immediately → display data → refresh in background.

---

## Xcode Project Structure

```
AISTracker/
├── AISTracker.xcodeproj
├── AISTracker/
│   ├── App/
│   │   ├── AISTrackerApp.swift          # Entry point, BGTask registration
│   │   ├── MainViewController.swift     # WKWebView setup + NativeBridge
│   │   └── Info.plist                   # Local network permission, BGTask IDs
│   ├── Bridge/
│   │   ├── NativeBridge.swift           # WKScriptMessageHandler routing
│   │   └── BridgeProtocol.swift         # Message types enum
│   ├── Data/
│   │   ├── DataFetcher.swift            # Wind, tides, currents (NOAA APIs)
│   │   ├── SFBOFSLoader.swift           # Range requests + regridding
│   │   ├── Regridder.swift              # Delaunay triangulation + interpolation
│   │   └── CacheManager.swift           # Disk + memory caching
│   ├── AIS/
│   │   ├── AISSocket.swift              # TCP/UDP to local hardware
│   │   ├── AISStreamClient.swift        # WebSocket to aisstream.io
│   │   ├── AISDecoder.swift             # NMEA → structured messages
│   │   └── ShipTypes.swift              # Type categorization map
│   ├── Database/
│   │   ├── Database.swift               # GRDB setup, WAL mode
│   │   ├── Vessel.swift                 # Vessel model + queries
│   │   └── Position.swift               # Position model + queries
│   ├── Background/
│   │   └── RefreshScheduler.swift       # BGAppRefreshTask coordination
│   └── Resources/
│       ├── sfbofs_mesh_latc.bin         # Bundled latc (399 KB)
│       ├── sfbofs_mesh_lonc.bin         # Bundled lonc (399 KB)
│       ├── sfbofs_weights.bin           # Precomputed interpolation weights (2.5 MB)
│       └── Web/                         # Copied from static/
│           ├── index.html
│           ├── js/
│           │   ├── app.js               # Modified: fetch() → native bridge
│           │   ├── tidal-flow.js        # Unchanged
│           │   └── wind-overlay.js      # Unchanged
│           ├── css/
│           │   └── style.css            # Unchanged
│           └── lib/
│               ├── leaflet.js           # Bundled
│               └── leaflet.css          # Bundled
├── Packages/
│   └── (Swift Package Manager dependencies)
│       ├── GRDB.swift                   # SQLite
│       └── SwiftHDF5 (optional)         # Tier 2 fallback only
└── Tests/
    ├── SFBOFSLoaderTests.swift          # Range request + regrid validation
    ├── AISDecoderTests.swift            # NMEA parsing test vectors
    └── RegridderTests.swift             # Interpolation accuracy
```

---

## Implementation Order

### Phase 1: Shell + Bridge (Week 1)
1. Create Xcode project with WKWebView loading bundled index.html
2. Implement NativeBridge message handler
3. Modify app.js fetch calls → bridge messages
4. Verify map loads, panels render, animations play

### Phase 2: Environmental Data (Week 1-2)
1. DataFetcher: wind field (Open-Meteo + NDBC)
2. DataFetcher: tidal currents (NOAA CO-OPS)
3. DataFetcher: tide heights (NOAA CO-OPS)
4. Wire all three through bridge → verify legends, timeline, overlays work

### Phase 3: SFBOFS Current Field (Week 2-3)
1. SFBOFSLoader: Range request fetching with validation
2. Regridder: Delaunay triangulation + barycentric interpolation
3. Precompute and bundle mesh coordinates + interpolation weights
4. Tier 2 fallback: full file download + HDF5 parsing
5. Wire through bridge → verify tidal flow particle animation

### Phase 4: AIS Data (Week 3-4)
1. AISDecoder: NMEA parsing, all 6 message types, multi-part buffering
2. AISSocket: TCP/UDP connection to local hardware
3. AISStreamClient: WebSocket to aisstream.io cloud
4. Auto-detection: local → cloud → demo
5. Database: SQLite schema, vessel upsert, position insert
6. Wire through bridge → verify vessel markers, popups, tracks

### Phase 5: Polish (Week 4-5)
1. Background App Refresh for environmental data
2. Disk caching + offline startup
3. WiFi-only toggle for SFBOFS downloads
4. Data freshness indicators through bridge
5. Settings screen: OWN_MMSI, AIS API key, data source selection
6. App icon, launch screen
7. TestFlight / App Store submission

---

## Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| NOAA changes SFBOFS file layout | Range requests break | Tier 2 fallback (full download + libhdf5). File size check as early warning. Push app update with new offsets. |
| NOAA discontinues SFBOFS S3 bucket | No current field data | OPeNDAP on NCEI THREDDS as backup source (proven working, but archival lag). Monitor NOAA announcements. |
| Canvas performance in WKWebView | Particle animations stutter | Modern iPhones handle this fine. Reduce particle count on older devices if needed (check `UIDevice` model). |
| AIS hardware connection on boat WiFi | iOS blocks local network | `NSLocalNetworkUsageDescription` permission. Test with actual hardware early in Phase 4. |
| pyais edge cases in AIS decoder | Malformed messages crash | Port pyais test vectors. Add try/catch around every message decode. Log and skip bad messages. |
| 57MB fallback on cellular | User data bill | Default to WiFi-only for SFBOFS. Show clear warning before cellular download. Cache aggressively. |

---

## Data Download Budget (Per 30-Minute Refresh Cycle)

| Data Source | Size | Frequency | Cellular OK? |
|-------------|------|-----------|-------------|
| Wind grid (49 hours) | ~150 KB | 30 min | Yes |
| NDBC stations (9) | ~50 KB | 10 min | Yes |
| Tide heights (14 stations) | ~100 KB | 6 hours | Yes |
| Tidal currents (6 stations) | ~50 KB | 6 hours | Yes |
| SFBOFS (49 hours × 800 KB) | ~38 MB | 6 hours | WiFi preferred |
| **Total per cycle** | **~38.4 MB** | | |
| **Total without SFBOFS** | **~350 KB** | | |

On WiFi: everything refreshes freely.
On cellular: everything except SFBOFS refreshes on schedule. SFBOFS uses cached data until WiFi available, or user explicitly opts in to cellular SFBOFS downloads.

---

## Development Setup & Workflow

### How to Build This with Claude Code

**Claude Code writes Swift files. Xcode builds and runs them. Work in phases — never more than one module ahead of testing.**

#### Initial Setup
1. Open Xcode → File → New → Project → iOS → App
2. Interface: **SwiftUI**, Language: **Swift**, name: **AISTracker**
3. Save it alongside (not inside) the existing AIS Tracker web project
4. Tell Claude Code the path Xcode created (e.g., `/Users/peterrostas/.../AISTracker/AISTracker/`)
5. Claude Code will write `.swift` files into that directory structure
6. In Xcode, files in the project folder auto-appear in the navigator (Xcode 15+). If not, drag them in.

#### Per-Phase Workflow
1. Claude Code writes the module files
2. You build in Xcode (Cmd+R) — paste any compiler errors back to Claude
3. Run in Simulator or on device — report what works, what doesn't
4. Claude Code fixes issues based on your feedback
5. Move to next phase only when current phase works

#### Why Not a Claude Plugin in Xcode?
- Xcode has no meaningful extension/plugin API for AI assistants
- Claude Code in terminal + Xcode side by side is the proven workflow
- You need Xcode regardless for simulator, device deployment, signing, storyboards, asset catalogs

#### What Claude Code Can and Cannot Do

**Can do well:**
- Write all Swift files with correct syntax and structure
- Port Python logic to Swift (interpolation, API calls, caching, database)
- Modify the existing JS files for the native bridge
- Fix compiler errors from Xcode output
- Generate test files and test data

**Cannot do (you must verify):**
- Run Xcode or the iOS Simulator
- Test WKWebView + Leaflet interaction (touch events, viewport, Canvas performance)
- Test real TCP connections to AIS hardware on boat WiFi
- Verify SFBOFS byte parsing produces correct float arrays on iOS (endianness should match — both little-endian — but must be confirmed)
- Profile memory usage of 3000 Canvas particles in WKWebView on actual devices
- Test Background App Refresh timing (iOS is unpredictable about when it grants background time)

### Estimated New Code

| Module | Swift Lines | JS Changes | Risk Level |
|--------|------------|------------|------------|
| App scaffolding (AppDelegate, ViewController) | ~200 | — | Low |
| NativeBridge (JS ↔ Swift message routing) | ~150 | ~100 | **High** (WKWebView quirks) |
| DataFetcher (wind, tides, currents) | ~400 | — | Low |
| SFBOFSLoader (Range requests + validation) | ~300 | — | **High** (byte-level parsing) |
| Regridder (Delaunay + interpolation) | ~250 | — | **High** (math correctness) |
| AISSocket (TCP/UDP to boat hardware) | ~200 | — | **High** (hardware-dependent) |
| AISStreamClient (WebSocket to aisstream.io) | ~150 | — | Medium |
| AISDecoder (NMEA 6-bit → structured data) | ~400 | — | **High** (bit manipulation) |
| Database (GRDB + schema + queries) | ~300 | — | Low |
| CacheManager (disk + memory, TTL) | ~200 | — | Low |
| BackgroundRefresh (BGTask + WiFi check) | ~150 | — | Medium |
| **Total** | **~2,700** | **~100** | |

---

## Notes for Future Claude Code Sessions

### Critical Context (Read This First)

This iPhone app is a **WKWebView hybrid** — NOT a SwiftUI rewrite. The existing HTML/JS/CSS from `static/` runs inside WKWebView. Swift only handles what the browser can't: data fetching, TCP sockets, SQLite, background refresh.

**Do NOT:**
- Rewrite the UI in SwiftUI — the whole point is reusing the existing web frontend
- Use MapKit — the app uses Leaflet inside WKWebView
- Skip the SFBOFS Range request approach and fall back to full 57MB downloads — the 800 KB approach is proven and should be the primary path
- Implement station IDW as a fallback for SFBOFS — the user requires the full 200m resolution grid, always
- Make the app depend on any backend server — everything must run on the phone

### SFBOFS Implementation — Key Details

This is the most complex module. Future sessions should understand:

**The problem:** SFBOFS current field data is published as 57MB NetCDF (HDF5) files on S3. The phone only needs 4 float arrays (1.6 MB). We use HTTP Range requests to extract the exact bytes.

**Verified byte offsets (stable across all tested files, April 2026):**
```
File size: 56,605,561 bytes (constant)
latc:      offset 2,597,959   len 409,056  (102,264 × float32)
lonc:      offset 2,188,903   len 409,056  (102,264 × float32)
u chunk 1: offset 26,940,241  len 204,528  (51,132 × float32)
u chunk 2: offset 28,985,521  len 204,528  (51,132 × float32)
v chunk 1: offset 35,124,497  len 204,528  (51,132 × float32)
v chunk 2: offset 37,169,777  len 204,528  (51,132 × float32)
```

**Why two chunks for u and v:** HDF5 chunk size is `[1, 10, 51132]`. The surface layer (102,264 cells) is split across two chunks with deeper sigma layers in between.

**Validation:** First 5 latc values must be `[37.993706, 37.99439, 37.99197, 37.991085, 37.98969]`. If not, offsets have changed → trigger Tier 2 (full 57MB download + libhdf5 parse).

**Mesh optimization:** latc/lonc never change (fixed FVCOM geometry). Bundle them in the app. Only u/v chunks are fetched per update (4 requests, 800 KB).

**Longitude conversion:** SFBOFS stores lon as 0-360 (e.g., 236.97). Convert: `lon > 180 ? lon - 360 : lon`. Bundled lonc should be pre-converted to -180/180.

**S3 URL pattern:**
```
https://noaa-nos-ofs-pds.s3.amazonaws.com/sfbofs/netcdf/{YYYY}/{MM}/{DD}/sfbofs.t{HH}z.{YYYYMMDD}.fields.n{FFF}.nc
HH = 03|09|15|21 (four model runs/day)
FFF = 000-048 (forecast hours)
Runs take ~3h to publish. Try most recent first, fall back to earlier runs / yesterday.
```

**Regridding can be precomputed:** Since the mesh and output grid (276×325, 0.002° spacing) are fixed, the Delaunay triangulation and barycentric weights are the same every time. Precompute once → save as `sfbofs_weights.bin` (~2.5 MB) → bundle in app → each update becomes a trivial weighted sum (~5ms with Accelerate framework).

### JS Bridge — What to Modify in app.js

The existing `app.js` makes these `fetch()` calls that must be replaced with native bridge messages:

| Line ~519 | `fetch('/api/vessels/{mmsi}/track?hours=2')` |
| Line ~859 | `fetch('/api/vessels')` |
| Line ~957 | `fetch('/api/currents?time=N')` |
| Line ~1037 | `fetch('/api/current-field?time=N')` |
| Line ~1198 | `fetch('/api/wind-field?time=N')` |
| Line ~1252 | `fetch('/api/tide-height?time=N')` |
| Line ~716 | `new WebSocket('/ws')` — real-time vessel updates |

**Recommended bridge pattern:** Create a `nativeFetch()` wrapper that mimics the fetch() Response interface so existing `.then(r => r.json())` chains still work. This minimizes JS changes:
```javascript
function nativeFetch(url) {
    return new Promise((resolve) => {
        const id = Date.now() + Math.random();
        window._pendingCallbacks[id] = resolve;
        window.webkit.messageHandlers.native.postMessage({url: url, callbackId: id});
    });
}
// Swift calls back: evaluateJavaScript("window._pendingCallbacks[id]({json: ...})")
```
This way most existing fetch() calls just change `fetch(url)` → `nativeFetch(url)` with minimal refactoring.

### AIS Decoder — Test Vectors

When porting the NMEA decoder, use these real message formats for testing:
```
Type 1/2/3 (Class A position): !AIVDM,1,1,,A,15N4cJ`005Jrek0H@9n`DW5608EP,0*13
Type 5 (Static, 2-part):       !AIVDM,2,1,3,B,55?MbV02>...payload...,0*1B
                                 !AIVDM,2,2,3,B,...continued...,2*23
Type 18 (Class B position):    !AIVDM,1,1,,B,B5N4cJ000>...payload...,0*6A
Type 24 (Class B static):      !AIVDM,1,1,,A,H5N4cJ@T4...payload...,0*5C
```
The pyais library source has extensive test fixtures — grab those for the test suite.

### Local AIS Hardware

- **IP:** 192.168.47.10, **Port:** 10110
- **Protocol:** Try TCP first (persistent connection, line-delimited NMEA), fall back to UDP
- **iOS requirement:** `NSLocalNetworkUsageDescription` in Info.plist, Bonjour not required for direct IP
- **On boat WiFi only** — detect by attempting connection; if it fails, switch to AISstream.io or demo mode
- The user's OWN_MMSI is 338361814 — highlight this vessel differently (gold/orange markers)

### Dependencies (Swift Package Manager)

| Package | Purpose | Required? |
|---------|---------|-----------|
| GRDB.swift | SQLite with WAL mode, async, type-safe | Yes |
| SwiftHDF5 or similar | Tier 2 SFBOFS fallback (full file parse) | Yes (fallback) |
| No Leaflet/map packages | Map runs in WKWebView | N/A |

### OPeNDAP — Potential Future Improvement

During research we confirmed that NOAA's NCEI THREDDS server supports OPeNDAP for SFBOFS, returning raw variable arrays as ASCII over HTTP (no NetCDF library). Example that worked:
```
https://www.ncei.noaa.gov/thredds/dodsC/model-sfbofs-files/2023/11/nos.sfbofs.fields.n000.20231114.t21z.nc.ascii?latc[0:1:N],lonc[0:1:N],u[0:1:0][0:1:0][0:1:N],v[0:1:0][0:1:0][0:1:N]
```
However, NCEI only had data through 2023 (archival), and the real-time CO-OPS THREDDS server (`opendap.co-ops.nos.noaa.gov`) was chronically timing out. If NOAA fixes the real-time server or NCEI catches up, this could replace the Range request approach entirely — no byte offsets, no HDF5, just HTTP GET returning text arrays. Worth checking periodically.

### App Configuration

- **Distribution:** Personal sideload via Xcode initially, potentially App Store later. Design with App Store in mind (no hardcoded secrets, proper Info.plist, icon assets) but don't worry about review process yet.
- **Target:** iOS 16+ (iPhone 12 / A14 chip and newer). Good Canvas performance guaranteed at this tier.
- **Settings:** Use iOS **Settings.bundle** (appears in system Settings app). Fields:
  - `OWN_MMSI` (string, default: `338361814`)
  - `AISSTREAM_API_KEY` (string, default: empty)
  - `AIS_HOST` (string, default: `192.168.47.10`)
  - `AIS_PORT` (string, default: `10110`)
  - `SFBOFS_CELLULAR` (toggle, default: off) — allow SFBOFS downloads on cellular
- **UI state:** Start from current web code as-is. Unfinished UI changes will be made in the web code later and carry over automatically since the iOS app loads the same HTML/JS/CSS.

### External Libraries in the Web Frontend

Confirmed: the only external JS library is **Leaflet 1.9.4** (bundled in `static/lib/`). No Chart.js or other dependencies. The speed chart in vessel popups is hand-rolled SVG in `app.js:buildSpeedChart()`. All tile layer URLs (CartoDB, OSM, NOAA charts, OpenSeaMap) are loaded by Leaflet at runtime.

### Python Backend Improvement (Bonus)

The same OPeNDAP/Range request approach could simplify the existing Python backend too:
- Drop `netCDF4` and `scipy` dependencies
- Download 1.6 MB instead of 57 MB per fetch
- Smaller Docker image for Fly.io
- This is independent of the iOS work and can be done anytime
