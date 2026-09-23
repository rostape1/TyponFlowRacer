#!/usr/bin/env python3
"""Tests for the GPS-derived log clock in nmea_capture.py.

Run: python3 tests/test_log_clock.py

Why this exists. The Pi has no RTC. With no internet at sea, NTP never runs, so
`fake-hwclock` restores whatever time the Pi had at its last shutdown and the
clock stays behind by however long it was powered off — cumulatively. On
2026-09-23 a survey of the archive found **52 of 76 recordings mis-stamped**, by
up to 115 hours. Filenames and line prefixes agreed with each other because both
came from the same wrong clock, so nothing looked broken; the GPS time inside
`$GPRMC` was the only ground truth in the file.

That is not a cosmetic defect. The filename is how you find a race, and the
playback auto-advance gap check compares filename times — with a clock that
jumps between boots it will happily chain two recordings that are days apart.

So: the logger derives its own clock from GPS and never trusts the system clock
for anything it writes. These tests pin that behaviour down.

Fixtures are real sentences lifted from nmea_2026-09-17_080000.txt, checksums
verified. That file is named 08:00 local and actually recorded 2026-09-19
16:52:05 UTC — it IS the bug.
"""

import os
import re
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Import fresh, and make sure a stale .pyc can never satisfy this import (P21).
sys.dont_write_bytecode = True
import nmea_capture as nc  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")


def section(name):
    print(f"{name}:")


# Real sentences, checksums verified. Both say 2026-09-19 16:52:0x UTC.
RMC_1 = "$GPRMC,165205,A,3749.1194,N,12225.4533,W,006.1,058.3,190926,015.1,E*6B"
RMC_2 = "$GPRMC,165206,A,3749.1202,N,12225.4514,W,006.0,060.1,190926,015.1,E*69"
GPS_1 = datetime(2026, 9, 19, 16, 52, 5, tzinfo=timezone.utc).timestamp()
GPS_2 = datetime(2026, 9, 19, 16, 52, 6, tzinfo=timezone.utc).timestamp()

# The wrong system clock this file was actually written under: 2026-09-17
# 15:00:00Z, i.e. 49.87 h behind the GPS truth above.
BAD_SYS = datetime(2026, 9, 17, 15, 0, 0, tzinfo=timezone.utc).timestamp()


def make_rmc(epoch, status="A", talker="GP"):
    """Build a checksum-correct $..RMC for an arbitrary instant.

    Needed because a fixed fixture cannot express "GPS time advances in step with
    system time" — holding GPS constant while system time moves simulates a
    frozen receiver, which is a genuinely drifting offset, not a stable clock.
    """
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    body = (f"{talker}RMC,{dt.strftime('%H%M%S')},{status},"
            f"3749.1194,N,12225.4533,W,006.1,058.3,{dt.strftime('%d%m%y')},015.1,E")
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}"


def reset():
    """Start every case from a known clock state."""
    nc.reset_gps_clock()


# --- checksum ------------------------------------------------------------
section("checksum")

check(nc.nmea_checksum_ok(RMC_1), "a real RMC must validate")
check(nc.nmea_checksum_ok(RMC_2), "and so must the second")
# One digit of the payload flipped: structurally perfect, checksum now wrong.
check(not nc.nmea_checksum_ok(RMC_1.replace("3749.1194", "3749.1195")),
      "a corrupted RMC must fail the checksum, or a garbled sentence can move the clock")
check(not nc.nmea_checksum_ok("$GPRMC,165205,A,3749.1194,N"),
      "a sentence with no '*' must fail rather than raise")
check(not nc.nmea_checksum_ok(""), "an empty line must fail rather than raise")
check(not nc.nmea_checksum_ok("$GPRMC,165205,A*ZZ"), "a non-hex checksum must fail")

# --- parsing GPS time ----------------------------------------------------
section("parse_rmc_utc")

check(nc.parse_rmc_utc(RMC_1) == GPS_1,
      f"RMC 165205/190926 must parse to 2026-09-19 16:52:05Z, got {nc.parse_rmc_utc(RMC_1)}")
# Talker id must not matter: the boat's own instruments emit $IIRMC alongside the
# GPS's $GPRMC. Checksum recomputed for the II variant, not copied from the GP one.
check(nc.parse_rmc_utc(
    "$IIRMC,165205,A,3749.1194,N,12225.4533,W,006.1,058.3,190926,015.1,E*7C") == GPS_1,
      "an $IIRMC must be accepted too — talker id is not part of the clock decision")
# Status V means "navigation receiver warning" — the fix is not valid, so its
# time may be the receiver's own uninitialised clock. Checksum is VALID here, so
# this test fails for the intended reason and not on the checksum.
check(nc.parse_rmc_utc(
    "$GPRMC,165205,V,3749.1194,N,12225.4533,W,006.1,058.3,190926,015.1,E*7C") is None,
      "an RMC with status V must be rejected — an invalid fix may carry an invalid time")
