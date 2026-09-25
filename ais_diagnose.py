#!/usr/bin/env python3
"""Offline AIS transponder diagnostic.

Run this on the boat network when the transponder receives but will not
transmit. It listens to the NMEA feed, saves a raw capture, and reports the
four things that actually explain a silent Class B:

  1. Is there a valid GPS fix?      (no fix -> no transmit, by design)
  2. Is the MMSI programmed?        (unset / 000000000 -> no transmit)
  3. Are there alarms on the bus?   ($AIALR, $AITXT)
  4. Is the unit emitting !AIVDO?   (own-ship report = it thinks it is TXing)

Stdlib only. No install, no network beyond the boat LAN.

    python3 ais_diagnose.py                       # 120s on the default feed
    python3 ais_diagnose.py --seconds 300         # longer (Class B TXes every 30s)
    python3 ais_diagnose.py --scan                # find the feed first
    python3 ais_diagnose.py --host 10.0.0.5 --port 2000
    python3 ais_diagnose.py --udp --port 10110    # some units broadcast UDP
    python3 ais_diagnose.py --replay capture.nmea # re-analyse a saved capture
"""

import argparse
import collections
import os
import socket
import sys
import time

DEFAULT_HOST = "192.168.47.10"
DEFAULT_PORT = 10110

# Candidate endpoints, tried in order by --scan. Covers the boat's receiver,
# the Pi's own bridge, and the ports common AIS vendors ship as defaults.
SCAN_HOSTS = [DEFAULT_HOST, "192.168.47.231", "TyponRpi4.local",
              "192.168.1.1", "192.168.4.1", "10.0.0.1"]
SCAN_PORTS = [10110, 10111, 2000, 39150, 4001, 8080]

# IEC 61993-2 / 62287-1 alarm identifiers reported by AIS transponders in
# $--ALR. Vendors deviate; anything not listed prints raw with its text field.
ALARM_IDS = {
    "001": "Tx malfunction",
    "002": "Antenna VSWR exceeds limit",
    "003": "Rx channel 1 malfunction",
    "004": "Rx channel 2 malfunction",
    "005": "Rx channel 70 (DSC) malfunction",
    "006": "General failure",
    "007": "UTC sync invalid",
    "008": "MKD / display connection lost",
    "009": "Internal GNSS position lost",
    "010": "No valid SOG information",
    "011": "No valid COG information",
    "012": "Heading lost / invalid",
    "013": "No valid ROT information",
    "014": "External EPFS (GPS) lost",
    "025": "External DGNSS in use",
    "026": "External DGNSS unavailable",
    "029": "No valid position information",
    "030": "No valid heading / SOG for reporting",
    "032": "Transmitter off / silent mode active",
}

# Alarms that directly explain "receives but does not transmit".
TX_BLOCKING = {"001", "002", "006", "009", "014", "029", "030", "032"}

# Routine navigation/AIS traffic. ANYTHING not in here is treated as possible
# diagnostic output and reproduced verbatim - that is the whole point of this
# tool. Vendors put their real error text in proprietary $P... sentences that
# no generic parser knows about, so we must not have an allowlist of errors,
# only an allowlist of boring traffic.
ROUTINE = {
    "VDM", "VDO",                                    # AIS
    "GGA", "RMC", "GLL", "GSA", "GSV", "VTG", "ZDA", "GNS",   # GNSS
    "HDG", "HDT", "HDM", "ROT", "RSA",               # heading
    "MWV", "MWD", "VWR", "VWT",                      # wind
    "VHW", "VLW", "DPT", "DBT", "DBS", "MTW",        # log / depth / temp
    "XDR", "RPM", "RSD", "TTM", "OSD",
}

# Human-readable distress words. Any sentence containing one gets pulled out
# and shown, whatever its type - this catches vendors who emit free text.
KEYWORDS = [
    "error", "fail", "fault", "malfunction", "alarm", "alert", "warn",
    "lost", "invalid", "no fix", "nofix", "unavail", "disabled", "disable",
    "silent", "vswr", "swr", "antenna", "mmsi", "not transmit", "no tx",
    "tx off", "txoff", "off line", "offline", "shutdown", "overtemp",
    "power", "voltage", "gps", "gnss", "position", "sync", "test",
]


# ---------------------------------------------------------------- NMEA basics

def checksum_ok(line):
    """True if the *NNN checksum matches. Bad checksums mean wiring/baud noise."""
    if "*" not in line:
        return None
    body, _, given = line[1:].partition("*")
    given = given.strip()[:2]
    if len(given) != 2:
        return None
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return f"{calc:02X}".upper() == given.upper()


def sixbit(payload):
    """AIS armoured payload -> list of bits."""
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        if v < 0 or v > 63:
            return bits
        for i in (5, 4, 3, 2, 1, 0):
            bits.append((v >> i) & 1)
    return bits


def ubits(bits, start, length):
    if start + length > len(bits):
        return None
    v = 0
    for b in bits[start:start + length]:
        v = (v << 1) | b
    return v


def sbits(bits, start, length):
    v = ubits(bits, start, length)
    if v is None:
        return None
    if v & (1 << (length - 1)):
        v -= 1 << length
    return v


