#!/usr/bin/env python3
"""Repair NMEA recordings whose timestamps came from the Pi's invented clock.

    python3 tools/fix_log_times.py SRC_DIR                 # survey (default)
    python3 tools/fix_log_times.py SRC_DIR -o OUT_DIR --apply

THE BUG. The Pi has no RTC. With no internet at sea NTP never runs, so
fake-hwclock restores whatever time the Pi had at its last shutdown and the clock
stays behind by however long it was powered off — cumulatively, across boots. On
2026-09-23 a survey of 76 recordings found 52 mis-stamped, by up to 115 hours.

Nothing looked broken, because the filename and the line prefixes came from the
same wrong clock and therefore agreed with each other. The GPS time inside
`$..RMC` — straight off the satellites, independent of the Pi — is the only
ground truth in the file.

WHAT THIS DOES. For each recording, measures the offset between the Pi's stamps
and GPS truth, then writes a corrected copy: every line prefix shifted, and the
file renamed to its true local time.

WHAT IT WILL NOT DO.

  * It never modifies or deletes the input. NMEA logs are race data and are the
    one thing here that cannot be refetched (P34). Corrected files go to a
    separate directory and the originals stay exactly as they are.
  * It never guesses. A file with no valid GPS fix cannot be corrected, so it is
    copied verbatim into `no-gps/` and listed in the manifest as unresolved,
    rather than being assumed fine.
  * It splits rather than smooths. If the offset changes part-way through a file
    (NTP finally arriving, or the first fix landing minutes in), the output is
    split at that point. One file must never contain two clocks: replay's parser
    would see time run backwards, and playback's auto-advance compares filename
    times to decide whether two recordings are contiguous.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

# The Pi's stamp, as written by nmea_capture.ts(): UTC, 'Z' on newer files.
STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})([.,]\d+)?(Z?)(\s)")

# Same tolerance the logger uses, so the two agree about what "a different clock"
# means. Anything beyond this is a clock step, not jitter.
CLOCK_STEP_TOLERANCE_S = 2.0

# Below this, a file is not worth rewriting: the stamps are already right.
CORRECTION_THRESHOLD_S = 120.0


def nmea_checksum_ok(sentence):
    """Reject corrupted sentences before letting them set a date (P40)."""
    if not sentence or sentence[0] not in "$!":
        return False
    star = sentence.rfind("*")
    if star < 1 or star + 3 > len(sentence.rstrip()):
        return False
    try:
        want = int(sentence[star + 1:star + 3], 16)
    except ValueError:
        return False
    got = 0
    for ch in sentence[1:star]:
        got ^= ord(ch)
    return got == want


def parse_rmc_utc(sentence):
    """Epoch seconds from a trustworthy $..RMC, else None."""
    if not sentence or "RMC" not in sentence[:8]:
        return None
    if not nmea_checksum_ok(sentence):
        return None
    p = sentence.split(",")
    if len(p) < 10 or p[2] != "A":
        return None
    hms, dmy = p[1], p[9]
    if len(hms) < 6 or len(dmy) != 6:
        return None
    try:
        return datetime(2000 + int(dmy[4:6]), int(dmy[2:4]), int(dmy[0:2]),
                        int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
                        tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse_stamp(line):
    """Epoch seconds from the Pi's own prefix, else None."""
    m = STAMP_RE.match(line)
    if not m:
        return None
    frac = (m.group(3) or ".0").replace(",", ".")
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp() + float(frac)


def shift_line(line, offset_s):
    """Rewrite a line's prefix by offset_s, preserving everything after it."""
    m = STAMP_RE.match(line)
    if not m:
        return line
    base = parse_stamp(line)
    if base is None:
        return line
    dt = datetime.fromtimestamp(base + offset_s, timezone.utc)
    # Always emit the explicit Z, even for pre-Z files: the corrected archive
    # should be self-describing throughout.
    return (dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
            + m.group(5) + line[m.end():])


def scan(path):
    """Measure the offset through a file.

    Returns (segments, stats) where segments is a list of
    (start_line_index, offset_s) — more than one means the clock stepped
    mid-file and the output must be split there.
    """
    segments = []
    current = None
    lines = 0
    fixes = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for idx, line in enumerate(fh):
            lines += 1
            if line.startswith("#"):
                continue
            gps = parse_rmc_utc(line[line.find("$"):].strip() if "$" in line else "")
            if gps is None:
                continue
            pi = parse_stamp(line)
            if pi is None:
                continue
            fixes += 1
            measured = gps - pi
            if current is None or abs(measured - current) > CLOCK_STEP_TOLERANCE_S:
                # A step. Record it, and anchor at this line.
                segments.append((idx, measured))
                current = measured
    return segments, {"lines": lines, "fixes": fixes}


def local_name(epoch):
    return datetime.fromtimestamp(epoch).strftime("nmea_%Y-%m-%d_%H%M%S.txt")