check(nc.parse_rmc_utc(RMC_1.replace("3749.1194", "3749.1195")) is None,
      "a bad checksum must be rejected here too, not just by nmea_checksum_ok")
check(nc.parse_rmc_utc("$GPGGA,165205,3749.1194,N,12225.4533,W,1,08,0.9,5.4,M,,,,*05") is None,
      "GGA carries no date, so it cannot set the clock")
check(nc.parse_rmc_utc("$GPRMC,,A,,,,,,,,,*26") is None,
      "an RMC with empty time/date fields is rejected")
check(nc.parse_rmc_utc(
    "$GPRMC,999999,A,3749.1194,N,12225.4533,W,006.1,058.3,190926,015.1,E*6E") is None,
      "an impossible time must be rejected, not rolled over")

# --- adopting an offset --------------------------------------------------
section("offset adoption")

reset()
changed = nc.observe_gps_time(RMC_1, now=BAD_SYS)
check(changed is False, "one reading must NOT adopt — a single garbled sentence cannot move the clock")
check(nc.clock_state()["source"] == "system", "still on the system clock after one reading")

changed = nc.observe_gps_time(RMC_2, now=BAD_SYS + 1)
check(changed is True, "a second consistent reading adopts the offset and signals a rotation")
st = nc.clock_state()
check(st["source"] == "gps", f"source should be 'gps', got {st['source']}")
check(abs(st["offset_s"] - (GPS_1 - BAD_SYS)) < 2,
      f"offset should be ~{GPS_1 - BAD_SYS:.0f}s (49.87h), got {st['offset_s']}")

# --- inconsistent readings must not adopt --------------------------------
reset()
nc.observe_gps_time(RMC_1, now=BAD_SYS)
# Second reading implies a wildly different offset (system clock "jumped" an
# hour between the two), so the pair does not agree and nothing is adopted.
changed = nc.observe_gps_time(RMC_2, now=BAD_SYS + 3600)
check(changed is False, "two readings that disagree must not adopt an offset")
check(nc.clock_state()["source"] == "system", "and the clock stays unknown")

# --- a settled clock is not re-adopted every second ---------------------
# GPS time advances in step with system time here, which is what a healthy
# receiver actually does. The offset is therefore constant and must be adopted
# exactly once — every spurious "changed" would start a new log file.
reset()
nc.observe_gps_time(RMC_1, now=BAD_SYS)
nc.observe_gps_time(RMC_2, now=BAD_SYS + 1)
before = nc.clock_state()["offset_s"]
rotations = sum(
    bool(nc.observe_gps_time(make_rmc(GPS_2 + 1 + i), now=BAD_SYS + 2 + i))
    for i in range(20)
)
check(rotations == 0,
      f"a stable clock must not keep signalling rotation ({rotations} spurious rotations) — "
      "every one would start a new log file")
check(nc.clock_state()["offset_s"] == before, "and the offset must not drift")

# --- a receiver whose clock freezes IS a real offset change --------------
# The mirror of the case above: if GPS time stops advancing while system time
# does, the offset genuinely is changing and must eventually be re-adopted.
reset()
nc.observe_gps_time(RMC_1, now=BAD_SYS)
nc.observe_gps_time(RMC_2, now=BAD_SYS + 1)
froze = any(bool(nc.observe_gps_time(RMC_2, now=BAD_SYS + 2 + i)) for i in range(20))
check(froze, "a frozen GPS clock drifts past tolerance and must be re-adopted, not ignored")

# --- the NTP step: offset changes mid-recording -------------------------
# This is what makes a single file internally inconsistent, which breaks both
# the replay parser and the auto-advance gap check. It must force a rotation.
reset()
nc.observe_gps_time(RMC_1, now=BAD_SYS)
nc.observe_gps_time(RMC_2, now=BAD_SYS + 1)
# NTP finally reaches the Pi and steps the system clock to the truth: the
# offset should collapse to ~0.
good_sys = GPS_1 + 10
changed_a = nc.observe_gps_time(RMC_1, now=good_sys)
changed_b = nc.observe_gps_time(RMC_2, now=good_sys + 1)
check(changed_b is True or changed_a is True,
      "a real clock step must be adopted and signal a rotation")
check(abs(nc.clock_state()["offset_s"]) < 15,
      f"after NTP corrects the system clock the offset should collapse to ~0, "
      f"got {nc.clock_state()['offset_s']}")

# --- clock_now ------------------------------------------------------------
section("clock_now")

