---
name: pre-ship-review
description: Pre-ship review gate for AIS Tracker. Five zero-context review passes over an explicit diff range, each armed with the repo's pitfall index, followed by adversarial verification of every finding before it reaches the user. Use before shipping material changes to the Pi proxy, data-loader, Service Worker, offline pre-fetch, or the data pipeline.
---

# Pre-ship review — AIS Tracker

Repo-local replacement for bare `5-pass-review`. Keeps that skill's one real insight — five passes in
zero-context subagents, so each gets genuinely fresh eyes — and fixes four things measured on a real
run of it in this repo on 2026-08-26:

1. **The diff range defaulted to something nobody chose.** On `boat-mode` the default range was a
   merge commit; the honest scope (base → working tree, including uncommitted edits) had to be
   hand-derived. Step 1 now establishes it explicitly and prints it for confirmation.
2. **Reviewers had no pitfall index**, so nothing stopped five agents re-proposing solved problems —
   the staleness gate, the sweep's 404 semantics, the tack penalty. Step 2 injects the relevant
   `[Pnn]` group into every pass.
3. **"Read-only" reviewers were dispatched with write access** and the orchestrator noticed after the
   fact. Step 2 makes that explicit and Step 3 verifies it.
4. **Findings reached the approval list unverified.** 19 findings were presented and all 19 approved
   without any of them being adversarially checked first. Step 4 now refutes each one before you ever
   see it.

**You are the orchestrator. You do not review the code yourself.** You dispatch, verify, present,
and then apply only what was approved.

---

## Step 0 — is this change in scope?

Required for material changes to:

- `pi/boat_server.py` (the proxy, cache, or pre-warm loops)
- `static/js/data-loader.js` (fetchers, TTLs, the staleness gate)
- `static/sw.js`
- `static/js/app.js`'s config bootstrap, tile-layer selection, or offline pre-fetch
- `download_offline.py`
- `.github/workflows/` data pipeline or the `test` job

Optional but useful for the router (`router.js`, `route-worker.js`) — though
`node tests/test_route.mjs` catches more there, faster. For anything else, say it's out of scope and
stop; a gate that runs on everything gets ignored.

## Step 1 — establish the diff range explicitly

```bash
cd "$(git rev-parse --show-toplevel)"
git status --short
git log --oneline origin/main..HEAD | cat
```

Decide the range and **state it in one line before dispatching**. Prefer, in order:

1. If the work is uncommitted: `git diff <merge-base>` — base → **working tree**. Say so; agents must
   know uncommitted edits are in scope.
2. If committed on a branch: `git diff <merge-base>..HEAD`.
3. Never silently review a whole long-lived branch. If the range exceeds ~2000 lines, split by
   subsystem and run the gate per subsystem rather than dispatching agents that will skim.

Print `git diff --stat <range> | tail -3` and confirm the size is what you intended.

## Step 2 — dispatch 5 passes in parallel, each armed with pitfalls

One message, five `Agent` calls, `subagent_type: general-purpose`, all read-only.

| Pass | Lens | Pitfall groups to inject |
|---|---|---|
| 1 | Logic correctness, edge cases, async/state, races | the groups matching the changed files |
| 2 | Repo pattern alignment, and **doc accuracy** | "git, ports, deploys" + the coverage matrix |
| 3 | Readability, naming, structure, prose creep in docs | — |
| 4 | Performance, resilience at sea, error handling, security | "Pi reverse proxy", "browser bootstrap" |
| 5 | Tests, regression risk, CI coverage, reviewer blockers | "JS ↔ Python mirror, tests and CI" |

For each pass, resolve the pitfall IDs first from CLAUDE.md's grouped index, then extract those
entries and **paste them into the prompt** — do not tell the agent to go read the file, it will read
the whole thing:

```bash
awk 'index($0,"[P06]")&&/^## /{f=1;print;next} f&&/^## /{exit} f' docs/pitfalls.md
```

Prompt template — fill `{PASS}`, `{LENS}`, `{RANGE}`, `{PITFALLS}`:

