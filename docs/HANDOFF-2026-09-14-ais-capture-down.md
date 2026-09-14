# HANDOFF — AIS capture down, 2026-09-14 ~00:40 PDT

Written at the end of a long session. **Read this before touching anything.**

---

## 1. Symptom

NMEA/AIS capture has recorded nothing since **2026-09-13 23:21 PDT**. Before that it worked all
day. The Pi and all its software are running normally.

```
GET http://typonrpi4.local:8080/api/health
  recording : false
  nmea      : { source: "192.168.47.10:10110", lines: 0, last_line_age_s: null }
  logger    : running (nmea_capture.py, alive, connected to the WebSocket)
  newest_log: nmea_2026-09-13_230656.txt  5,046,272 bytes, 3112s old (frozen)
  boot_log  : "TCP connection failed: [Errno 111] Connect call failed
               ('192.168.47.10', 10110), retrying in 5s"   x40 (still, continuously)
```

Errno 111 = ECONNREFUSED. The Pi asks the AIS receiver for data every 5 seconds and is refused
every time.

## 2. The decisive evidence

The same device, same port, behaves **differently depending on who connects**:

| Client | Source IP | Result |
|---|---|---|
| Pi (`boat_server.py`) | 192.168.47.231 | **ECONNREFUSED**, continuously, for ~50+ min |
| Peter's Mac (`nc 192.168.47.10 10110 \| head -5`) | 192.168.47.112 | Connects, receives **nothing**, connection closed immediately |

Also established:
- `192.168.47.10` answers ARP (`0:80:a3:c9:f3:f9`) — powered, on the LAN.
- The AIS unit is switched on. Owner confirms. A maritime device that is on is transmitting.
- A stationary boat is **not** a cause: earlier logs at the dock show `$HCHDG`, `$YXXDR`,
  `$GPRMC`, `$IIVDR` at 10 Hz with `SOG 000.0`. Being tied up does not stop the feed.
- No code deployed tonight touches the TCP connect path. ECONNREFUSED happens before any of it.

## 3. Timeline (this is the important part)

| Time (PDT) | Event |
|---|---|
| → 22:50 | Recording normally |
| 22:50 | **Pi power-cut #1** |
| 22:50–23:06 | Refused (16 min) |
| 23:06 | Recovered on its own → 15 min of good data |
| ~23:21 | **Pi power-cut #2** |
| 23:21 → now (~00:40) | Refused continuously, ~80 min, has NOT self-recovered |

Four Pi power-cycles happened tonight, on my instruction, while I was chasing a different theory.
Recording broke after them, not before.

## 4. Root cause analysis

### Proven
1. The receiver refuses connections **from the Pi's IP** while accepting them from another IP on
   the same LAN.
2. It sends no data to the connection it does accept, and closes it at once.
3. This began immediately after an abrupt Pi power-cut, and recovered once, unaided, after 16
   minutes — the signature of a timeout expiring.

### Best explanation (consistent with all of the above, NOT directly measured)
The MDA-5 serves NMEA over TCP to **one client at a time** and is holding a **dead session** it
believes still belongs to the Pi.

When the Pi loses power abruptly it never sends a FIN or RST, so the receiver's session stays open
until its own idle timeout expires. Meanwhile:
- a new connection **from the Pi's IP** looks like a duplicate → refused;
- a connection **from a different IP** is accepted but gets no data, because the single output slot
  is still assigned to the phantom session → immediate close.

That explains the asymmetry, the 16-minute self-recovery, and why it recurred after each power cut.

### Not yet ruled out
- Some other client on the boat LAN holding the slot (a chartplotter, a phone app). Nothing was
  checked for this.
- A fault internal to the MDA-5. It has prior history: its internal VDR microSD once hit a firmware
  limit and crash-looped the unit (see `sitex_mda5_ais_fault`). This could be a recurrence.
- An IP allowlist or single-peer binding configured on the unit.

### What could not be tested, and why
Raw TCP from this machine is blocked by the Claude Code sandbox
(`nc: connectx ... Operation not permitted`), so port 10110 could never be probed directly from
the agent side. Everything above is inferred from the Pi's own errno plus one manual `nc` run.
**Getting SSH onto the Pi removes this blindness** — `ss -tnp | grep 10110` on the Pi would show
exactly who holds the connection, and settle section 4 in one command.

## 5. The design flaw this exposes

> "This MUST work so that I can restart the Pi any time. I need to shut it down when not at the
> boat and back when I want to track."

That is a hard requirement and **the current design violates it.** A single-client TCP feed means
every abrupt Pi power-off can orphan a session on the receiver and lock the Pi out for an unbounded
period. Nothing in our code can fix that from the Pi side after the Pi is already dead.

Options, best first:

1. **Switch the feed to UDP.** UDP is connectionless — there is no session to orphan, so pulling
   the Pi's power can never lock anything out. Many AIS units, including SI-TEX, can output NMEA
   over UDP broadcast. `ais_diagnose.py` already has a `--udp` mode, which suggests this was
   considered before. **`boat_server.py` and `nmea_ws_proxy.py` are TCP-only today** — adding a UDP
   listener is a real but contained change. This is the correct fix for the stated requirement.
