# Kyora Bench

Kyora Bench measures how often the **whole Kyora pipeline** turns a realistic game brief into a working
game. It is a suite of 24 game-development tasks with automatic, bench-owned checkers. The only number it
produces is the pass rate **you measure** on your own machine with your own keys and Claude login.
No score is pre-filled anywhere: not in the README, not in the UI, not in this document.

## What one task measures

Every task is one real pipeline run, exactly as a user would start it:

```
Kimi K3 intake -> Kimi K3 plan -> Nemotron design + UI spec -> code engine build (Claude Code, --effort max)
-> static checks + Node checks + in-browser tests -> >= 94 % gate -> [Kimi K3 review -> engine fix] x <= 3 -> slide
```

The difference from a normal run is the test file. At the TEST stage the pipeline keeps the engine's own
`tests/browser-tests.js` as `tests/generated-browser-tests.js` and puts the bench's file for the task in its
place (`kyora/bench/tasks/<id>.browser-tests.js`). The plan's acceptance tests are replaced by the bench's
test ids, so the gate requires every bench test to report a result. The generated tests still exist in the
workspace but are not scored.

**A task passes** iff its run ends in state `complete` **and** the run's final gate says `ok: true`
(pass rate >= 0.94 over all counted static, Node and browser checks, every required check passing, every
bench test id present). If the runner can see the final gate's checks and fewer of the bench's browser tests
than the task's `min_browser_tests` appear in them, the task fails too: the bench was not actually measured.

## The 24 tasks

| id | category | kind | game |
|---|---|---|---|
| b01-breakout | arcade | generate | Breakout |
| b02-snake | arcade | generate | Snake |
| b03-pong | arcade | generate | Pong against an AI paddle |
| b04-space-shooter | arcade | generate | Vertical space shooter |
| b05-flappy | arcade | generate | Flappy-style pipes |
| b06-asteroids | arcade | generate | Asteroids with wrapping |
| b07-2048 | puzzle | generate | 2048 |
| b08-match-3 | puzzle | generate | Match-3 with cascades |
| b09-sokoban | puzzle | generate | Sokoban with a level loader |
| b10-minesweeper | puzzle | generate | Minesweeper |
| b11-sliding-puzzle | puzzle | generate | 15-puzzle |
| b12-memory-pairs | puzzle | generate | Memory pairs |
| b13-platformer | action | generate | Side-view platformer with a level loader |
| b14-endless-runner | action | generate | Endless runner (jump and duck) |
| b15-topdown-dodge | action | generate | Top-down dodge with a virtual joystick |
| b16-tower-defense-lite | strategy/sim | generate | Tower defense on a fixed path |
| b17-idle-clicker | strategy/sim | generate | Idle clicker with scaling prices |
| b18-life-sim | strategy/sim | generate | Tiny life sim (needs, day cycle) |
| b19-text-adventure | narrative | generate | Parser text adventure with a fixed map |
| b20-visual-novel | narrative | generate | Branching visual novel with save/load |
| b21-tilemap-editor | systems | generate | Tile-map editor (fill, undo, export) |
| b22-particle-sandbox | systems | generate | Falling-sand sandbox |
| b23-snake-add-pause-highscore | modify | modify | Add pause + persisted high score to an existing Snake |
| b24-breakout-touch-and-bugfix | modify | modify | Fix a paddle collision bug and add touch to an existing Breakout |

Each task file (`kyora/bench/tasks/<id>.json`) holds the brief (`prompt`, 150-300 words), `engine: "vanilla"`,
`timeout_min`, `constraints` (no external dependencies, file budget, touch controls), the `contract`
(`globals: ["window.Game"]` plus every `window.Game` method with its meaning, repeated verbatim in the
prompt), `checks` (expected static checks, the browser test file, `min_browser_tests`) and
`pass_rule: {min_pass_rate: 0.94}`. The two modify tasks name a `fixture`: a small plain-text game under
`kyora/bench/tasks/fixtures/<name>/` that the runner zips at run time (deterministically) and imports as the
project's starting point. The Breakout fixture contains the collision bug the b24 brief describes; the Snake
fixture has no pause and no high score.

## How the tests drive a game

The tests never look at pixels. Every brief requires a `window.Game` object with at least:

- `Game.start()` - a new game from any phase (`phase` becomes `'play'`);
- `Game.getState()` - a fresh snapshot `{phase: 'menu'|'play'|'over', score, ...task fields}`;
- `Game.setSeed(n)` - all randomness comes from one seeded PRNG, so the same seed gives the same game;
- `Game.step(n)` - advance exactly `n` fixed 1/60 s ticks. The first call switches the game to a manual
  clock, so tests are deterministic and do not depend on the device's frame rate;
- input helpers (`Game.input.press('left')`, `Game.input.move('up')`, `Game.input.flip(3)`, ...) and, where
  needed, small debug hooks (`Game.debug.setBall(...)`, `Game.loadLevel(...)`) to set up exact situations.

Each test file also exercises the real input paths through the harness API (`KyoraTest.input.key`,
`click`, `touch` with swipes and long presses) and takes a screenshot of the canvas. Tests use generous
bounds where the brief leaves freedom (for example "the paddle moved left", not "the paddle is at x=212").

The runner appends one fixed block of **harness rules** to every brief before submitting it (the text is
`HARNESS_NOTES` in `kyora/bench/runner.py`, and its SHA-256 is stored in every `results.json`): manual clock
semantics, menu-first loading, seeded randomness, coordinates, keyboard by `KeyboardEvent.code`, one action
per click or tap, `touch-action: none` on the canvas, and the note that the bench replaces
`tests/browser-tests.js` when measuring. The same block goes to every task, so the comparison stays fair.

