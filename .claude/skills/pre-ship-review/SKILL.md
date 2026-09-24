---
name: pre-ship-review
description: Pre-ship review gate for AIS Tracker. A deterministic smoke prepass, then two zero-context review passes armed with the repo's pitfall index, then adversarial verification. Use before shipping material changes to the Pi proxy, data-loader, Service Worker, offline pre-fetch, or the data pipeline.
---

# Pre-ship review — AIS Tracker

Repo-local replacement for bare `5-pass-review`. **Rebuilt 2026-09-24 after a measured five-pass
run**, which produced this split on the vessel-name database change:

| Pass | Findings | Outcome |
|---|---|---|
| 1 logic | 11 | the top 4 were real ship-blockers |
| 3 readability | 15 | 5 mechanical and real; the rest judgment |
| 4 perf/security | 8 | same top 4, found independently |
| 2 docs, 5 tests | — | **died to API timeouts, and their absence cost nothing** |

**Three passes independently found the same four defects.** Everything worth acting on arrived with a
measurement attached. And the single worst finding — an untracked generated file inside `sw.js`'s
atomic `cache.addAll()`, which would have silently destroyed all offline capability — was reachable
by one command:

```bash
git ls-files --error-unmatch static/<each ASSETS entry>
```

That cost ~270k tokens of review agents to find. It now costs 5 seconds, with no LLM, and it caught a
**second instance in the same change** that the agents had missed.

So the gate is: **script first, two passes, measurement as the admission ticket.**

**You are the orchestrator. You do not review the code yourself.** You run the script, dispatch,
verify, present, and apply only what was approved.

---

## Step 0 — scope

Required for material changes to `pi/boat_server.py`, `static/js/data-loader.js`, `static/sw.js`,
`static/js/app.js`'s config bootstrap / tile selection / offline pre-fetch, `download_offline.py`, or
the `.github/workflows/` data pipeline and `test` job.

For the router, `node tests/test_route.mjs` catches more, faster. For anything else, say it's out of
scope and stop. A gate that runs on everything gets ignored.

## Step 1 — the smoke prepass. Always. Before any reviewer.

```bash
.claude/skills/pre-ship-review/bin/smoke.sh          # or: smoke.sh "git diff HEAD"
```

~5 seconds, no LLM. It checks the seven things this repo has actually shipped broken:

1. **Source files are text to `grep`** — a raw NUL in a regex made two files binary, so their `P19`
   citations were invisible to the discovery command CLAUDE.md prescribes.
2. **`sw.js` `ASSETS` exist *and are tracked in git*** (`P43`). On disk is not enough; a deploy is a
   fresh checkout.
3. **Diverged copies agree** (`P20`) — the two `cleanName` regexes, the disk thresholds mirrored in
   `hub.html`. Values are *evaluated*, not pattern-matched, because a check that cries wolf gets
   ignored.
4. **Everything parses** — `node --check`, `py_compile`, `bash -n`, JSON.
5. **Every test suite passes**, with `__pycache__` cleared first (`P21`).
6. **Every suite is wired into CI** (`P22`).
7. **Doc cross-references resolve**, and the newest `[Pnn]` is indexed in CLAUDE.md.

**`SMOKE_FINDINGS` must be 0 before you dispatch anyone.** Fix what it found first — reviewers
should never spend tokens on this class.

Each check exists because of a specific incident. Add one when a review finds a mechanical defect;
that is how the gate gets cheaper over time.

## Step 2 — establish the diff range explicitly

```bash
git status --short
git log --oneline origin/main..HEAD | cat
git diff --stat <range> | tail -3
```

**State the range in one line before dispatching.** Uncommitted work means `git diff <merge-base>`
— base to **working tree** — and agents must be told untracked files are in scope. Over ~2000 lines,
split by subsystem rather than dispatching agents who will skim.

## Step 3 — two passes, in parallel, read-only

One message, two `Agent` calls, `subagent_type: general-purpose`.

| Pass | Lens | Pitfall groups to inject |
|---|---|---|
| A | Logic, edge cases, async/state, races | the groups matching the changed files |
| B | Resilience at sea, error handling, security, performance | "Pi reverse proxy", "browser bootstrap" |

**There is no readability pass.** That is `/simplify`'s job, and in the measured run it produced the
most findings with the lowest action rate — plus it is where re-litigation of solved pitfalls comes
from. Its genuinely valuable findings were all mechanical, and mechanical checks belong in Step 1.

**No docs pass and no tests pass either.** Step 1 covers doc links and CI wiring. Ask Pass A to
mutation-test any new suite as part of its remit instead.

Cap each pass at **5 findings**, ranked. Tell them explicitly:

> **A finding needs a measurement you produced.** A claim with no evidence — no command run, no line
> read, no failure reproduced — is not a finding. Say "no issues found" rather than padding.

For pitfalls, give the agent the exact extraction command for *its* IDs rather than pasting the text
(which costs the orchestrator's context) and rather than telling it to read the file (it will read
all of it):

```bash
for id in P13 P16 P19; do
  awk -v id="[$id]" 'index($0,id)&&/^## /{f=1;print;next} f&&/^## /{exit} f' docs/pitfalls.md
done
```