def decode_ais(payload):
    """Minimal decode: type, MMSI, position, and the availability sentinels.

    The sentinels matter more than the values. A transponder that cannot fix
    its position says so explicitly in these fields, and the timestamp field
    in particular carries a plain statement of *why*.
    """
    bits = sixbit(payload)
    msg = {"type": ubits(bits, 0, 6), "mmsi": ubits(bits, 8, 30),
           "lat": None, "lon": None, "sog": None, "cog": None,
           "hdg": None, "ts": None}
    t = msg["type"]
    if t in (1, 2, 3):
        off = {"sog": 50, "lon": 61, "lat": 89, "cog": 116, "hdg": 128,
               "ts": 137}
    elif t in (18, 19):
        off = {"sog": 46, "lon": 57, "lat": 85, "cog": 112, "hdg": 124,
               "ts": 133}
    else:
        return msg

    lon_i, lat_i = sbits(bits, off["lon"], 28), sbits(bits, off["lat"], 27)
    if lon_i is not None and lat_i is not None:
        # 181 deg / 91 deg are the "not available" sentinels.
        if abs(lon_i) != 108600000:
            msg["lon"] = lon_i / 600000.0
        if abs(lat_i) != 54600000:
            msg["lat"] = lat_i / 600000.0
    sog = ubits(bits, off["sog"], 10)
    if sog is not None and sog != 1023:
        msg["sog"] = sog / 10.0
    cog = ubits(bits, off["cog"], 12)
    if cog is not None and cog != 3600:
        msg["cog"] = cog / 10.0
    hdg = ubits(bits, off["hdg"], 9)
    if hdg is not None and hdg != 511:
        msg["hdg"] = hdg
    msg["ts"] = ubits(bits, off["ts"], 6)
    return msg


# ITU-R M.1371 second-of-UTC field. Values 60-63 are not a time at all - they
# are the transponder stating the condition of its position source. 63 is the
# unit telling you outright that it cannot transmit a position.
TS_MEANING = {
    60: "time stamp not available",
    61: "positioning system in MANUAL input mode",
    62: "positioning system in ESTIMATED / dead-reckoning mode",
    63: "POSITIONING SYSTEM INOPERATIVE",
}


# ------------------------------------------------------------------- analysis

