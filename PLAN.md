# Ultibot — Master Plan (v5 — LOCKED)

> An AI game player: you build a **decision tree** of simple boolean questions
> that decides which images the bot looks for, in what order, and what it does
> when it finds them. Faster than a human, but *reasonable* — hard safety
> rails, and **a contingency at every question**: every `if` has an `else`,
> and the default else is *"go up one level and do something harmlessly
> random."*

**Status:** LOCKED PLAN — approved by the user. This document is the source
of truth; changes require an explicit decision, not drift.

**Design philosophy (locked):** *Robustness is the foundation of functionality.*

**Honesty principle (locked):** Ultibot is a learning/research tool. If it's
ever used where other players are involved, we're upfront that it's a bot and
we play by the game's rules (ToS, bot-allowed servers, custom lobbies). The
Safety Shell governs what the bot *does*; honesty governs *where* it does it.

**Aspiration (locked):** the end goal is a bot that behaves *more* ethically
and reasonably than the average human player — a standard-bearer. See §4a.

---

## 1. Core Concept

**Eyes → Brain → Hands**, wrapped in a **Safety Shell**:

1. **Eyes** capture the screen, find template images, read OCR stat sensors,
   (later) localize you on a learned map.
2. **Brain** walks your **decision tree** from the root, every tick (~20×/sec).
3. **Hands** execute actions — every step can *verify its result* and retry.
4. **Safety Shell** mediates everything: kill switch, rate limits, cooldowns,
   human-like jitter, audit log.

The Brain is pluggable: default driver is your tree; an **AI driver** (§7) can
supervise it later.

---

## 2. The Decision Tree (locked semantics)

```
GAME (root node — one profile per game you're "training")
│
├── [0] &failsafe-random            ← FIRST ENTRY of every level (see below)
│
├── QUESTION: "am I in a menu?"
│   ├── TRUE  → &menu-navigation
│   └── FALSE → (not written? default: bubble up → run failsafe-random)
│
├── QUESTION: "is my health low?"
│   ├── TRUE  → &emergency-survival
│   └── FALSE → continue down
│
└── QUESTION: "is there loot?"
    ├── TRUE  → &looting
    └── FALSE → &idle-wander
```

### Locked decisions (v4)

- **Tick semantics:** predictable — the first action group that actually fires
  **stops the walk** for that tick. Nodes may opt into `then: continue`.
- **Contingency at every question:** every question node semantically *has* an
  else. If the author didn't write one, the **default else** fires:
  **bubble up one tree level and execute that level's first entry.**
- **The failsafe convention:** the **first entry at every tree level is a
  random-behavior group** (conventionally `&failsafe-random`). The validator
  *enforces* this — a level whose first entry isn't a failsafe group fails to
  load. This guarantees the default else always lands somewhere safe: when the
  tree has no answer, the bot fidgets — wanders, presses a random key, maybe
  checks its inventory — instead of freezing or spamming. Per-level failsafes
  are allowed (a menu level can fidget differently than the world level).
- **Randomness is also a tool, not just a fallback:** arbitrary walk / random
  key presses are first-class actions (needed e.g. for object interactions:
  walk up to the thing, mash use), usable anywhere in the tree.

```jsonc
// the failsafe group is just a normal group built from random primitives —
// fully customizable per game:
"failsafe-random": [
  { "type": "choose", "options": [
    { "weight": 4, "actions": [
        { "type": "random_key", "keys": ["w","a","s","d"],
          "hold_ms": [300, 900] } ] },
    { "weight": 1, "actions": [{ "$ref": "check-inventory" }] },
    { "weight": 2, "actions": [{ "type": "wait", "ms": [400, 1200] }] }
  ]}
]
```

### Conditions (leaf vocabulary, combinable with `and`/`or`/`not`)
`image` (confidence, variants, region, anchor-relative) · `stat` (OCR sensor
comparisons) · `pixel` · `cooldown_ready` · `timer` · `once` · `chance(p)` ·
*(Phase 7)* `at(place)`, `near(place)`