reset()
nc.observe_gps_time(RMC_1, now=BAD_SYS)
nc.observe_gps_time(RMC_2, now=BAD_SYS + 1)
got = nc.clock_now(now=BAD_SYS + 1)
check(abs(got.timestamp() - GPS_2) < 2,
      f"clock_now must report GPS truth, not the system clock; got {got.isoformat()}")
check(got.tzinfo is not None, "clock_now must be timezone-aware")

reset()
got = nc.clock_now(now=BAD_SYS)
check(abs(got.timestamp() - BAD_SYS) < 1,
      "with no GPS fix, clock_now falls back to the system clock rather than refusing")

# --- filenames ------------------------------------------------------------
section("filenames")

reset()
nc.observe_gps_time(RMC_1, now=BAD_SYS)
nc.observe_gps_time(RMC_2, now=BAD_SYS + 1)
name = os.path.basename(nc.make_filename(now=BAD_SYS + 1))
# Local time of 2026-09-19 16:52:06Z. Asserted against the same conversion the
# code does, so the test does not hardcode a timezone.
want = datetime.fromtimestamp(GPS_2).strftime("nmea_%Y-%m-%d_%H%M%S.txt")
check(name == want, f"filename must use GPS truth: expected {want}, got {name}")
check("_noclock" not in name, "a GPS-derived name must not be marked _noclock")

reset()
name = os.path.basename(nc.make_filename(now=BAD_SYS))
check("_noclock" in name,
      f"with no GPS fix the filename MUST say so — a silently wrong date is the whole bug; got {name}")

# --- end-to-end: rotation and stamps on disk -----------------------------
section("rotation on disk")

with tempfile.TemporaryDirectory() as tmp:
    reset()
    old_dir, nc.LOG_DIR = nc.LOG_DIR, tmp
    nc._current_hour = None
    nc._outfile = None
    try:
        # Two sentences under the bad clock, before any GPS fix.
        nc.log_sentence("$IIMWV,045,R,12.3,N,A*12", "test", now=BAD_SYS)
        first = nc.stats["current_file"]
        # Now GPS arrives and the clock is corrected.
        nc.log_sentence(RMC_1, "test", now=BAD_SYS)
        nc.log_sentence(RMC_2, "test", now=BAD_SYS + 1)
        second = nc.stats["current_file"]

        check(first != second,
              "adopting a GPS offset must rotate to a new file, so no single file "
              "mixes two clocks")
        check("_noclock" in os.path.basename(first),
              f"the pre-fix file must be marked unreliable, got {os.path.basename(first)}")
        check("_noclock" not in os.path.basename(second),
              f"the corrected file must not be, got {os.path.basename(second)}")

        if nc._outfile:
            nc._outfile.flush()
        body = open(second, encoding="utf-8").read()
        check("# clock: gps" in body,
              "the corrected file's header must record its clock provenance")
        # Every data line in the corrected file must carry GPS-true time.
        data = [l for l in body.splitlines() if l and not l.startswith("#")]
        check(bool(data), "the corrected file must contain the sentences")
        check(all(l.startswith("2026-09-19") for l in data),
              f"stamps must be GPS-true (2026-09-19), got {data[0][:30] if data else 'none'}")

        pre = open(first, encoding="utf-8").read()
        check("# clock: system" in pre and "NO GPS" in pre.upper(),
              "the pre-fix file's header must warn that its times are unverified")

        # A clock step SMALLER than one hour must still rotate. The case above
        # also crossed an hour boundary, so `hour != _current_hour` would have
        # rotated on its own and the clock_changed test was doing no work.
        # A 10-second step leaves the hour key identical: only clock_changed
        # can catch it, and if it does not, one file ends up holding two clocks.
        before_file = nc.stats["current_file"]
        stepped = GPS_2 + 10           # GPS jumps 10s ahead of where we are
        nc.log_sentence(make_rmc(stepped), "test", now=BAD_SYS + 2)
        nc.log_sentence(make_rmc(stepped + 1), "test", now=BAD_SYS + 3)
        check(nc.stats["current_file"] != before_file,
              "a sub-hour clock step must still rotate — otherwise one file holds "
              "two clocks and replay sees time run backwards")
    finally:
        if nc._outfile:
            nc._outfile.close()
            nc._outfile = None
        nc.LOG_DIR = old_dir
        nc._current_hour = None

# --- the optional system-clock push --------------------------------------
# This is a bonus, never a requirement: the logs are already correct from the
# offset alone. So every failure path must be non-fatal and must SAY what
# happened, rather than leaving "did the clock get set?" unanswerable.
section("system clock push")


class FakeRun:
    def __init__(self, rc=0, stderr=""):
        self.rc, self.stderr, self.calls = rc, stderr, []

    def __call__(self, cmd):
        self.calls.append(cmd)

        class R:
            pass
        r = R()
        r.returncode, r.stderr, r.stdout = self.rc, self.stderr, ""
        return r