class Report:
    def __init__(self):
        self.total = 0
        self.bad_checksum = 0
        self.by_sentence = collections.Counter()
        self.by_talker = collections.Counter()
        self.alarms = []            # (raw, id, active, acked, text)
        self.txt = []               # raw $--TXT lines
        self.other = {}             # head -> {count, first, t, samples}
        self.keyword_hits = []      # (raw, [matched keywords])
        self.timeline = []          # (t, raw) for non-routine lines
        self.abk = []               # $--ABK transmit acknowledgements
        self.plain = []             # (t, raw) non-NMEA text: boot banners etc.
        self.last_vdo_state = None  # so a long run can announce transitions
        self.vdo_notices = []
        self.start_wall = time.time()
        self.vdo = []               # decoded own-ship messages
        self.vdm_mmsi = set()       # other vessels heard
        self.gps_fix = None         # True / False / None(never saw GGA|RMC)
        self.gps_samples = 0
        # Per-talker, because a boat has more than one GPS. Merging them hides
        # a dead receiver behind a healthy one - which is exactly the failure
        # this tool exists to find.
        self.gps = {}               # talker -> {n, valid, first_valid, sats}
        self.gsa_modes = collections.defaultdict(collections.Counter)
        self.gsv_snr = collections.defaultdict(list)
        self.last_position = None
        self.first_ts = None
        self.last_ts = None

    def _gps(self, talker, ok, now, sats=""):
        rec = self.gps.setdefault(talker, {"n": 0, "valid": 0,
                                           "first_valid": None, "sats": ""})
        rec["n"] += 1
        if ok:
            rec["valid"] += 1
            if rec["first_valid"] is None:
                rec["first_valid"] = now
        if sats:
            rec["sats"] = sats
        # Keep the legacy aggregate for the summary line only.
        self.gps_samples += 1
        self.gps_fix = ok if self.gps_fix is None else (self.gps_fix or ok)

    def feed(self, line):
        line = line.strip()
        if not line:
            return None
        now = time.time()
        if self.first_ts is None:
            self.first_ts = now
        self.last_ts = now

        if line[0] not in "$!":
            # NOT NMEA. Do not discard it - transponders emit plain-text boot
            # banners and self-test results, and that free text is often the
            # only place the unit says why it will not transmit.
            self.plain.append((now, line))
            if len(self.timeline) < 200:
                self.timeline.append((now, line))
            return line

        self.total += 1

        if checksum_ok(line) is False:
            self.bad_checksum += 1

        head = line.split(",")[0]
        tag = head[1:]

        # Proprietary sentences are $P + 3-char manufacturer mnemonic + a
        # variable-length type. Parsing them as talker+3 mangles exactly the
        # sentences most likely to carry the vendor's error text.
        proprietary = tag.startswith("P")
        if proprietary:
            talker = tag[:4] if len(tag) >= 4 else tag
            stype = tag[4:] or "(none)"
        elif len(tag) >= 5:
            talker, stype = tag[:2], tag[2:5]
        else:
            talker, stype = tag, ""
        self.by_talker[talker] += 1
        self.by_sentence[("P:" if proprietary else "") + (stype or "?")] += 1

        f = line.split("*")[0].split(",")
        notice = None

        # --- catch-all: anything that is not routine traffic is evidence ----
        interesting = proprietary or stype not in ROUTINE
        low = line.lower()
        hit = [k for k in KEYWORDS if k in low]
        # Keyword matching only counts outside routine traffic; "GPS" appears
        # inside AIS payloads by coincidence and would drown the signal.
        if hit and not interesting:
            hit = []
        if hit:
            self.keyword_hits.append((line, hit))

        if interesting:
            key = head
            if key not in self.other:
                self.other[key] = {"count": 0, "first": line,
                                   "t": now, "samples": []}
            rec = self.other[key]
            rec["count"] += 1
            if len(rec["samples"]) < 5 and line not in rec["samples"]:
                rec["samples"].append(line)
            if len(self.timeline) < 200:
                self.timeline.append((now, line))

        if stype == "ALR":
            # $--ALR,hhmmss.ss,xxx,A,A,c--c*hh
            aid = f[2].strip() if len(f) > 2 else ""
            active = (f[3].strip().upper() == "A") if len(f) > 3 else None
            acked = (f[4].strip().upper() == "A") if len(f) > 4 else None
            text = f[5].strip() if len(f) > 5 else ""
            self.alarms.append((line, aid, active, acked, text))

        elif stype == "TXT":
            self.txt.append(line)

        elif stype == "ABK":
            # Addressed/broadcast acknowledgement: field 4 is the result code.
            # 0 = message broadcast successfully. Anything else is a refusal
            # to transmit, straight from the transponder.
            self.abk.append(line)

        elif stype == "GGA":
            # field 6 = fix quality; 0 means no fix
            if len(f) > 6 and f[6].strip().isdigit():
                self._gps(talker, int(f[6]) > 0, now,
                          sats=f[7].strip() if len(f) > 7 else "")

        elif stype == "RMC":
            if len(f) > 2 and f[2].strip():
                self._gps(talker, f[2].strip().upper() == "A", now)

        elif stype == "GSA":
            # field 2: 1=no fix, 2=2D, 3=3D. Records quality, not just presence.
            if len(f) > 2 and f[2].strip().isdigit():
                self.gsa_modes[talker][int(f[2])] += 1

        elif stype == "GSV":
            # Satellites in view with SNR. All-zero SNR means the receiver knows
            # from almanac where the satellites are but hears nothing yet - the
            # signature of a cold start (or a dead antenna). Distinguishing the
            # two needs time, which is why this drives the cold-start warning.
            for i in (7, 11, 15, 19):
                if len(f) > i:
                    v = f[i].strip()
                    if v.isdigit():
                        self.gsv_snr[talker].append(int(v))

        elif stype in ("VDO", "VDM"):
            parts = f
            if len(parts) >= 6:
                frag_count, frag_num, payload = parts[1], parts[2], parts[5]
                if frag_num == "1" or frag_count == "1":
                    msg = decode_ais(payload)
                    if stype == "VDO":
                        self.vdo.append(msg)
                        if msg["lat"] is not None:
                            self.last_position = (msg["lat"], msg["lon"])
                        # Announce transitions. On a 10-minute watch the whole
                        # question is "does a position EVER appear", and VDO is
                        # routine traffic so it would otherwise scroll silently.
                        state = (msg["lat"] is not None, msg["ts"])
                        if state != self.last_vdo_state:
                            self.last_vdo_state = state
                            if msg["lat"] is not None:
                                notice = (f"*** OWN-SHIP POSITION ACQUIRED: "
                                          f"{msg['lat']:.5f},{msg['lon']:.5f} "
                                          f"ts={msg['ts']} ***")
                            else:
                                notice = ("AIVDO: no position, ts="
                                          f"{msg['ts']} "
                                          f"({TS_MEANING.get(msg['ts'], 'valid')})")
                            self.vdo_notices.append((now, notice))
                    elif msg["mmsi"]:
                        self.vdm_mmsi.add(msg["mmsi"])

        return notice or (line if interesting else None)