### Actions (vocabulary)
`click`/`right_click`/`double_click`/`move_to` (target: match, anchor+offset,
fixed point) · `key` · `key_hold` · `type` · `wait([min,max])` ·
**`random_key`** (random pick from a key set, random hold duration — covers
"arbitrarily walk" in WASD games and "press a random key") ·
**`random_mouse`** (small aimless wander of the cursor) · `run_macro(name)` ·
`choose` (weighted random pick among action lists) · **`veto(groups, reason)`**
(suppress groups — the Conduct Layer's voice, §4a) · `chance` gate on any
action or node · every step supports `verify:` + `retry:` + `on_fail:`
(**contingency at the action level**).

### Macros
Named verified sequences (menu navigation etc.): click → confirm image →
next step, retry with backoff, `on_fail` fallback.

---

## 3. Robustness Toolkit (locked)

1. **Anchor-relative coordinates** — positions relative to a found anchor
   image; window moves don't break anything.
2. **Image variants** — several templates per logical image.
3. **Per-image match options** — confidence, grayscale, multi-scale, masks,
   search regions.
4. **Verified steps + retry + on_fail** — contingency at the action level.
5. **Failsafe default else** — contingency at the tree level (§2).
6. **Stuck detection** — screen stops changing while acting → pause + notify.
7. **Dry-run mode** — logs what it *would* do, no input sent.

---

## 4. Safety Shell (locked)

Kill switch (`F9`) + mouse-corner failsafe · global actions/min limiter ·
per-group cooldowns + max-fires · human-like jitter/delays · audit log ·
dry-run · **AI driver gets no raw input** — whitelisted named actions only,
through the same shell. (Screenshotter swallows F12 while running — its keys
and our F9 stay separate, always.)

---

## 4a. Conduct Layer (the ethics charter)

The goal: a bot whose manners exceed human players' — verifiably. Two
mechanisms, both emergent from the existing architecture:

**1. Ethics = the top of the tree.** A reserved, always-first subtree —
`&conduct` — evaluated before everything else, using the *same condition and
action vocabulary* as any other group. It doesn't act; it **vetoes**. Example
declarations (plain profile JSON, not code):

```jsonc
{ "if": { "image": "other_player_fighting_this_mob.png" },
  "then": [{ "type": "veto", "groups": ["combat", "looting"],
             "reason": "their kill, not mine" }],
  "else": "continue" }
```

A `veto` suppresses the named groups for its duration — courtesy expressed in
the same language as everything else. Charter ideas it can encode: never
attack first · never loot a kill I didn't participate in · yield resources to
players who arrived first · never crowd another player · no chat except
whitelisted disclosure messages.

**2. Conduct receipts.** The audit log doubles as a **conduct record**: every
action traces to the rule that allowed it, and every veto is logged with its
reason. Exportable as a human-readable **conduct report** — the receipts for
"my bot has better manners than your guild."

Plus the transparency baseline: where disclosure is possible, the bot
discloses (status/macro/ToS-compliant means). The charter is a floor, not a
ceiling — users can only make it stricter, never weaker than the built-ins.

---

## 5. Asset pipeline (locked)

**Screenshotter** is the official capture tool:

```
pythonw Screenshotter/screenshotter.py --dir "assets\<profile>"
```

Phase 5 GUI launches it for you and watches the folder.

---

## 6. Sensors (OCR stats — one-time setup)

Named, anchor-relative screen regions read via **RapidOCR** (pure pip, no
external binary). Used by `stat` conditions. GUI setup: find anchor → drag
box → name → done. (Parking lot: threshold-mask bar reading — "how full is
the red bar" — no OCR needed.)

---

## 7. AI Driver (the MCP harness — Phase 6)

- Model receives a **compact text state vector** (detected images, sensor
  values, position) — tiny tokens, screenshots off by default.
- Returns **one whitelisted named action/group** per decision (on-change or
  ~1/sec cadence). Never raw input. Same Safety Shell applies.
- **MCP server + tiny built-in client** (works standalone with an API key).
- **Hybrid default:** tree = reflexes, AI = strategy (enables/disables
  subtrees). Option: AI fully in charge with tree paused.

---

## 8. Atlas — the map handler (Phase 7)

- **Learning mode:** stitches successive captures into a world map via ORB
  feature matching + homography (§9). Recommended: stitch the **minimap
  corner** (most reliable); full-screen mode for static-background 2D games.
- **Localization:** match current minimap into the world map → world `(x,y)`
  becomes a sensor.
- **Landmarks:** name points ("bank", "spawn", "boss door").
- **New conditions:** `at(place)`, `near(place, radius)`, `map_known_here`.
- **New actions:** `navigate_to(place)` — greedy heading control verified by
  actual position change (stuck detection applies); A* over explored terrain
  is a parking-lot upgrade.
- **Robustness:** incremental building, per-region confidence, conflicting
  stitches flagged never silently merged.

---

## 9. OpenCV primer (what we'll use, why)

- **Images are arrays.** A screenshot is a NumPy array `H × W × 3` (BGR).
  Cropping = slicing; pixel checks = arithmetic.
- **`cv2.matchTemplate` = our bread and butter.** Slides the template across
  the frame, scoring every position; `TM_CCOEFF_NORMED` gives 0–1 — that
  number *is* `confidence`. Best score's location is where we click.
- **Why matches fail, and our fixes:** hue shifts → grayscale both sides;
  UI scale → multi-scale matching; busy backgrounds → search regions + masks.
- **Speed:** regions + grayscale keep 20 Hz easy; `mss` capture is never the
  bottleneck.
- **Feature matching = the stitching trick.** ORB finds keypoints, describes
  their neighborhoods, matches descriptors between frames → homography →
  warp + merge. That's Atlas's engine (and how panoramas work).
- **Thresholds & histograms:** `cv2.threshold` turns "the red bar" into a
  measurable mask (bar-reading without OCR); histogram diffs power stuck
  detection.
- **What OpenCV is *not*:** it doesn't read text (RapidOCR) and doesn't
  understand meaning (your tree / the AI). Eyes, not brain — exactly the
  Ultibot architecture.

---

## 10. Tech stack (locked)

Python 3.11+ · `mss` · `opencv-python` + `numpy` · `pynput` · JSON profiles ·
RapidOCR (Phase 5) · `mcp` SDK (Phase 6) · Tkinter GUI (Phase 5) · venv.

---

## 11. Phases v4

- **0 — Skeleton. ✅ DONE.** Layout, venv, deps, profile JSON schema +
  validator (tree structure, `$ref` integrity, **failsafe-first-entry
  convention**). `ultibot/config.py` + `python -m ultibot validate`, 21
  tests green (`tests/test_config.py`), `profiles/example-game.json` is the
  reference profile. Validator note: two-pass design so forward `$ref`s are
  legal; nested levels without failsafe-first *warn* (bubble-up reaches
  root) rather than error.
- **1 — Eyes. ✅ DONE.** `eyes/capture.py` (mss, monitor + region grabs),
  `eyes/match.py` (confidence, grayscale, multi-scale, regions, variants,
  alpha masks — best hit across all options wins). CLI: `python -m ultibot
  find <img> [--profile/--assets, --region, --grayscale, --scales,
  --variants, --in file]` and `shot <out.png>`. 11 new tests (synthesized
  frames, no screen needed), 32 total green. Templates that are themselves
  saved grayscale work in both modes.
- **2 — Brain. ✅ DONE.** `brain/conditions.py` (evaluator + injected
  clock/RNG EngineState: cooldowns, timers, onces, vetoes, stat history),
  `brain/tree.py` (walker: first-fired-stops, default-else bubble-up to
  nearest failsafe level, failsafe ignores cooldowns/vetoes, question-crash
  guard), `brain/engine.py` (loop + event formatting), `eyes/senses.py`
  (LiveSenses: one grab per tick, missing templates warn-once = not-found).
  CLI: `python -m ultibot run <profile> [--ticks N --verbose]` — dry-run,
  no input. Groups gained `cooldown_ms` / `max_fires`; image conditions
  gained `scales`; vetoes gained `ms`. 23 new tests, 55 total green.
- **3 — Hands.** Executor, humanization, verify+retry+on_fail, macros,
  stuck detection.
- **4 — Safety Shell.** Kill switch, failsafe, rate limiter, audit log.
- **5 — Sensors + GUI.** OCR sensors, visual **tree editor** (drag nodes;
  auto-inserts failsafe entries; highlights missing elses), group/macro
  editor, Screenshotter launcher + asset watcher, live dry-run panel.
- **6 — AI Driver.** State vector, MCP server + built-in client, hybrid mode.
- **7 — Atlas.** Map learning/stitching, localization, `at`/`near`,
  `navigate_to`.
- **Parking lot / dreams.** A* pathing, stats dashboard, notifications,
  bar-reading, profile import/export — and the far dream: perception fast
  enough for FPS games, *always honestly disclosed as a bot*.

---

## 12. Layout

```
The Ultibot/
├── PLAN.md
├── Screenshotter/           ← your tool (--dir assets\<profile>)
├── requirements.txt
├── ultibot/
│   ├── __main__.py
│   ├── config.py            ← load + validate (tree, refs, failsafe rule)
│   ├── eyes/
│   │   ├── capture.py
│   │   ├── match.py
│   │   ├── sensors.py       ← OCR (Phase 5)
│   │   └── atlas.py         ← maps (Phase 7)
│   ├── brain/
│   │   ├── conditions.py
│   │   ├── tree.py          ← walker: else-bubble-up, failsafe, chance
│   │   └── ai_driver.py     ← MCP (Phase 6)
│   ├── hands/
│   │   ├── actions.py       ← executor + verify/retry/on_fail
│   │   ├── macros.py
│   │   └── humanize.py
│   └── safety/
│       ├── guard.py
│       ├── ratelimit.py
│       └── audit.py
├── assets/<profile>/
├── maps/<profile>/
├── profiles/
└── logs/
```

---

## 13. Decisions

**ALL LOCKED (v5):**
1. ✅ Tick semantics: first fired group stops the walk (predictable).
2. ✅ Mandatory else: default = bubble up one level, run that level's first
   entry; validator enforces failsafe-first-entry at every level.
3. ✅ Random primitives (`random_key`, `random_mouse`, `choose`, `chance`)
   are first-class actions, usable anywhere.
4. ✅ AI interface: MCP server + tiny built-in client.
5. ✅ AI mode: tree reflexes + AI strategy hybrid (AI in charge = option).
6. ✅ Phase order: AI driver (6) before Atlas (7).
7. ✅ Conduct Layer (§4a): veto action + always-first `&conduct` subtree +
   audit-backed conduct report. Charter is a floor, never weakenable.
8. ✅ Walker clarification (recorded in Phase 0): the failsafe-first entry
   is the *landing pad* for default elses — it is NOT executed during a
   normal top-down descent (else it would fire every tick). It runs when a
   default else bubbles up to it, or when a whole walk fires nothing.