def reset_push():
    nc._clock_push.update(tried=False, ok=None, detail="not attempted")


reset_push()
r = FakeRun()
with tempfile.NamedTemporaryFile(suffix="-helper") as helper:
    ok = nc.push_clock_to_system(49.87 * 3600, helper=helper.name, runner=r)
    check(ok is True, f"a successful helper call must report success, got {ok}")
    check(len(r.calls) == 1, f"exactly one invocation, got {len(r.calls)}")
    cmd = r.calls[0]
    check(cmd[0] == "sudo" and "-n" in cmd,
          f"must call sudo non-interactively — a prompt would hang the logger forever; got {cmd}")
    check(cmd[2] == helper.name, f"must invoke the helper, not date(1) directly; got {cmd}")
    check(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", cmd[3])),
          f"the time argument must be exactly 'YYYY-MM-DD HH:MM:SS', got {cmd[3]!r}")

    # Attempted once per process, not once per sentence: re-stepping every second
    # would fight NTP and produce a jittering clock.
    before = len(r.calls)
    nc.push_clock_to_system(49.87 * 3600, helper=helper.name, runner=r)
    check(len(r.calls) == before, "must not retry — once per process")

reset_push()
ok = nc.push_clock_to_system(49.87 * 3600, helper="/nonexistent/helper", runner=FakeRun())
check(ok is False, "a missing helper must report failure, not raise")
check("not installed" in nc._clock_push["detail"],
      f"and must say WHY, so it is diagnosable; got {nc._clock_push['detail']!r}")

reset_push()
r = FakeRun(rc=1, stderr="sudo: a password is required")
with tempfile.NamedTemporaryFile(suffix="-helper") as helper:
    ok = nc.push_clock_to_system(49.87 * 3600, helper=helper.name, runner=r)
    check(ok is False, "a non-zero exit must report failure")
    check("password" in nc._clock_push["detail"],
          f"and must surface the helper's stderr; got {nc._clock_push['detail']!r}")

reset_push()
r = FakeRun()
with tempfile.NamedTemporaryFile(suffix="-helper") as helper:
    nc.push_clock_to_system(0.5, helper=helper.name, runner=r)
    check(len(r.calls) == 0, "an already-correct system clock must not be stepped at all")


def _raise(cmd):
    raise OSError("boom")


reset_push()
with tempfile.NamedTemporaryFile(suffix="-helper") as helper:
    ok = nc.push_clock_to_system(49.87 * 3600, helper=helper.name, runner=_raise)
    check(ok is False,
          "an exception in the runner must be caught — this must never take the logger down")

# It must be OFF unless explicitly asked for. Silently stepping a boat's system
# clock because the software was updated would be its own incident.
check(nc.SET_SYSTEM_CLOCK is False,
      "--set-system-clock must default to off: it needs a privileged one-time setup")

# --- the helper script itself --------------------------------------------
section("helper script")

helper_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "pi", "ais-set-clock.sh"), encoding="utf-8").read()
check("[0-9]{4}-[0-9]{2}-[0-9]{2}" in helper_src,
      "the root helper must validate its argument's shape — its input arrives over a radio link")
check("2024" in helper_src and "2040" in helper_src,
      "and must bound the year, or a GPS rollover could set the clock to 1999")
check("NTPSynchronized" in helper_src,
      "and must stand down when NTP is actually in charge, rather than fighting it")

# --- P33: durations must never come off this clock -----------------------
section("P33")

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "nmea_capture.py"), encoding="utf-8").read()
# Scope to the function body — up to the next top-level def — rather than a
# fixed character window, which silently excluded the line under test.
body = src.split("def uptime_seconds")[1].split("\ndef ")[0]


def code_only(text):
    """Strip the docstring, so a mention of a bug in prose is not read as code."""
    parts = text.split('"""')
    return parts[0] + "".join(parts[2::2]) if len(parts) > 2 else text


check("time.monotonic()" in code_only(body),
      "uptime_seconds must still use time.monotonic — a GPS step is exactly the "
      "jump P33 is about, and it must not be counted as elapsed time")
check("time.time()" not in code_only(body),
      "and it must not read wall clock at all")
# The GPS offset is a wall-clock correction. Letting it near a duration would
# reintroduce P33 through the new code rather than the old.
for fn in ("uptime_seconds", "disk_stats"):
    b = code_only(src.split(f"def {fn}")[1].split("\ndef ")[0])
    check("_gps_offset" not in b and "clock_now" not in b,
          f"{fn} must not touch the GPS clock — durations stay monotonic (P33)")

print(f"\n{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("all passed")