def first_stamp(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            t = parse_stamp(line)
            if t is not None:
                return t
    return None


def unique(path):
    """Never silently overwrite: an existing name means two sources collided."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def process(src, out_dir, apply_changes):
    segments, st = scan(src)
    base = os.path.basename(src)
    rec = {"source": base, "lines": st["lines"], "gps_fixes": st["fixes"], "outputs": []}

    if not segments:
        rec["status"] = "no-gps"
        if apply_changes:
            d = os.path.join(out_dir, "no-gps")
            os.makedirs(d, exist_ok=True)
            dest = unique(os.path.join(d, base))
            shutil.copy2(src, dest)
            rec["outputs"].append({"path": os.path.relpath(dest, out_dir), "offset_s": None})
        return rec

    offs = [o for _, o in segments]
    rec["status"] = "split" if len(segments) > 1 else (
        "shifted" if abs(offs[0]) > CORRECTION_THRESHOLD_S else "already-correct")
    rec["offsets_h"] = [round(o / 3600, 3) for o in offs]

    # Boundaries: segment i covers [start_i, start_{i+1}).
    bounds = [s for s, _ in segments] + [st["lines"]]
    # Everything before the first fix belongs to the first segment — it was
    # written under the same clock, we just had not measured it yet.
    bounds[0] = 0

    if not apply_changes:
        for i, (_, off) in enumerate(segments):
            rec["outputs"].append({"path": "(dry-run)", "offset_s": round(off, 3)})
        return rec

    os.makedirs(out_dir, exist_ok=True)
    header = [l for l in open(src, encoding="utf-8", errors="replace") if l.startswith("#")]

    written = 0
    for i, (_, off) in enumerate(segments):
        lo, hi = bounds[i], bounds[i + 1]
        body = []
        with open(src, "r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh):
                if idx < lo or idx >= hi:
                    continue
                if line.startswith("#"):
                    continue
                body.append(shift_line(line, off))
        if not body:
            continue
        anchor = parse_stamp(body[0])
        name = local_name(anchor if anchor is not None else 0)
        dest = unique(os.path.join(out_dir, name))
        with open(dest, "w", encoding="utf-8") as o:
            o.write(f"# corrected from {base} by tools/fix_log_times.py\n")
            o.write(f"# clock offset applied: {off / 3600:+.4f}h "
                    f"({off:+.1f}s) — GPS truth from $..RMC\n")
            if len(segments) > 1:
                o.write(f"# segment {i + 1} of {len(segments)} "
                        f"(source lines {lo}..{hi}) — the clock stepped mid-recording\n")
            for h in header:
                o.write("# original " + h.lstrip("# ").rstrip() + "\n")
            o.write("#\n")
            o.writelines(body)
        written += len(body)
        rec["outputs"].append({"path": os.path.basename(dest),
                               "offset_s": round(off, 3), "lines": len(body)})

    # Every data line must survive. Losing sentences to a "repair" would be far
    # worse than wrong filenames.
    data_in = st["lines"] - len(header)
    rec["lines_in"], rec["lines_out"] = data_in, written
    if written != data_in:
        rec["status"] = "LINE COUNT MISMATCH"
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="directory of nmea_*.txt recordings (never modified)")
    ap.add_argument("-o", "--out", help="output directory for corrected copies")
    ap.add_argument("--apply", action="store_true",
                    help="actually write corrected copies (default is a survey only)")
    a = ap.parse_args()

    if a.apply and not a.out:
        ap.error("--apply needs -o/--out; this tool never writes over its input")
    if a.out and os.path.abspath(a.out) == os.path.abspath(a.src):
        ap.error("output directory must differ from the source")

    files = sorted(f for f in os.listdir(a.src) if f.startswith("nmea_") and f.endswith(".txt"))
    if not files:
        print(f"no nmea_*.txt in {a.src}", file=sys.stderr)
        return 1

    print(f"{'FILE':34} {'STATUS':16} OFFSET(h)")
    records = []
    for f in files:
        rec = process(os.path.join(a.src, f), a.out, a.apply)
        records.append(rec)
        offs = ",".join(f"{o:+.2f}" for o in rec.get("offsets_h", [])) or "-"
        print(f"{rec['source']:34} {rec['status']:16} {offs}")

    bad = [r for r in records if r["status"] == "LINE COUNT MISMATCH"]
    counts = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    if a.apply:
        man = os.path.join(a.out, "manifest.json")
        with open(man, "w", encoding="utf-8") as fh:
            json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                       "source_dir": os.path.abspath(a.src),
                       "records": records}, fh, indent=2)
        print(f"manifest: {man}")
        print(f"originals untouched in {a.src}")
    else:
        print("\nsurvey only — pass -o OUT_DIR --apply to write corrected copies")

    if bad:
        print(f"\n{len(bad)} FILES LOST LINES — do not trust this output", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
