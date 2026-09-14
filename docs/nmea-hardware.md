# NMEA receiver hardware — the Lantronix XPort

Operating notes for the box that feeds `/nmea`. The code trap it causes is
[`P40`](pitfalls.md); this file is the *device* half — how to identify it, probe it, and change
its transport without bricking a race.

## What the receiver actually is

`192.168.47.10:10110` is **not** the SI-TEX MDA-5's own network stack. It is a separate
**Lantronix XPort** serial-to-Ethernet module with **its own power switch**, wired to the MDA-5
over RS-232 at **460800 baud, 8-N-1, hardware CTS/RTS**.

That distinction costs a night if you miss it. The XPort answers ARP, ICMP, its web UI on `:80`
and its setup menu on `:9999` *while serving no data at all* — its network side is powered by a
different thing than its data side. So:

> **"The AIS unit is switched on" is not evidence about the feed.** Neither is a ping reply.

Both boxes have to be on, and they are separate switches.

## Why a refusal tells you nothing

The XPort serves TCP to **one client at a time** and answers every other connection with
`ECONNREFUSED`. A refusal therefore means *either* "another client holds the slot" *or* "the box
isn't listening" — and nothing observable from outside separates them. Its SNMP agent implements
neither `tcpConnTable` nor the Lantronix private MIB, so you cannot ask it who holds the slot.

This is why the Pi now also listens on UDP: datagram mode has no session and no slot, so no
client's power state can wedge the feed. See `P40`.

## Probing it without being lied to

Tooling misreports this device badly. Verified on this Mac, 2026-09-14:

| Tool | What it did |
|---|---|
| `nc` | Reported a **known-open** port as closed. Do not trust it. |
| `nmap` | Reported every port `filtered` — the agent sandbox denies raw sockets. |
| Claude Code `Bash` | Needs `dangerouslyDisableSandbox` for ICMP/TCP to the boat LAN at all; port 22 is blocked even then. HTTP to the Pi works by hostname (`typonrpi4.local`) but is 403'd by IP. |

Probe with **raw Python sockets and print the real `errno`**. `ECONNREFUSED` is 111 on the Pi
(Linux) and 61 on macOS — the same condition, different numbers.

The XPort has **no RTC**, so its HTTP `Date:` header is meaningless. Do not use it to timestamp
evidence; that is the same trap as `P33` on the Pi.

## Reading the configuration is not free

Connecting to TCP `:9999` and pressing Enter enters Setup Mode, which prints the whole current
configuration. **Exiting Setup Mode reboots the unit.** Never do it while recording.

The web UI on `:80` exposes the same settings as a frameset, and `secure/setuprec.xml` is a
complete machine-readable config record — take that as a backup *before* changing anything, so a
revert is a restore rather than a reconstruction.

Decode Connect Mode from the device's own `secure/connset.js`, never from memory. The mapping is
`curropt & 0xc0`:

| Value | "Accept Incoming" |
|---|---|
| `0xc0` | Yes — unconditional |
| `0x00` | No |
| `0x40` | Only while the attached device asserts modem-control-in |

As configured, ours is `C0`. **So a refusal is not the MDA-5 being powered off** — the XPort
accepts TCP regardless of the serial side. Do not reason backwards from "nav was off" to "that
explains the refusal".

`xprtproto` is a single **TCP or UDP** choice, never both. That is what makes running both
listeners on the Pi safe: only one can ever deliver, so there is no double-counting, and a
reverted config keeps working with no redeploy. It also means the two must stay on the **same
port** — flipping the protocol must not also mean changing a port, or the change lands on a closed
door and looks exactly like a dead receiver.

## Credentials and remote administration

The unit is administrable over the network. The specifics — its MAC, its authentication state and
the exact config-dump URL — are deliberately **not recorded in this repo**, which is public. They
are in the maintainer's local agent memory and readable from the device itself in under a minute
using the sections above.

If you are hardening this: the management ports (`:80`, `:9999`, `30718`, SNMP) are all reachable
from anything on the boat LAN, and the Pi joins marina and hotspot WiFi. That is worth a firewall
rule, and it is not one today.
