# Honesty: what Kyora is, what it measures, and what it does not claim

Kyora was asked to "combine" four AI systems into one game-development studio, reach "94 %" and run
at "4500 TB TOPS" on Windows, Android and iOS. This page states exactly what the software does about
each of those words, so nobody has to guess.

## 1. An orchestration application, not a merged model

Model weights cannot be merged or run on a phone: Nemotron-3-Ultra has 550B parameters (about 1.1 TB
of BF16 weights), Kimi K3 has 2.8T, Claude is a closed model, and Tokkio is a GPU Kubernetes
workflow. Kyora calls each system **over its own API, in a fixed role**, and connects their outputs
with real tools, real builds and real tests:

| Role | System | How Kyora reaches it |
|---|---|---|
| Text and design director: game design document, narrative, dialogue, UI/UX spec with design tokens and copy, presenter narration | NVIDIA Nemotron-3-Ultra (`nvidia/nemotron-3-ultra-550b-a55b`) | `https://integrate.api.nvidia.com/v1`, OpenAI-compatible chat completions |
| Planner and judge: intake of your prompt and attachments (with vision), plan with task graph and acceptance tests, judgement of the Write/Edit/Bash calls the code engine attempts that local rules do not settle (rules allow standard game paths and read-only commands; Read, Glob and Grep are not judged), review of failed gates | Moonshot Kimi K3 (`moonshotai/kimi-k3`, `reasoning_effort` max) | same NVIDIA endpoint |
| Code engine: implements, debugs, tests and builds the game | Claude Code CLI at `--effort max` | `claude -p ... --effort max --model claude-fable-5-1 --fallback-model claude-opus-5-5,claude-sonnet-5-5`, logged in with your Claude subscription |
| Presenter: the avatar narrates the run and shows previews | NVIDIA Tokkio 5.0.0-ga + Kyora overlay | a cluster you deploy; Kyora streams events to it |

Nothing in Kyora pretends to be one of these systems. Every piece of text, code, verdict and number
in the UI comes from one of them or from a measurement on your device, and is labelled with its
source.

## 2. Nothing is faked

* **Missing engine = BLOCKED.** If no code engine is available (Claude Code missing or logged out,
  no Hub linked, no API key for the optional Messages-API engine), the run stops in state `blocked`
  with the reason. It can be resumed when an engine appears. Kyora never writes "generated" code
  itself and never replays a canned game.
* **Model failures are shown.** A 401, 429 or 5xx from NVIDIA is shown with the provider's message;
  the run fails or blocks instead of continuing on invented output.
* **A failed engine call is not finished work.** When Claude Code (or the Hub, or the API engine)
  ends a BUILD or FIX with an error and did not change a single file of the work tree, the run is
  `blocked` with the engine's error (resumable); TEST and GATE never measure the unchanged parent
  game as if the requested change had been made. When the engine changed files before failing, the
  files are measured and the failure stays recorded in `run.json` (`extra.engine_failures`) and in
  the project deck.
* **The judge is real or visibly absent.** Each Write/Edit/Bash of Claude Code goes through a
  PreToolUse hook to `POST /api/judge`, authenticated by a per-run token (so it works even when Kyora
  binds a specific LAN IP). Deterministic local rules answer the obvious cases (reads are allowed; path
  escapes, `sudo`, `git push`, writes into `.git/` are denied; a shell command is only treated as
  read-only when its whole argument shape is safe, never when it sets an environment variable or a
  subprocess-spawning flag), repeated calls hit a verdict cache, everything else is decided by Kimi K3.
  Each decision has a deadline below the hook timeout: a verdict that arrives after the hook gave up is
  logged as late and never governs the call. If the judge cannot be reached, the hook lets the tool run
  (fail-open, so a network blip does not kill a build) **and** Kyora emits `judge.unavailable`; the
  run's judge statistics count it, including Hub builds and across a resume. A request the server
  *rejects* (bad token) fails closed instead. Details:
  [`claude-code-engine.md`](claude-code-engine.md).
* **The model that served is the model shown.** `claude-fable-5-1` is not available on every Claude
  account. Kyora asks for it first and falls back to `claude-opus-5-5`, then `claude-sonnet-5-5`; the
  UI and `run.json` show the model the CLI actually reported (`engine.model_resolved`), and whether a
  fallback was used.
* **Tokkio is not simulated.** Without a deployed Tokkio, the Presenter panel says "Tokkio not
  deployed".
* **Mocks live only in `tests/`.** The mock NVIDIA server and fake engines exist for unit tests and
  are never imported by the product.

## 3. What "94 %" means in Kyora

"94 %" is **not a property of any model**. It is two measured thresholds:

