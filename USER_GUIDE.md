# AIS Tracker - Sailor's Guide

A complete guide to using AIS Tracker on your boat. Whether you're anchored in a busy harbor or crossing the Bay, this guide will help you get the most out of every feature.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [The Map Interface](#the-map-interface)
3. [Your Vessel Icon](#your-vessel-icon)
4. [Vessel Tracking & Popups](#vessel-tracking--popups)
5. [Collision Avoidance (CPA/TCPA)](#collision-avoidance-cpatcpa)
6. [Tidal Current Overlay](#tidal-current-overlay)
7. [Wind Overlay](#wind-overlay)
8. [Tide Height Stations](#tide-height-stations)
9. [Forecast & Time Travel](#forecast--time-travel)
10. [Offline Mode](#offline-mode)
11. [Installing on Your Phone](#installing-on-your-phone)
12. [Configuration](#configuration)
13. [Tips & Tricks](#tips--tricks)

---

## Getting Started

### What You Need

- A computer, Raspberry Pi, or phone/tablet
- Python 3.11+ installed
- One of these AIS data sources:
  - **WiFi AIS receiver** on your boat network (best — real-time, no internet needed)
  - **AISstream.io account** (free — works anywhere with internet)
  - **Demo mode** (no hardware, simulated vessels for testing)

### First Run

```bash
pip install -r requirements.txt
python main.py
```

Open **http://localhost:8888** in your browser. That's it.

The app auto-detects your AIS source:
1. Tries your local AIS receiver first (TCP, then UDP)
2. Falls back to AISstream.io if you have an API key in `.env`
3. Falls back to demo mode if nothing else is available

### Connecting Your AIS Receiver

Most WiFi AIS receivers broadcast NMEA data over TCP or UDP. Set these in your `.env` file:

```
AIS_HOST=192.168.47.10
AIS_PORT=10110
```

The app tries TCP first. If that fails within 5 seconds, it switches to UDP automatically.

### Setting Your Own Vessel

Set your MMSI so the app knows which vessel is yours:

```
OWN_MMSI=338361814
```

Your vessel gets a special icon with directional arrows, and the side panel marks it as "(You)".

---

## The Map Interface

### Map Layers

Use the layer control (top-left corner) to switch between:

| Layer | Best For |
|-------|----------|
| **Dark (CartoDB)** | Night sailing, low glare in the cockpit |
| **Street (OSM)** | Finding landmarks and shore references |
| **NOAA Nautical Chart** | Navigation with depth soundings, channels, buoys |
| **OpenSeaMap Marks** | Buoys, lights, and aids to navigation overlay |

All layers work offline if you've cached the tiles.

### Status Bar (Bottom)

The bar at the bottom shows:

- **Connection status** — Green dot = connected, red = disconnected
- **Vessel count** — How many vessels are being tracked
- **Message counter** — AIS messages processed
- **Flow: ON/OFF** — Toggle tidal current animation
- **Wind: OFF/ON** — Toggle wind animation
- **Tide: OFF/ON** — Toggle tide height station markers

### Side Panel (Right)

Click the **<** arrow to expand the vessel list panel:

- **Search box** — Find vessels by name or MMSI
- **Vessel cards** — Each shows name, type, distance, bearing, speed, and CPA status
- **Eye icon** — Hide/show individual vessels on the map
- Vessels fade out after 10 minutes without an AIS update

### Map Click

Click anywhere on the water to see:
- Lat/lon coordinates
- Tidal current speed and direction at that point
- Wind speed and direction
- Distance and bearing from your boat
- ETA based on your current speed

---

## Your Vessel Icon

Your boat's icon is larger than others and shows four directional arrows:

| Arrow | Color | Meaning |
|-------|-------|---------|
| **Dashed white** | White | **Heading** — where your bow is pointing |
| **Solid green** | Green | **COG** — your actual course over ground |
| **Solid cyan** | Cyan | **Current** — tidal current push (length = strength) |
| **Dashed purple** | Purple | **Wind** — wind direction and force (length = speed) |

This lets you see at a glance why your COG differs from your heading — you can see the current pushing you sideways or the wind affecting your course.

---

## Vessel Tracking & Popups

Click any vessel on the map to see its popup with:

### Navigation Data
- **SOG** — Speed over ground in knots
- **COG** — Course over ground in degrees
- **Current** — Tidal current at the vessel's location (speed and direction)
- **Wind** — Wind at the vessel's location (speed, direction, gusts)
- **Avg Speed** — Average speed over the last 2 hours

### Relative Information (other vessels)
- **Distance** — Nautical miles from your boat
- **Bearing** — Compass bearing from your boat
- **ETA** — How long until you'd reach them at your current speed
- **Speed Diff** — Whether they're faster or slower than you
- **CPA/TCPA** — Closest point of approach (see next section)

### Speed History Chart
Every popup includes a 2-hour speed history chart:
- Blue sections = decelerating
- Green sections = steady speed
- Red sections = accelerating

### Track Trails
Each vessel leaves a colored trail on the map showing its recent path. Your own trail is a solid line; other vessels show dashed trails.

---

## Collision Avoidance (CPA/TCPA)

AIS Tracker continuously calculates collision risk for every vessel relative to yours.

### What the Numbers Mean

- **CPA** (Closest Point of Approach) — The minimum distance (in nautical miles) that the other vessel will pass from you, based on both vessels' current speed and course
- **TCPA** (Time to CPA) — How many minutes until that closest point

### Alert Levels

| Level | Condition | What It Means |
|-------|-----------|---------------|
| **COLLISION RISK** (red) | CPA < 0.1 nm AND TCPA < 30 min | Vessels will pass within ~600 feet. Take action. |
| **Close Approach** (yellow) | CPA < 0.5 nm AND TCPA < 60 min | Vessels will pass within ~3000 feet. Keep watch. |
| **Diverging** | TCPA negative or vessels separating | Vessels are moving apart. No concern. |
| **Parallel** | Similar course and speed | Vessels traveling in roughly the same direction. |

### Where to See It
- **Vessel popup** — Full CPA/TCPA data with distance and time
- **Side panel cards** — Color-coded CPA status at a glance
- Alert colors match in both locations

### Important Note
CPA/TCPA assumes both vessels maintain their current speed and course. If either vessel turns or changes speed, the calculation updates instantly with the new AIS data.

---

## Tidal Current Overlay

Toggle with **Flow: ON/OFF** in the status bar.

### What You See
Thousands of animated particles flow across the map showing tidal current direction and speed. The color indicates strength:

| Color | Speed |
|-------|-------|
| Blue | Slack (near 0 kn) |
| Cyan | ~0.5 kn |
| Green | ~0.8 kn |
| Yellow | ~1.2 kn |
| Orange | ~1.8 kn |
| Red | 2+ kn (strong) |

A legend in the bottom-left shows the color scale and data source.

### Data Sources

The app uses the best available data:

1. **NOAA SFBOFS** (primary) — A full hydrodynamic model of SF Bay at ~200m resolution. Shows realistic eddies, channel acceleration, and flow patterns you won't get from station data alone.

2. **NOAA Tidal Current Stations** (fallback) — 6 measurement stations across the Bay (Golden Gate, Alcatraz, Angel Island, Raccoon Strait, Bay Bridge). Used when the SFBOFS model is unavailable.

### Current Station Arrows
Small colored arrows on the map show current speed/direction at NOAA's 6 tidal current stations:
- **Blue arrows** = flood (incoming tide)
- **Orange arrows** = ebb (outgoing tide)
- Arrow length proportional to current speed
- Click any arrow for exact speed and direction

---

## Wind Overlay

Toggle with **Wind: OFF/ON** in the status bar.

### What You See
Animated particles showing wind direction and speed across the Bay area. Two color schemes are available (toggle in the legend):

**Green scheme** (default): dark green (light) → lime → bright green → white (strong)
**Purple scheme**: dark purple (light) → magenta → light purple → white (strong)

### Data Sources

- **HRRR Model** (gridded) — NOAA's High-Resolution Rapid Refresh forecast, showing wind across the entire Bay area
- **NDBC Buoy Stations** (point data) — 9 coastal buoys and weather stations including Fort Point, SF Bar Pilots, Richmond, Alameda, Oakland, and offshore buoys

### Wind Station Markers
When wind is on, point markers show measured wind at each NDBC station. These are real observations, not model predictions.

### Limitations
Wind forecast data is available up to **48 hours** ahead. Beyond that, the wind overlay is automatically disabled (the model doesn't provide data further out).

---

## Tide Height Stations

Toggle with **Tide: OFF/ON** in the status bar.

### What You See
14 circular markers across SF Bay, each showing the current water level in feet above MLLW (Mean Lower Low Water).

- **Blue numbers** = water above MLLW (positive)
- **Red numbers** = water below MLLW (negative)

### Station Popups
Click any tide station to see:
- **Tide Height** — Current water level (e.g., +4.23 ft MLLW)
- **Next High/Low** — Time and height of the next tidal extreme
- **Datum** — MLLW reference

### Stations Covered
San Francisco (Golden Gate), Alameda, Oakland, Berkeley, Redwood City, San Mateo Bridge, Dumbarton Bridge, San Leandro Marina, Corte Madera Creek, Richmond, Half Moon Bay, Pinole Point, Martinez, Port Chicago.

### Unlimited Range
Tide predictions use harmonic math — they work for any date, months or years ahead. No model dependency.

---

## Forecast & Time Travel

### 48-Hour Timeline
The scrollable strip at the bottom of the screen shows 48 hours of forecast time.

1. **Tap an hour** — It highlights in orange
2. **Click GO** — Loads all data for that time (currents, wind, tides)
3. **Watch the progress** — A banner shows real percentage as each data source loads
4. **Click NOW** — Return to real-time

### Beyond 48 Hours
Click the **calendar button** to pick any date and time.

| Data Type | Forecast Range |
|-----------|---------------|
| Tide heights | Unlimited (math-based) |
| Tidal currents | ~48 hours (SFBOFS model) |
| Wind | ~48 hours (HRRR model) |

When you go beyond 48 hours, wind data is automatically hidden with a note that it's unavailable at that range. Tide predictions work at any range.

### What Changes in Forecast Mode
- All particle animations show predicted flow for the selected time
- Tide station markers update to predicted heights
- Current arrows show predicted speeds
- Vessel popup data (current/wind at position) reflects the forecast
- An orange banner shows the forecast date/time
- The status bar gets an orange border as a reminder

---

## Offline Mode

### Automatic Offline Detection
When you lose internet (common at sea), the app automatically:
- Switches to cached map tiles
- Uses cached environmental data
- Shows an "OFFLINE" banner with cache age
- Stops trying to refresh from APIs

When internet returns, everything resumes automatically.

### Pre-Downloading Data

Before leaving the dock:

1. Make sure you're connected to WiFi
2. Click the **download arrow** in the timeline strip
3. Watch the progress panel — it downloads 24 hours of:
   - Tide predictions for all 14 stations
   - Tidal current data (6 stations + SFBOFS grid)
   - Wind field data (HRRR model)
   - Wind station observations
4. Once complete, you have 24 hours of full environmental data available offline

### What Works Offline
- Full map with all cached tile layers
- All environmental overlays (if pre-downloaded)
- Vessel popups with cached data
- Forecast timeline (within cached range)
- All map interactions (pan, zoom, layer switching)

### What Doesn't Work Offline
- Live AIS vessel positions (unless you have a local AIS receiver)
- Fetching new environmental data beyond what's cached
- Map tiles for areas you haven't viewed before

---

## Installing on Your Phone

AIS Tracker is a PWA (Progressive Web App) — you can install it on your phone or tablet for a full-screen native app experience.

### iPhone / iPad
1. Open **http://your-server:8888** in Safari
2. Tap the **Share** button (square with arrow)
3. Scroll down and tap **Add to Home Screen**
4. Tap **Add**

The app icon appears on your home screen. Opening it launches a full-screen app with no browser chrome.

### Android
1. Open the URL in Chrome
2. Tap the **three dots** menu
3. Tap **Install app** or **Add to Home Screen**

### Raspberry Pi Setup
For the best on-boat experience, run AIS Tracker on a Raspberry Pi connected to your boat's WiFi network. Then install the PWA on your phone/tablet and access it at `http://<pi-ip>:8888`. Everything stays on the local network — no internet required for AIS tracking.

---

## Configuration

All settings can be configured via environment variables or a `.env` file in the project directory.

| Setting | Default | Environment Variable | Description |
|---------|---------|---------------------|-------------|
| AIS Receiver IP | 192.168.47.10 | `AIS_HOST` | Your WiFi AIS receiver's IP address |
| AIS Receiver Port | 10110 | `AIS_PORT` | NMEA data port |
| AIS Protocol | auto | `AIS_PROTOCOL` | `auto`, `tcp`, or `udp` |
| AISstream API Key | (none) | `AISSTREAM_API_KEY` | Free key from aisstream.io |
| Own MMSI | 338361814 | `OWN_MMSI` | Your vessel's MMSI number |
| Server Host | 127.0.0.1 | `SERVER_HOST` | Bind address (use 0.0.0.0 for network access) |
| Server Port | 8888 | `SERVER_PORT` | Web server port |
| Database Path | ./ais_tracker.db | `DB_PATH` | SQLite database location |

### Important: Network Access

If you want to access AIS Tracker from other devices (phone, tablet), set:

```
SERVER_HOST=0.0.0.0
```

This makes the server listen on all network interfaces instead of just localhost.

---

## Tips & Tricks

### Night Sailing
Use the **Dark (CartoDB)** map layer — it's designed for low light conditions and won't ruin your night vision.

### Planning Your Departure
Use the forecast timeline to check currents for the next few hours. A favorable current in the Gate can save you significant time and fuel.

### Monitoring an Anchorage
Leave AIS Tracker running while anchored. The speed history chart will show if your boat (or neighbors) start dragging — even small movements show up as speed spikes.

### Understanding Current Effects
Watch the four arrows on your vessel icon. If the cyan (current) arrow is long and perpendicular to your course, you're getting significant set. Adjust your heading to compensate.

### Busy Harbors
Use the **search box** in the side panel to quickly find a specific vessel. Use the **eye icon** to hide vessels you don't care about and declutter the map.

### Pre-Download Before Going Offshore
Always download offline data while you still have WiFi. The download button in the timeline strip caches 24 hours of environmental data — enough for a day sail.

### Click the Water
Click anywhere on the map to instantly see current, wind, distance, and bearing at that point. Useful for planning your route — check current strength at different points along your path.

### CPA Alerts While Cooking
Leave the app running on a tablet in the galley. The red/yellow CPA alerts in the side panel will catch your eye if a vessel gets too close while you're below deck.