Browser tests run inside the game page through the Kyora harness (`/runner/<project>/<ref>`). Something
has to open that page: the headless driver (`kyora/runners/headless_browser.mjs`, used automatically when
Node and Playwright are installed), or the Kyora UI in a browser, which opens the harness when a run asks
for browser tests. Without a browser the run is BLOCKED after `quality.browser_wait_s` and the task fails.

## Running it

It costs real money and time. **Each task runs the full pipeline: Nemotron + Kimi K3 tokens on your NVIDIA
keys and Claude subscription usage at max effort; typically 10-30 min per task.** A full run is 24 pipeline
runs, so expect many hours and a large share of your Claude usage limits. Start with one or two tasks.

```
kyora selfcheck                       # models reachable, Claude logged in, an engine available
kyora bench --list                    # the 24 tasks
kyora bench --tasks b01 --dry-run     # what would run (estimate only, spends nothing)
kyora bench --tasks b01,b02           # asks for confirmation, then runs two tasks
kyora bench --yes                     # the full suite without the prompt (needed for certification)
```

Task ids can be given in full (`b07-2048`) or by prefix (`b07`). From the CLI the runner starts an
embedded loopback server when none is running, because the judge hook and the browser harness need one;
open the printed URL to watch the pipeline and to run browser tests if Playwright is not installed.

In the UI, the **Bench** tab lists the tasks, shows the same cost warning in a confirmation dialog, streams
`bench.progress` events, and shows the latest measured results and badge.

REST (loopback, or a Hub token with `hub.expose_ui`):

| route | purpose |
|---|---|
| `GET /api/bench/tasks` | `{tasks, estimate, running}` |
| `POST /api/bench/run` | json `{task_ids, confirm: true}` -> 202 `{started, estimate}`; 400 without `confirm: true` or without a code engine; 409 while a bench run is active |
| `POST /api/bench/cancel` | cancel the active bench run (the current pipeline run is cancelled, the rest skipped) |
| `GET /api/bench/results` | `{results: <latest results.json> or null, tasks: <its per-task rows>}` |
| `GET /api/bench/badge` | certification computed from the latest measured run |
| `GET /api/bench/report` | the latest standalone `report.html` |

Without a code engine (no Claude Code login, no Hub, no Anthropic key) the bench refuses to start; it never
simulates an engine. Only one bench run can be active at a time; tasks run one after another, each with its
own project `bench-<id>-<timestamp>` and its own `timeout_min` (the run is cancelled when it expires).

## Results

Every bench run writes `KYORA_HOME/bench/<timestamp>/`:

- `results.json` - rewritten after every task, so a crash keeps what was measured: `bench_id`, start and end
  times, `kyora_version`, `platform`, the SHA-256 of each composed prompt and of the harness rules, `skipped`
  task ids, and per task `{task_id, run_id, project_id, state, gate, passed, duration_s, engine, active_model,
  error, bench_tests_seen, ...}`, plus the `score`.
- `report.html` - a standalone page (no scripts, no external URLs) with the per-task table: result, state,
  gate %, time, engine/model and error, ending with *"Measured by Kyora Bench on <date> with
  <engine>/<model> on this machine. No number here is estimated."*

Events: `bench.progress {done, total, task_id, passed}` after each task and `bench.done {score}` at the end.

## Certification

The badge says **certified** only when all of these hold for a single bench run:

1. the suite is complete: all 24 distinct task ids ran in that run (partial runs, or results merged from
   several runs, never certify);
2. one engine/model pair served every task (the model is read from what actually answered, so a fallback
   model on some tasks breaks the badge);
3. the measured pass rate is at least 0.94 (23 of 24 tasks = 0.958 certifies; 22 of 24 = 0.917 does not).

Otherwise the badge shows "not certified" with the reason, or "no bench run yet". The badge is recomputed
from the latest `results.json` on every request; there is no stored or default value.

94 % here is the same threshold as Kyora's per-project quality gate. It says nothing about any model's
published benchmark scores, and Kyora makes no such claim.

## Keeping the bench honest

`tests/test_bench.py` checks the suite itself: 24 tasks with unique ids and the required keys, every
contract method quoted verbatim in its prompt, 150-300 word briefs, 8-14 uniquely numbered tests per file
with `min_browser_tests` not above the real count, `node --check` on every script, registration of every
test id in the real `kyora-test-api.js`, deterministic fixture zips, and the scoring and certification
rules. When Node and Playwright are available it also runs bench test files in headless Chromium against the
shipped fixtures: the Snake fixture passes all b02 tests, the keyboard-only Breakout passes every b01 test
except touch, and both fixtures still fail the new-feature tests of b23 and b24.

During development every test file was also run in Chromium, with the real harness API, against a
reference implementation of its contract. Those reference games are deliberately not shipped: they would
be the answers.

Known limits: the tests check behaviour through `window.Game` and real input events, not visual quality;
an implementation that games the debug hooks could pass tests a player would not enjoy, which is why the
pipeline's Kimi K3 review and the per-run deck stay part of every task. Timing-sensitive input (long press,
swipes) uses real milliseconds in the browser, so a heavily loaded device can still produce a flaky result;
re-run a single task with `kyora bench --tasks <id>` before trusting a surprising failure.