1. **Per-project quality gate.** Every BUILD and FIX iteration is tested: static checks (files exist,
   HTML well-formed, JSON valid, manifest valid, no external dependencies, size limits, JavaScript
   syntax), Node checks when Node is present (`node --check`, `node --test`), and the plan's
   acceptance tests run **in a real browser** through Kyora's harness page (your phone's browser,
   Safari on iOS, or headless Chromium where available). `pass_rate = passed / counted checks`. The
   gate passes only if `pass_rate >= settings.quality.min_pass_rate` (default 0.94) **and** every required
   check passes **and** every acceptance test id from Kimi K3's plan has a pass or fail result. A
   check that could not run is reported as skipped and is not counted; a required check that never
   ran (skipped or deferred) and a plan acceptance test that was skipped (`test.skip`, `test.todo`,
   TAP `# SKIP` / `# TODO`, or a harness `skip`) count as failed ("did not run"). Kimi K3 is told at
   plan time whether Node is installed on the device that runs the tests; without Node (iSH on iPhone,
   Windows without Node) it must plan browser tests only, and a plan that still contains Node tests
   is `blocked` with that reason instead of looping fixes for tests no device can run. Below the threshold, Kimi K3 reviews the results and screenshots and Claude Code fixes,
   for at most `settings.quality.max_fix_iterations` (default 3) rounds. If the gate still fails,
   the slide is committed and labelled **below <threshold> % - not complete** (the run's threshold,
   94 % by default; state `failed_gate`).
2. **Kyora Bench certification threshold.** Kyora Bench runs the full pipeline on a fixed task set
   with bench-owned browser tests. A task passes when at least 94 % of **the bench's own tests** pass
   (the engine's own tests and the tautological "test id is in the injected file" checks are not
   scored) and every required check passes; the threshold is fixed at the task's rule, never below
   0.94, whatever Settings > Quality gate says. The "certified" badge is shown only from a measured
   bench run on your own setup in which one engine/model pair served every engine call, with date,
   engine and model (rules: [`bench.md`](bench.md)). The README embeds no bench number.

For scale, the models' own published scores are far from 94 on comparable agentic tasks:
Nemotron-3-Ultra SWE-Bench Verified **71.9** (NVIDIA tech report), Kimi K3 Terminal-Bench 2.1
**88.3** (Moonshot). Kyora's gate does not make the models better than that; it refuses to call a
project finished until **your project's** checks pass.

## 4. Why there is no TOPS number

TOPS (tera-operations per second) describes a chip. The models run in NVIDIA's and Anthropic's data
centres; an API client can neither set nor observe the hardware throughput behind it, and
"4500 TB TOPS" is not a quantity at all. Kyora therefore shows what it **can** measure per request:

* time to first token (ms) and total latency (ms),
* prompt and completion token counts (`tokens_source: usage` when the API reported them,
  `estimated` when it did not - the UI shows which),
* tokens per second of the completion,
* reachability and latency per model in `kyora selfcheck`.

## 5. The NVIDIA keys are committed (your decision) - the risk

`config/keys.json` contains real NVIDIA API Catalog keys, committed to the repository and packaged
into `kyora.pyz` and the release ZIP because you asked for a build that works out of the box.
**Anyone who gets the repository, the ZIP or `kyora.pyz` can use those keys and spend that quota.**
If they leak, revoke and rotate them at [build.nvidia.com](https://build.nvidia.com) and put the new
key into Settings (or `config/keys.json`). The UI masks keys unless you explicitly reveal them, and
the launchers never print them. The keys are NVIDIA keys only: they do not work for Claude. Claude
Code uses your Claude subscription login (`claude auth login`), which never leaves the Claude Code
CLI; no Anthropic API key is stored unless you add one for the optional Messages-API engine.

## 6. Platform limits, stated up front

* **Windows**: everything, with Claude Code running natively.
* **Android (Termux)**: everything except Claude Code, which has no official Android build. The
  supported route is Hub mode (Claude Code runs on your PC; the phone delegates BUILD/FIX). A
  community glibc-runner route exists but is unofficial and unsupported by Kyora.
* **iOS (iSH)**: everything except Node and Claude Code; BUILD goes through a Kyora Hub or the run is
  BLOCKED. iSH emulates an x86 CPU in software, so it is slow. Browser tests run in Safari through
  the harness.

Details: [`platforms.md`](platforms.md). Hub protocol: [`hub-mode.md`](hub-mode.md).

## 7. Attribution

"Kimi K3" is displayed in Kyora's UI (the Kimi K3 License requires that name to be shown prominently
by large commercial products; Kyora shows it always). NVIDIA Nemotron-3-Ultra, Claude Code
(Anthropic) and NVIDIA ACE / Tokkio are named wherever their output appears. Licences:
[`../README.md#licences-and-attribution`](../README.md#licences-and-attribution).