2. **Shorten the receiver's TCP idle timeout** in the MDA-5's own configuration, if exposed. Turns
   an unbounded lockout into a bounded one. Does not eliminate it.
3. **Always shut the Pi down cleanly** (`sudo systemctl stop ais-tracker`, or `sudo poweroff`) so
   the socket closes properly. Correct, but it depends on SSH working and contradicts "cut the
   power whenever I like".
4. **Have the Pi bind its NMEA socket with `SO_REUSEADDR` + TCP keepalive.** Helps the Pi notice a
   dead peer; does nothing about the receiver holding a session after the Pi vanishes.

## 6. Immediate next actions

1. **Power-cycle the MDA-5 / AIS receiver** — not the Pi. Off, ten seconds, on. Clears its session
   table. The Pi retries every 5 s and will reconnect on its own within seconds. This is the
   fastest route back to recording.
2. **Verify SSH.** Never actually attempted — I asked for it late and it got lost in the noise.
   From a normal Terminal (the sandbox blocks port 22, so it cannot be done from inside a session):
   ```
   ssh rostape1@TyponRpi4.local
   ```
   The key install ran during the last boot, so this *should* work with no password. If it does,
   `journalctl -u ais-tracker` and `ss -tnp` become available and the guesswork ends.
3. **Then investigate the receiver properly**: does it have a web UI? Does it support UDP output?
   What is its TCP session timeout? Is anything else on the boat connected to it?
4. **Design the UDP path** (option 1 above) so the power-cycle requirement is actually met.

## 7. What was shipped tonight, and is live on the Pi

All of this is on `boat-mode` and running. None of it is implicated in the outage.

| Commit | What |
|---|---|
| `7effaed` | Logs never deleted; monotonic uptime (`P33`); disk-space alert; ENOSPC reported as a disk fault, not "Disconnected" |
| `9a31c65` | `/hub`, `/api/logs`, race playback, PDT display with `Z`-suffixed UTC storage |
| `e1e02ed` | `loadUrl` timeout + race guard, mobile transport positioning, several dead-state fixes |
| `5bcc421` | Logger supervision, `/api/health`, SSH key installed from `pi/authorized_keys.pub` |
| `3c3cd3e` | `/api/health` reports the NMEA source, line count and last-line age |
| `4c82ff3` | `P38`, `P39` documented |

Verified working on the Pi: `/hub`, `/api/logs`, `Modified (PDT)` in the log listing, and
`Z`-suffixed UTC timestamps in newly written logs. All five test suites pass locally.

## 8. Where I went wrong — read this to avoid repeating it

- **I told the owner to power-cycle the Pi four times.** Each abrupt cut most likely orphaned a
  session on the receiver, i.e. my instructions caused the recurrences of the very fault I was
  chasing. `systemctl restart` over SSH would have closed the socket cleanly.
- **I diagnosed "the logger crashed" from `:8081` being down and the log file frozen.** It had not
  crashed. Both symptoms are equally consistent with a healthy logger and a silent feed. I built
  supervision for a problem that did not exist (still worth having, but it was not the fix). Filed
  as `P39`.
- **I said the SSH key was installed after a reboot when it was not.** `startup.sh` git-pulls
  itself, and bash reads scripts incrementally, so the running instance kept executing the old
  version — shell changes land one boot late. Filed as `P38`.
- **I said "the AIS isn't sending."** Wrong, and the owner corrected me. ECONNREFUSED means a
  connection was refused, not that the device is silent.
- **I never asked for the one five-second test that mattered** (`nc` from the Mac) until very late,
  and never confirmed SSH at all.
- **I flagged a red herring**: the frozen log's size of exactly 8,388,608 bytes. That file ran
  22:00→22:50, and 50 minutes at ~10 MB/h is ~8.3 MB. Arithmetic, not truncation.

## 9. Unrelated but still open

- `docs/logging-and-playback.md` — replay shows **today's** tides/currents/wind, so environmental
  layers are switched off during replay with a banner. Two routes to fixing it properly are written
  up there, including that **`$IIVDR` already carries measured set and drift once a second in
  every log** and `nmea-parser.js` has no case for it.
- A five-agent `/pre-ship-review` sweep was planned for 2026-09-14. Pass 5 (tests/CI) has never
  run — it died on an API error — so no independent review has audited the new test suites.
- `static/js/app.js` is ~3,300 lines; the replay transport block is a candidate for extraction into
  `static/js/replay-transport.js`.
- Peter's SSH password for the Pi is **lost** — not in the macOS keychain, not in the Passwords app.
  Key-based access is now the only route.
- `~/Documents/typon-nmea-backup-2026-09-13/` is the **only** copy of 27 April/May recordings that
  the old retention sweep deleted. It needs a second copy.
