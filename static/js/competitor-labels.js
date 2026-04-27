/**
 * Competitor labels on map markers — distance, speed, bearing relative to Typon.
 */

const CompetitorLabels = (() => {
    const distHistory = new Map();
    const HISTORY_MAX_AGE = 5 * 60 * 1000;
    const TREND_WINDOW = 2 * 60 * 1000;

    function update(vessel, ownLat, ownLon, marker) {
        if (!ownLat || !ownLon || !vessel.lat || !vessel.lon) return;
        if (vessel.is_own_vessel) return;

        const dist = haversineNm(ownLat, ownLon, vessel.lat, vessel.lon);
        const bearing = relativeBearing(ownLat, ownLon, vessel.lat, vessel.lon);
        const now = Date.now();

        if (!distHistory.has(vessel.mmsi)) distHistory.set(vessel.mmsi, []);
        const hist = distHistory.get(vessel.mmsi);
        hist.push({ t: now, d: dist });

        const cutoff = now - HISTORY_MAX_AGE;
        while (hist.length > 0 && hist[0].t < cutoff) hist.shift();

        const distTrend = computeTrend(hist, now, TREND_WINDOW);
        const sogTrend = computeSogTrend(vessel);

        const name = vessel.name || vessel.shipname || `MMSI ${vessel.mmsi}`;
        const distStr = dist < 0.1 ? dist.toFixed(2) : dist.toFixed(1);
        const distArrow = distTrend < -0.01 ? '▲' : distTrend > 0.01 ? '▼' : '─';
        const distColor = distTrend < -0.01 ? '#2ecc71' : distTrend > 0.01 ? '#e74c3c' : '#8395a7';
        const sogStr = vessel.sog != null ? vessel.sog.toFixed(1) : '-.-';
        const sogArrow = sogTrend > 0.01 ? '▲' : sogTrend < -0.01 ? '▼' : '─';
        const bearStr = Math.round(bearing).toString().padStart(3, '0');

        const html = `<div class="competitor-label-content">
            <div class="comp-name">${escapeHtml(name)}</div>
            <div class="comp-stats">
                <span style="color:${distColor}">${distStr}nm ${distArrow}</span>
                <span>${sogStr}kn ${sogArrow}</span>
                <span>${bearStr}°</span>
            </div>
        </div>`;

        if (marker._competitorTooltip) {
            marker.setTooltipContent(html);
        } else {
            marker.bindTooltip(html, {
                permanent: true, direction: 'right', offset: [15, 0],
                className: 'competitor-tooltip', interactive: false,
            });
            marker._competitorTooltip = true;
        }
    }

    function computeTrend(hist, now, window) {
        const recent = hist.filter(p => p.t >= now - window);
        if (recent.length < 2) return 0;
        const first = recent[0];
        const last = recent[recent.length - 1];
        const dt = (last.t - first.t) / 60000;
        if (dt < 0.1) return 0;
        return (last.d - first.d) / dt;
    }

    function computeSogTrend(vessel) {
        if (!vessel._sogHistory) vessel._sogHistory = [];
        if (vessel.sog != null) {
            vessel._sogHistory.push({ t: Date.now(), v: vessel.sog });
            if (vessel._sogHistory.length > 30) vessel._sogHistory.shift();
        }
        const h = vessel._sogHistory;
        if (h.length < 3) return 0;
        const half = Math.floor(h.length / 2);
        const avgFirst = h.slice(0, half).reduce((s, p) => s + p.v, 0) / half;
        const avgSecond = h.slice(half).reduce((s, p) => s + p.v, 0) / (h.length - half);
        return avgSecond - avgFirst;
    }

    function haversineNm(lat1, lon1, lat2, lon2) {
        const R = 3440.065;
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const a = Math.sin(dLat / 2) ** 2 +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLon / 2) ** 2;
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    function relativeBearing(lat1, lon1, lat2, lon2) {
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const y = Math.sin(dLon) * Math.cos(lat2 * Math.PI / 180);
        const x = Math.cos(lat1 * Math.PI / 180) * Math.sin(lat2 * Math.PI / 180) -
                  Math.sin(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.cos(dLon);
        return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }

    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }

    function clearHistory() {
        distHistory.clear();
    }

    return { update, clearHistory };
})();