```
You are performing Pass {PASS} of a 5-pass pre-ship review on the AIS Tracker repo.

## Lens
{LENS}

## Diff range
{RANGE}
Working directory: /Users/peterrostas/Projects/AIS Tracker

Read CLAUDE.md first. This is a navigation aid used offshore: the characteristic failure is
data that LOOKS live and isn't, so weight silent degradation far above cosmetic issues.

## Already-solved traps — do NOT re-propose these
{PITFALLS}
If you believe one of these is wrong, say so explicitly and give the failure case. Do not
quietly recommend reverting it.

## Rules
- READ-ONLY. Do not edit, create or delete any file. Do not run git commands that mutate state.
- Read the diff, then the changed files in full, plus call sites and adjacent code.
- Treat the implementation as complete but untrusted. Try hard to break it.
- Follow existing repo conventions; do not introduce new abstractions or patterns.
- For each finding give: file:line, what breaks, a concrete failure scenario (inputs → wrong
  output), and a specific fix.
- Be honest. "No issues found" is a valid and useful answer.
- Do not reproduce secret values if you encounter any; name the file instead.

## Output
A numbered list of findings, then one sentence summarizing the pass.
```

## Step 3 — confirm nothing was written

```bash
git status --short
```

Compare against the Step 1 output. If a reviewer mutated the tree, show the diff and ask before
touching it — never `git checkout -- .` unprompted, it can destroy real unstaged work.

## Step 4 — verify every finding BEFORE the user sees it

This is the step bare `5-pass-review` skips, and the reason it produced 19 unverified findings.

Deduplicate first: consolidate findings multiple passes reported, noting which passes agreed.

Then, for each surviving finding, dispatch a refuter — in parallel, one per finding:

```
Try to REFUTE this claimed defect in the AIS Tracker repo. Default to refuted=true when
uncertain; the cost of a false finding is higher than a missed one here, because it sends a
human to change working navigation code.

Claim: {finding}
File: {file}:{line}
Claimed failure: {scenario}

Read the actual code. Then decide:
- Does the described input actually reach this code path?
- Is the claimed behavior what the code really does, or what it looks like it does?
- Is it already handled elsewhere (a caller, a guard, a test, a documented pitfall)?
- Is the failure reachable in either real deployment (GitHub Pages web mode / the Pi at sea)?

Return: {refuted: bool, reasoning: one paragraph, confidence: low|medium|high}
```

For a finding in a data or offline path, prefer three refuters with distinct lenses (does-it-reproduce
/ is-it-already-guarded / does-it-matter-at-sea) and kill it on a majority.

Drop refuted findings. Mark the survivors `CONFIRMED`, and anything the refuter couldn't settle
`PLAUSIBLE`. Report how many were dropped — that number is how you know this step is working.

## Step 5 — present, and get approval

Present the verified findings as a numbered list, most severe first, then use `AskUserQuestion`
(`multiSelect: true`) to let the user choose. Group into at most 4 questions × 4 options; if there are
more findings than that, group by subsystem, and say what each group contains.

Never imply a finding is verified when it is only `PLAUSIBLE`. Say which is which.

## Step 6 — apply only what was approved

Dispatch fix agents **partitioned by file** so no two can touch the same file. A workable split for
this repo:

- `pi/*` + root `*.py` + `*.sh`
- `static/**`
- `CLAUDE.md`, `USER_GUIDE.md`, `docs/**`, `.gitignore`, `.github/**`

Warn each agent that siblings are editing concurrently and that editing outside its partition
corrupts their work. Note that a docs agent running beside a code agent will document behavior the
code agent is still changing — reconcile the docs *after* the code agents finish, or accept that you
must re-audit the docs yourself at the end. (That happened on 2026-08-26: the doc pass described
cache semantics the fix pass had already replaced.)

Then verify for real, and report the actual output:

```bash
node tests/test_physics.mjs && node tests/test_staleness.mjs && python3 tests/test_boat_server.py
python3 -m py_compile pi/boat_server.py nmea_capture.py nmea_ws_proxy.py download_offline.py
bash -n start_boat.sh pi/startup.sh pi/test_boat_server.sh
for f in static/js/*.js static/sw.js; do node --check "$f" || echo "FAIL $f"; done
```

If a fix touched rendering, load it in a browser per CLAUDE.md's mandatory visual rule. If you can't,
**say the change is unverified** rather than implying otherwise.

## Step 7 — close the loop

- Any confirmed finding that revealed a *new* trap gets a `docs/pitfalls.md` entry with the next
  `[Pnn]` and a one-line index entry in CLAUDE.md. This is what stops the next review re-finding it.
- If a fix changed documented behavior, update the doc, not just the code.
- Report: passes run, findings raised, findings refuted and dropped, fixes applied, what you could
  not verify.

Do **not** also run `autoplan`, `review`, `devex-review`, `health`, or `cso`. This is the gate.