def render(rep, capture_path, listened):
    W = 72
    p = print
    p("")
    p("=" * W)
    p("  AIS TRANSPONDER DIAGNOSTIC")
    p("=" * W)

    dur = (rep.last_ts - rep.first_ts) if rep.first_ts else 0
    p(f"\nListened {listened:.0f}s, {rep.total} sentences"
      f"{f' over {dur:.0f}s of traffic' if dur else ''}")
    if capture_path:
        p(f"Raw capture: {capture_path}")

    if rep.total == 0:
        p("\n  NOTHING RECEIVED.")
        p("  The feed itself is down - this is not a transmit problem yet.")
        p("  Check: unit powered, ethernet/serial link up, right host:port.")
        p("  Try: python3 ais_diagnose.py --scan")
        p("=" * W)
        return 2

    p("\n-- Sentence inventory " + "-" * (W - 22))
    for tag, n in rep.by_sentence.most_common():
        p(f"   {tag}  {n:6d}")
    p("   talkers: " + ", ".join(f"{t}({n})" for t, n in rep.by_talker.most_common()))
    if rep.bad_checksum:
        pct = 100.0 * rep.bad_checksum / rep.total
        p(f"   !! {rep.bad_checksum} bad checksums ({pct:.1f}%) - wiring or baud noise")

    findings = []   # (severity, text)

    # 1. GPS ---------------------------------------------------------------
    p("\n-- GPS receivers (one row per talker) " + "-" * max(0, W - 39))
    if not rep.gps:
        p("   No GGA/RMC on this feed.")
    else:
        for talker, rec in sorted(rep.gps.items()):
            pct = 100.0 * rec["valid"] / rec["n"] if rec["n"] else 0
            when = ("" if rec["first_valid"] is None else
                    "  first fix at "
                    + time.strftime("%H:%M:%S", time.localtime(rec["first_valid"])))
            state = ("NO FIX AT ALL" if rec["valid"] == 0 else
                     "fix throughout" if pct > 95 else
                     f"INTERMITTENT - only {pct:.0f}% of samples valid")
            p(f"   ${talker}..  {rec['n']:4d} samples  {state}{when}")
            modes = rep.gsa_modes.get(talker)
            if modes:
                names = {1: "no fix", 2: "2D", 3: "3D"}
                p("        GSA: " + ", ".join(
                    f"{names.get(m, m)} x{n}" for m, n in sorted(modes.items())))
            if rec["valid"] == 0:
                findings.append(("CAUSE", f"GPS talker ${talker} never achieved "
                                          f"a fix in {rec['n']} samples."))
            elif pct < 90:
                findings.append(("WARN", f"GPS talker ${talker} had a fix in only "
                                         f"{pct:.0f}% of samples - marginal signal "
                                         f"or a cold start."))
        p("\n   NOTE: a healthy GPS here does NOT mean the transponder has one.")
        p("   If the AIS uses its own antenna, its position source is only")
        p("   observable in the !AIVDO fields below.")

    # --- cold-start gate --------------------------------------------------
    # A GPS powered up minutes ago reports exactly what a dead one does. If the
    # capture caught a cold start, no conclusion about the AIS is safe yet.
    cold = []
    for talker, snrs in rep.gsv_snr.items():
        if len(snrs) >= 8:
            early = snrs[:max(4, len(snrs) // 2)]
            zeros = sum(1 for s in early if s == 0)
            if zeros / len(early) > 0.8:
                cold.append(f"${talker} began with {zeros}/{len(early)} "
                            f"satellites at SNR 00")
    for talker, rec in rep.gps.items():
        if rec["first_valid"] and rec["valid"] < rec["n"] * 0.5:
            cold.append(f"${talker} had no fix for most of the capture, then "
                        f"acquired one part-way through")
    if cold:
        p("\n-- COLD START DETECTED " + "-" * (W - 24))
        for c in cold:
            p(f"   {c}")
        p("   The GPS was still acquiring during this capture. A receiver that")
        p("   has not fixed yet reports the SAME 'position unavailable' state as")
        p("   a broken one. Nothing below distinguishes them.")
        p("   Wait until GSA shows mode 3 steadily, then capture again.")
        findings.append(("INCONCLUSIVE",
                         "GPS was cold-starting - re-run once it holds a 3D fix."))

    # 2. Own-ship transmit -------------------------------------------------
    p("\n-- Own-ship reports (!AIVDO) " + "-" * (W - 30))
    if not rep.vdo:
        p("   NONE in the capture window.")
        p("   The unit is not even generating its own report - so this is upstream")
        p("   of the radio: no fix, no MMSI, or silent mode.")
        findings.append(("CAUSE", "Zero !AIVDO. Unit is not producing an own-ship "
                                  "report at all."))
    else:
        mmsis = collections.Counter(m["mmsi"] for m in rep.vdo)
        types = collections.Counter(m["type"] for m in rep.vdo)
        p(f"   {len(rep.vdo)} own-ship messages, types: "
          + ", ".join(f"{t}x{n}" for t, n in types.most_common()))
        for mmsi, n in mmsis.most_common():
            p(f"   MMSI {mmsi}  ({n})")
            if not mmsi or mmsi == 0:
                findings.append(("CAUSE", "MMSI is 0 / unprogrammed. The unit will "
                                          "never transmit until a valid MMSI is set."))
        if rep.last_position:
            p(f"   position in report: {rep.last_position[0]:.5f}, "
              f"{rep.last_position[1]:.5f}")
        else:
            p("   position field: NOT AVAILABLE (181/91 sentinel)")
            findings.append(("CAUSE", "!AIVDO reports position NOT AVAILABLE - the "
                                      "transponder itself has no fix, whatever the "
                                      "other GPS on the bus is doing."))

        # The transponder's own statement of why. This is the closest thing to
        # an error message a Class B ever emits.
        ts_seen = collections.Counter(m["ts"] for m in rep.vdo
                                      if m.get("ts") is not None)
        for ts, n in ts_seen.most_common():
            if ts in TS_MEANING:
                p(f"   timestamp field = {ts}: {TS_MEANING[ts]}   (x{n})")
                if ts == 63:
                    findings.append(("CAUSE", "AIVDO timestamp=63: the transponder "
                                              "reports its POSITIONING SYSTEM "
                                              "INOPERATIVE. This is the unit saying "
                                              "why it will not transmit."))
                elif ts in (61, 62):
                    findings.append(("WARN", f"AIVDO timestamp={ts}: "
                                             f"{TS_MEANING[ts]}"))
        blank = [k for k in ("sog", "cog", "hdg")
                 if all(m.get(k) is None for m in rep.vdo)]
        if blank:
            p("   also NOT AVAILABLE in every report: "
              + ", ".join(k.upper() for k in blank))
        ident = len({m for m in
                     (tuple(sorted(x.items(), key=str)) for x in rep.vdo)})
        if len(rep.vdo) > 3 and ident <= 2:
            p("   every report is byte-identical - the unit is repeating a static")
            p("   'nothing to report' frame, not updating from a live sensor.")
        if dur > 90 and len(rep.vdo) < 2:
            findings.append(("WARN", f"Only {len(rep.vdo)} own-ship report in "
                                     f"{dur:.0f}s. Class B expects one every 30s "
                                     f"underway / 3min at anchor."))

    # 3. Alarms ------------------------------------------------------------
    p("\n-- Alarms ($--ALR) " + "-" * (W - 20))
    if not rep.alarms:
        p("   None on the bus.")
    else:
        seen = {}
        for raw, aid, active, acked, text in rep.alarms:
            key = (aid, active)
            if key in seen:
                continue
            seen[key] = True
            label = ALARM_IDS.get(aid, "unknown alarm id - check the unit's manual")
            state = "ACTIVE" if active else ("cleared" if active is False else "?")
            p(f"   [{aid}] {state:8s} {label}")
            if text:
                p(f"           text: {text}")
            if active and aid in TX_BLOCKING:
                findings.append(("CAUSE", f"Alarm {aid} active: {label}"))
            elif active:
                findings.append(("WARN", f"Alarm {aid} active: {label}"))

    # 4. WHAT THE UNIT ACTUALLY SAID --------------------------------------
    # Everything below is verbatim. No interpretation, no filtering: if the
    # transponder stated a reason, it is in one of these four blocks.

    if rep.plain:
        p("\n-- Plain text from the unit (boot banner / self-test) "
          + "-" * max(0, W - 55))
        for t, line in rep.plain[:40]:
            p(f"   [{time.strftime('%H:%M:%S', time.localtime(t))}] {line}")
        if len(rep.plain) > 40:
            p(f"   ... and {len(rep.plain) - 40} more (see the capture file)")
        # Plain text that names a fault IS the answer - promote it to a cause,
        # quoted verbatim, rather than asking the reader to scroll back.
        promoted = 0
        for t, line in rep.plain:
            low2 = line.lower()
            if any(k in low2 for k in KEYWORDS) and promoted < 5:
                promoted += 1
                findings.append(("CAUSE", f'unit said: "{line}"'))
        if not promoted:
            findings.append(("LOOK", "Unit emitted plain text - read it above."))

    if rep.txt:
        p("\n-- Text messages ($--TXT) " + "-" * (W - 27))
        seen_txt = []
        for line in rep.txt:
            body = line.split(",", 4)[-1] if line.count(",") >= 4 else line
            if body in seen_txt:
                continue
            seen_txt.append(body)
            p(f"   {line}")
        if not seen_txt:
            p("   (none)")

    if rep.abk:
        p("\n-- Transmit acknowledgements ($--ABK) " + "-" * (W - 38))
        codes = {"0": "message broadcast successfully",
                 "1": "broadcast in progress / later attempt",
                 "2": "addressed message, no acknowledgement",
                 "3": "requested broadcast could not be broadcast",
                 "4": "late reception"}
        for line in rep.abk[:20]:
            f2 = line.split("*")[0].split(",")
            # $--ABK,<MMSI>,<channel>,<msgID>,<sequence>,<ackType>
            # The result is the LAST field, not a fixed index - leading fields
            # are routinely empty for broadcast messages.
            code = f2[-1].strip() if len(f2) > 1 else ""
            p(f"   {line}")
            if code:
                p(f"        -> {codes.get(code, 'unknown result code')}")
            if code and code != "0":
                findings.append(("CAUSE", f"ABK result {code}: "
                                          f"{codes.get(code, 'transmit refused')}"))

    if rep.other:
        p("\n-- Non-routine sentences (verbatim) " + "-" * (W - 36))
        p("   Anything here is not ordinary nav traffic. Vendor error output")
        p("   lives in these - especially $P... proprietary sentences.")
        for head, rec in sorted(rep.other.items(),
                                key=lambda kv: -kv[1]["count"]):
            if head[1:4] in ("ALR",) or head[1:].startswith(("AIALR",)):
                continue   # already shown in the alarm section
            p(f"\n   {head}   x{rec['count']}   first at "
              f"{time.strftime('%H:%M:%S', time.localtime(rec['t']))}")
            for s in rec["samples"]:
                p(f"      {s}")
    else:
        p("\n-- Non-routine sentences " + "-" * (W - 26))
        p("   None. The unit emitted only ordinary nav/AIS traffic and said")
        p("   nothing about a fault on this feed.")

    if rep.keyword_hits:
        p("\n-- Lines containing fault words " + "-" * (W - 32))
        shown = set()
        for line, hits in rep.keyword_hits:
            if line in shown:
                continue
            shown.add(line)
            p(f"   {line}")
            p(f"        matched: {', '.join(hits)}")
            if len(shown) >= 25:
                p("   ... truncated, see the capture file")
                break

    # 5. Receive path sanity ----------------------------------------------
    p("\n-- Receive path " + "-" * (W - 17))
    if rep.vdm_mmsi:
        p(f"   Hearing {len(rep.vdm_mmsi)} other vessels - the receiver and the")
        p("   VHF antenna path are working. The fault is transmit-side only.")
    else:
        p("   No other vessels heard either. Either you are somewhere empty, or")
        p("   the antenna/coax is bad in both directions (one antenna, one fault).")

    # Verdict --------------------------------------------------------------
    p("\n" + "=" * W)
    causes = [t for s, t in findings if s == "CAUSE"]
    warns = [t for s, t in findings if s == "WARN"]
    inconclusive = [t for s, t in findings if s == "INCONCLUSIVE"]
    if inconclusive:
        p("  RESULT: INCONCLUSIVE - do not act on the findings below.")
        for t in inconclusive:
            p(f"    ! {t}")
        if causes:
            p("\n  (these would be the causes, but this capture cannot")
            p("   distinguish them from a GPS that simply has not fixed yet:)")
            for t in causes:
                p(f"    - {t}")
        p("=" * W)
        p("")
        return 3
    if causes:
        p("  LIKELY CAUSE" + ("S" if len(causes) > 1 else ""))
        for t in causes:
            p(f"    * {t}")
    else:
        p("  No blocking condition found on the NMEA bus.")
        p("    If it still is not being seen by others, suspect: silent-mode")
        p("    switch, VHF antenna/coax or splitter, or a Tx stage fault that")
        p("    the unit is not self-reporting. Confirm reception independently")
        p("    (another vessel, a shore receiver, or MarineTraffic) before")
        p("    concluding the unit is at fault - coverage gaps look identical.")
    for t in warns:
        p(f"  warn: {t}")
    for t in [x for s, x in findings if s == "LOOK"]:
        p(f"  look: {t}")
    p("=" * W)
    p("")
    return 0 if not causes else 1


# ------------------------------------------------------------------- capture

def open_tcp(host, port, timeout=5.0):
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(1.0)
    return s


def nmea_out(body):
    """Wrap a sentence body with '$' and a correct checksum."""
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}\r\n"


# IEC 61162-1 query sentences: $ttllQ,sss - requester tt, target ll, wanted sss.
# READ-ONLY. Nothing here writes configuration, changes MMSI, or keys the
# transmitter; a query only asks the device to report something it already
# knows. Anything that mutates the unit is deliberately absent.
QUERIES = [
    ("CCAIQ,ALR", "AIS: report active alarms"),
    ("CCAIQ,TXT", "AIS: report status text"),
    ("CCAIQ,VER", "AIS: report firmware version"),
    ("CCAIQ,VDO", "AIS: report own-ship data"),
    ("CCAIQ,SSD", "AIS: report static ship data"),
    ("CCAIQ,VSD", "AIS: report voyage data"),
    ("CCGPQ,GGA", "GPS: report a fix"),
]


def interrogate(host, port, seconds, capture_path, serial_dev=None, baud=38400):
    """Ask the transponder to state its alarms, over NMEA itself.

    Over TCP this works only if the endpoint is bidirectional. Over the USB
    serial port it is the transceiver's own command channel, which is the
    reliable path. Read-only either way: see QUERIES.
    """
    if serial_dev:
        print(f"Opening {serial_dev} at {baud} baud (read/write) ...")
        try:
            import subprocess
            # clocal + -crtscts matter: a USB virtual COM port often does not
            # assert CTS, and with hardware flow control left on macOS refuses
            # the write with ENXIO ("Device not configured").
            r = subprocess.run(["stty", "-f", serial_dev, str(baud), "raw",
                                "-echo", "cs8", "-cstopb", "-parenb",
                                "clocal", "-crtscts", "-ixon", "-ixoff"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise OSError(r.stderr.strip())
            sock = None
            # Keep O_NONBLOCK SET. On macOS a /dev/cu.* virtual COM port
            # refuses writes with ENXIO once O_NONBLOCK is cleared, so reads
            # tolerate EAGAIN instead (see _recv).
            fd = os.open(serial_dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            fp = os.fdopen(fd, "r+b", buffering=0)
        except OSError as e:
            print(f"  CANNOT OPEN: {e}")
            print("  Is proAIS2 still connected? Disconnect it first - only one")
            print("  program can hold the port.")
            return None, 2
    else:
        print(f"Connecting to {host}:{port} ...")
        try:
            sock = open_tcp(host, port)
            fp = None
        except OSError as e:
            print(f"  CANNOT CONNECT: {e}")
            return None, 2
    print("Connected.\n")
    print("Sending READ-ONLY query sentences. None of these change any setting,")
    print("touch the MMSI, or key the transmitter.\n")

    def _recv(n):
        if fp:
            try:
                return fp.read(n) or b""
            except BlockingIOError:
                # Non-blocking fd with nothing buffered yet. Not an error.
                time.sleep(0.05)
                return b""
        return sock.recv(n)

    def _send(data):
        if fp:
            fp.write(data)
            fp.flush()
        else:
            sock.sendall(data)

    def _close():
        (fp or sock).close()

    rep = Report()
    fh = open(capture_path, "w") if capture_path else None
    buf = b""
    responses = []

    def drain(window):
        nonlocal buf
        end = time.time() + window
        got = []
        while time.time() < end:
            try:
                chunk = _recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                # Serial fd is non-blocking: empty just means "nothing yet".
                if fp:
                    continue
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if fh and line:
                    fh.write(line + "\n")
                    fh.flush()
                hit = rep.feed(line)
                if hit:
                    got.append(hit)
        return got

    print("Baseline: listening 10s before asking anything...")
    baseline = set(drain(10))
    if not rep.total:
        print("  No data at all - the link is dead, stop here.")
        _close()
        if fh:
            fh.close()
        return rep, 2
    print(f"  baseline: {rep.total} sentences, "
          f"{len(baseline)} distinct non-routine\n")

    writable = True
    for body, what in QUERIES:
        sentence = nmea_out(body)
        try:
            _send(sentence.encode("ascii"))
        except OSError as e:
            print(f"  !! cannot write to this socket ({e})")
            print("  The feed is one-way; the unit cannot be queried this way.")
            writable = False
            break
        print(f"  -> ${body:<12s}  {what}")
        new = [l for l in drain(6) if l not in baseline]
        for line in dict.fromkeys(new):
            print(f"       RESPONSE: {line}")
            responses.append((body, line))
        if not new:
            print("       (no new sentence)")

    _close()
    if fh:
        fh.close()

    print("\n" + "=" * 72)
    if not writable:
        print("  Link is receive-only. Query route is closed.")
    elif responses:
        print("  THE UNIT ANSWERED:")
        for body, line in responses:
            print(f"    ${body} -> {line}")
    else:
        print("  The unit accepted the writes but answered nothing.")
        print("  Either the TCP path is output-only downstream of a multiplexer,")
        print("  or this transponder does not implement query sentences.")
    print("=" * 72)
    return rep, 0


def open_serial(dev, baud):
    """Open a USB/serial NMEA port with stty + a plain file handle.

    No pyserial: this has to run on a boat with no internet to pip-install
    anything. The MDA-5's USB port is its own diagnostic channel and carries
    output the multiplexed network feed may never forward.
    """
    import subprocess
    r = subprocess.run(["stty", "-f", dev, str(baud), "raw", "-echo",
                        "cs8", "-cstopb", "-parenb"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError(f"stty failed on {dev}: {r.stderr.strip()}")
    return open(dev, "rb", buffering=0)


def list_serial_ports():
    import glob
    return sorted(glob.glob("/dev/tty.usb*") + glob.glob("/dev/cu.usb*")
                  + glob.glob("/dev/tty.SLAB*") + glob.glob("/dev/tty.wchusb*"))


def capture_serial(dev, baud, seconds, capture_path):
    print(f"Opening {dev} at {baud} baud ...")
    try:
        fp = open_serial(dev, baud)
    except OSError as e:
        print(f"  CANNOT OPEN: {e}")
        ports = list_serial_ports()
        print("  Serial ports present: " + (", ".join(ports) or "NONE"))
        return None, 2
    print(f"Open. Capturing {seconds}s ...  non-routine lines echo below:\n")
    rep = Report()
    fh = open(capture_path, "w") if capture_path else None
    buf = b""
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            try:
                chunk = fp.read(512)
            except Exception:
                time.sleep(0.2)
                continue
            if not chunk:
                time.sleep(0.1)
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if fh and line:
                    fh.write(line + "\n")
                    fh.flush()
                hit = rep.feed(line)
                if hit:
                    print(f"  >> {time.strftime('%H:%M:%S')}  {hit}")
    except KeyboardInterrupt:
        print("\n  stopped early")
    finally:
        fp.close()
        if fh:
            fh.close()
    return rep, 0


def local_ipv4s():
    """Every IPv4 this Mac holds, so we can scan the subnet we are actually on."""
    out = []
    try:
        import subprocess
        txt = subprocess.run(["/sbin/ifconfig"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "127.0.0.1" not in line:
                out.append(line.split()[1])
    except Exception:
        pass
    return out


def arp_neighbours():
    """Hosts the Mac has already talked to - the AIS box shows up here once
    it has answered anything, even if it ignores ping."""
    hosts = []
    try:
        import re
        import subprocess
        txt = subprocess.run(["arp", "-a"], capture_output=True,
                             text=True, timeout=5).stdout
        hosts = re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)", txt)
    except Exception:
        pass
    return hosts


def scan():
    mine = local_ipv4s()
    print("This Mac's IPv4 addresses: " + (", ".join(mine) or "none"))

    candidates = list(SCAN_HOSTS)
    on_boat_subnet = any(ip.startswith("192.168.47.") for ip in mine)
    if not on_boat_subnet:
        print("\n  WARNING: no 192.168.47.x address on this Mac.")
        print("  The AIS unit lives at 192.168.47.10. With the Pi down, nothing")
        print("  is bridging you onto that subnet. Either join the boat WiFi, or")
        print("  plug ethernet straight into the unit and give this Mac a static")
        print("  address on its subnet:")
        print("    System Settings > Network > Ethernet > Details > TCP/IP")
        print("    Configure IPv4: Manually   IP 192.168.47.50   Mask 255.255.255.0")
        print("  Then re-run --scan.\n")

    for ip in arp_neighbours():
        if ip not in candidates:
            candidates.append(ip)
    # Also sweep the /24 we are actually on, in case the unit self-assigned.
    for ip in mine:
        octets = ip.split(".")
        if len(octets) == 4 and not ip.startswith("169.254."):
            base = ".".join(octets[:3])
            for last in (1, 10, 11, 100, 254):
                cand = f"{base}.{last}"
                if cand not in candidates and cand != ip:
                    candidates.append(cand)

    print(f"Scanning {len(candidates)} hosts x {len(SCAN_PORTS)} ports "
          f"(1.5s each, be patient)...\n")
    found = []
    for host in candidates:
        for port in SCAN_PORTS:
            try:
                s = socket.create_connection((host, port), timeout=1.5)
            except OSError:
                continue
            s.settimeout(4.0)
            try:
                data = s.recv(2048).decode("ascii", "replace")
            except OSError:
                data = ""
            s.close()
            nmea = any(l.startswith(("$", "!")) for l in data.splitlines())
            print(f"  {host}:{port}  {'NMEA DATA' if nmea else 'open, silent 4s'}")
            if nmea:
                found.append((host, port))
    if not found:
        print("\nNo NMEA feed found.")
        print("If a port was 'open, silent' the unit may only talk after a")
        print("power cycle - run the capture against it and reboot the unit.")
        return 2
    print("\nFeed(s) found. Run:")
    for host, port in found:
        print(f"  python3 ais_diagnose.py --host {host} --port {port} --seconds 180")
    return 0


def capture(host, port, seconds, use_udp, capture_path):
    rep = Report()
    fh = open(capture_path, "w") if capture_path else None
    deadline = time.time() + seconds

    if use_udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        sock.settimeout(1.0)
        print(f"Listening UDP :{port} for {seconds}s ... (ctrl-C to stop early)")
    else:
        print(f"Connecting to {host}:{port} ...")
        try:
            sock = open_tcp(host, port)
        except OSError as e:
            print(f"\n  CANNOT CONNECT: {e}")
            print("  Are you on the boat WiFi? Try: python3 ais_diagnose.py --scan")
            if fh:
                fh.close()
            return None, 2
        print(f"Connected. Capturing {seconds}s ... (ctrl-C to stop early)")
        print("  >>> POWER-CYCLE THE AIS UNIT NOW. <<<")
        print("  Most transponders state their fault once, in the boot self-test.")
        print("  Non-routine lines are echoed below as they arrive:\n")


    buf = b""
    last_note = time.time()
    try:
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096) if not use_udp else sock.recvfrom(4096)[0]
            except socket.timeout:
                continue
            except OSError as e:
                print(f"  link dropped: {e}")
                break
            if not chunk and not use_udp:
                print("  peer closed the connection")
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if fh and line:
                    fh.write(line + "\n")
                    fh.flush()   # survive a ctrl-C or a power cut mid-capture
                hit = rep.feed(line)
                if hit:
                    # Echo anything non-routine the moment it arrives. Boot
                    # banners and self-test errors appear once, at power-up,
                    # and must not be buried until the summary prints.
                    print(f"  >> {time.strftime('%H:%M:%S')}  {hit}")
            if time.time() - last_note >= 15:
                last_note = time.time()
                left = int(deadline - time.time())
                print(f"   ... {rep.total} sentences, {len(rep.vdo)} own-ship, "
                      f"{left}s left")
    except KeyboardInterrupt:
        print("\n  stopped early")
    finally:
        sock.close()
        if fh:
            fh.close()
    return rep, 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--seconds", type=int, default=120,
                    help="capture window; use >=120 so a 30s Class B cycle lands")
    ap.add_argument("--udp", action="store_true", help="listen for UDP broadcast")
    ap.add_argument("--scan", action="store_true", help="probe common host:port pairs")
    ap.add_argument("--replay", metavar="FILE", help="analyse a saved capture")
    ap.add_argument("--query", action="store_true",
                    help="interrogate the unit with read-only NMEA query "
                         "sentences (needs a bidirectional TCP link)")
    ap.add_argument("--serial", metavar="DEV",
                    help="read the transceiver's USB/serial port instead of "
                         "TCP, e.g. /dev/tty.usbserial-1420")
    ap.add_argument("--baud", type=int, default=38400,
                    help="serial baud (MDA-5 NMEA0183 output is 38400)")
    ap.add_argument("--list-serial", action="store_true",
                    help="list USB serial ports and exit")
    ap.add_argument("--out", default=None, help="capture file (default: timestamped)")
    args = ap.parse_args()

    if args.scan:
        return scan()

    if args.replay:
        rep = Report()
        with open(args.replay, "r", errors="replace") as fh:
            for line in fh:
                rep.feed(line)
        return render(rep, args.replay, 0)

    if args.list_serial:
        ports = list_serial_ports()
        print("USB serial ports: " + (", ".join(ports) or "NONE FOUND"))
        return 0 if ports else 2

    out = args.out or time.strftime("ais_capture_%Y%m%d_%H%M%S.nmea")

    if args.serial and not args.query:
        rep, err = capture_serial(args.serial, args.baud, args.seconds, out)
        if rep is None:
            return err
        return render(rep, out, args.seconds)

    if args.query:
        rep, err = interrogate(args.host, args.port, args.seconds, out,
                               serial_dev=args.serial, baud=args.baud)
        if rep is None:
            return err
        return render(rep, out, args.seconds)

    rep, err = capture(args.host, args.port, args.seconds, args.udp, out)
    if rep is None:
        if os.path.exists(out) and os.path.getsize(out) == 0:
            os.remove(out)
        return err
    return render(rep, out, args.seconds)


if __name__ == "__main__":
    sys.exit(main())