Prompt template — fill `{PASS}`, `{LENS}`, `{RANGE}`, `{IDS}`, `{TARGETS}`:

```
You are performing Pass {PASS} of a 2-pass pre-ship review on the AIS Tracker repo.

## Lens
{LENS}

## Diff range
{RANGE}   (untracked files listed there ARE in scope)
Working directory: /Users/peterrostas/Projects/AIS Tracker

Read CLAUDE.md first. This is a navigation aid used offshore: the characteristic failure is
data that LOOKS live and isn't, so weight silent degradation far above cosmetic issues.

A deterministic smoke script has already run and reports 0 findings. Do NOT re-check syntax,
test pass/fail, CI wiring, doc links, precache manifests, or binary-vs-text. That class is
covered; spend your effort where a script cannot reach.

## Already-solved traps — do NOT re-propose these
Extract your area's entries FIRST, before forming any hypothesis. Do not read
docs/pitfalls.md whole; it is a lookup table.
  for id in {IDS}; do
    awk -v id="[$id]" 'index($0,id)&&/^## /{f=1;print;next} f&&/^## /{exit} f' docs/pitfalls.md
  done
If you think one is wrong, say so explicitly with the failure case. Do not quietly suggest
reverting it.

## Worth attacking specifically
{TARGETS}

## Rules
- READ-ONLY. Do not edit, create or delete any file, or run git commands that mutate state.
  You MAY run the test suites to observe behaviour.
- AT MOST 5 findings, most severe first. Fewer is better.
- A FINDING NEEDS A MEASUREMENT YOU PRODUCED: the command you ran, the line you read, the
  failure you reproduced. A claim you could not evidence is not a finding — drop it.
- For each: file:line, what breaks, a concrete failure scenario (inputs -> wrong output),
  and a specific fix.
- "No issues found" is a valid and useful answer.
- Do not reproduce secret values; name the file instead.

## Output
A numbered list of findings, then one sentence summarising the pass.
```

## Step 4 — confirm nothing was written

```bash
git status --short
```

Compare against Step 2. If a reviewer mutated the tree, show the diff and ask — never
`git checkout -- .` unprompted; it destroys real unstaged work.

## Step 5 — verify before the user sees anything

Deduplicate first, noting which passes agreed. **Agreement across both passes is strong evidence;
promote those.**

Then triage by kind, because the two kinds have very different confirm rates:

- **Mechanical** (a file is missing, two literals differ, a guard is absent, a path is unreachable) —
  verify it **yourself** with one command. Faster and more reliable than a refuter agent.
- **Judgment** (this is confusing, this could be slow, this might race) — dispatch at most **3**
  refuters total, for the ones that would change the code most:

```
Try to REFUTE this claimed defect. Default to refuted=true when uncertain; a false finding
sends a human to change working navigation code, which costs more than a missed one.

Claim: {finding}   File: {file}:{line}   Claimed failure: {scenario}

Read the actual code. Then: does the described input reach this path? Is the claimed
behaviour what the code does, or what it looks like it does? Is it already handled by a
caller, a guard, a test, or a documented pitfall? Is it reachable in either real deployment
(GitHub Pages web mode / the Pi at sea)?

Return: {refuted: bool, reasoning: one paragraph, confidence: low|medium|high}
```

Drop refuted findings. Mark survivors `CONFIRMED` (you or a refuter proved it) or `PLAUSIBLE`
(unsettled). **Report how many were dropped** — that number is how you know this step works.

## Step 6 — present and get approval

Numbered list, most severe first, then `AskUserQuestion` with `multiSelect: true`. Group by
subsystem if there are more than 4. **Never imply a `PLAUSIBLE` finding is verified.**

## Step 7 — apply only what was approved

Partition fix agents by file so no two can collide: `pi/*` + root `*.py`/`*.sh` · `static/**` ·
`CLAUDE.md`/`docs/**`/`.github/**`. Reconcile docs **after** the code agents finish — a doc agent
running alongside a code agent documents behaviour that is still changing.

Then verify for real and report the actual output:

```bash
.claude/skills/pre-ship-review/bin/smoke.sh
```

If a fix touched rendering, load it in a browser per CLAUDE.md's mandatory visual rule — and prove
the fix, don't just observe the absence of a symptom. For the injection finding that meant disabling
the escaper in the live page and counting the injected attributes: 1 with it off, 0 with it on.
**If you cannot verify, say the change is unverified** rather than implying otherwise.

## Step 8 — close the loop

- A confirmed finding that revealed a *new* trap gets a `docs/pitfalls.md` entry with the next
  `[Pnn]` and a one-line CLAUDE.md index entry.
- **If the finding was mechanical, add a check to `bin/smoke.sh`.** This is the step that makes the
  gate cheaper next time, and it is the whole point of the rebuild.
- If a fix changed documented behaviour, update the doc too.
- Report: smoke findings, passes run, findings raised, findings dropped, fixes applied, and what you
  could not verify.

Do **not** also run `autoplan`, `review`, `devex-review`, `health`, or `cso`. This is the gate.
