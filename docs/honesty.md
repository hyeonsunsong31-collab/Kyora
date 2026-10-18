# Honesty: what Kyora is, what it measures, and what it does not claim

Kyora was asked to "combine" four AI systems into one game-development studio, reach "94 %" and run
at "4500 TB TOPS" on Windows, Android and iOS, and later to keep every function working without a
Claude subscription. This page states exactly what the software does about each of those words, so
nobody has to guess.

## 1. An orchestration application, not a merged model

Model weights cannot be merged or run on a phone: Nemotron-3-Ultra has 550B parameters (about 1.1 TB
of BF16 weights), Kimi K3 has 2.8T, Claude is a closed model, and Tokkio is a GPU Kubernetes
workflow. Kyora calls each system **over its own API, in a fixed role**, and connects their outputs
with real tools, real builds and real tests:

| Role | System | How Kyora reaches it |
|---|---|---|
| Text and design director: game design document, narrative, dialogue, UI/UX spec with design tokens and copy, presenter narration | NVIDIA Nemotron-3-Ultra (`nvidia/nemotron-3-ultra-550b-a55b`; fallback models when it is busy or gone, section 3) | `https://integrate.api.nvidia.com/v1`, OpenAI-compatible chat completions |
| Planner and judge: intake of your prompt and attachments (with vision), plan with task graph and acceptance tests, judgement of the Write/Edit/Bash calls the code engine attempts that local rules do not settle (rules allow standard game paths and read-only commands; Read, Glob and Grep are not judged), review of failed gates | Moonshot Kimi K3 (`moonshotai/kimi-k3`, top-level `reasoning_effort` max; with `kimi.effort_field = extra_body` it is sent as `chat_template_kwargs.reasoning_effort`, best-effort: Kimi K3 documents only the top-level field and may ignore it). On NVIDIA's free endpoint Kimi K3 is retired since 2026-10-15 (HTTP 410), so there the role runs on its fallback models (section 3) | the same NVIDIA endpoint, or the endpoint in `nvidia.kimi.base_url` (for example one that still serves Kimi K3) |
| Code engine: implements, debugs, tests and builds the game | One of: the Claude Code CLI (`--effort max` by default); the same Claude models through the Anthropic Messages API; the **Kyora agent engine**, a different, non-Claude model (default `z-ai/glm-5.3` and fallbacks) | `claude -p ... --effort max --model claude-fable-5-1 --fallback-model claude-opus-5-5,claude-sonnet-5-5`, logged in with your Claude subscription; the official `anthropic` SDK with your API key (billed per use); Kyora's own tool loop over `/chat/completions` with your NVIDIA keys ([`agent-engine.md`](agent-engine.md)) |
| Presenter: the avatar narrates the run and shows previews | NVIDIA Tokkio 5.0.0-ga + Kyora overlay | a cluster you deploy; Kyora streams events to it |

Nothing in Kyora pretends to be one of these systems. Every piece of text, code, verdict and number
in the UI comes from one of them or from a measurement on your device, and is labelled with its
source. When another model, key or engine stands in, Kyora says so (section 3). Static labels name the
*role* ("Kimi K3" in the navigation, the prompt help and the deck's architecture slide); the model that
actually answered is in the events, the tiles, the log and self-check.

## 2. Nothing is faked

* **Missing engine = BLOCKED.** If no code engine is available (no Hub linked, Claude Code missing or
  logged out, no API key for the optional Messages-API engine, and the Kyora agent engine switched off
  or without a key), the run stops in state `blocked` with the reason. It can be resumed when an engine
  appears. Kyora never writes "generated" code itself and never replays a canned game.
* **Model failures are shown.** A 401, 403, 404, 410, 429 or 5xx from NVIDIA is shown with the
  provider's message and what it means (`model.error` carries a `hint`); 429 and 5xx are retried first.
  When no key and no fallback model can answer, the run blocks instead of continuing on invented output.
* **A failed engine call is not finished work.** When a code engine ends a BUILD or FIX with an error
  and did not change a single file of the work tree, the run is `blocked` with the engine's error
  (resumable) or, for an account failure, continues on the next engine (section 3); TEST and GATE
  never measure the unchanged parent game as if the requested change had been made. When the engine
  changed files before failing, the files are measured and the failure stays recorded in `run.json`
  (`extra.engine_failures`) and in the project deck.
* **The judge is real or visibly absent.** Each Write/Edit/Bash of Claude Code goes through a
  PreToolUse hook to `POST /api/judge`, authenticated by a per-run token (so it works even when Kyora
  binds a specific LAN IP); the Messages-API engine and the Kyora agent engine ask the same judge in
  process before each file write and shell command. Deterministic local rules answer the obvious cases
  (reads are allowed; path escapes, `sudo`, `git push`, writes into `.git/` are denied; a shell command
  is only treated as read-only when its whole argument shape is safe, never when it sets an environment
  variable or a subprocess-spawning flag), repeated calls hit a verdict cache, everything else is
  decided by the Kimi role (on NVIDIA's endpoint today: its fallback model, named in the judge journal
  as `judge_model`). Each decision has a deadline below the hook timeout: a verdict that arrives after
  the hook gave up is logged as late and never governs the call. If the judge cannot be reached, the
  tool runs (fail-open, so a network blip does not kill a build) **and** Kyora emits
  `judge.unavailable`; the run's judge statistics count it, including Hub builds and across a resume. A
  request the server *rejects* (bad token) fails closed instead. Details:
  [`claude-code-engine.md`](claude-code-engine.md).
* **The model that served is the model shown.** `claude-fable-5-1` is not available on every Claude
  account. Kyora asks for it first and falls back to `claude-opus-5-5`, then `claude-sonnet-5-5`; the
  UI and `run.json` show the model the CLI actually reported (`engine.model_resolved`), and whether a
  fallback was used. The same holds for the model roles and the Kyora agent engine (section 3).
* **Costs are not invented.** Claude Code's `total_cost_usd` is shown as an API-list-price estimate
  ("API-equiv. est."), not a charge; the Messages API and the Kyora agent engine's endpoint report no
  cost, so none is shown.
* **Tokkio is not simulated.** Without a deployed Tokkio, the Presenter panel says "Tokkio not
  deployed".
* **Mocks live only in `tests/`.** The mock NVIDIA server and fake engines exist for unit tests and
  are never imported by the product.

## 3. Substitutions Kyora can make, and how each is shown

Kyora keeps working when a model is retired, a key is refused, a provider is overloaded or a Claude
subscription ends, by substituting. Every substitution is published as an event (persisted, replayed
in the UI) and shown where the original would have been.

| Substitution | When it happens | How it is shown |
|---|---|---|
| **Fallback model for a role** (Nemotron, Kimi): the next of `nvidia.<role>.fallback_models` | The endpoint says the model is gone (HTTP 404/410: skipped until the role's settings change or Kyora restarts), or it stayed overloaded or failing (HTTP 500/502/503/504 or overload wording) after the client's retries (cooldown 120 s, then it is tried first again) | `model.fallback {kind: "model", role, requested, from_model, to_model, status, reason, temporary, cooldown_s?, purpose?}`; log line "<role>: X answered HTTP 410, now answered by Y - <reason>"; the role's tile shows the answering model and "fallback from X"; every `model.request` / `model.metrics` / `model.done` / `model.error` of the call carries `requested_model` and `fallback: true`; self-check WARN "... The role is answered by the fallback model Y" |
| **Shared key** (`nvidia.share_keys`, on): the other role's key on the same endpoint | A role's own key is refused (HTTP 401/403); the switch holds until the keys change in Settings or Kyora restarts | `model.fallback {kind: "key", role, model, from_key, to_key, status, reason}` with key labels only, never key material; log line "<role>: nemotron key refused (HTTP 403), now using the kimi key"; tile footer "nemotron key refused (HTTP 403), using kimi key"; self-check WARN "the nemotron key was refused (HTTP 403); the kimi key answered (nvidia.share_keys) - ..." |
| **Agent engine model or key fallback** | The same causes inside a Kyora agent engine run, plus a model that refuses function tools | `engine.stderr {kind: "model", from_model, to_model, status, reason, temporary}` / `{kind: "key", from_key, to_key, status}` lines in the log; `engine.model_resolved {requested, active, fallback_used, reason?}`; code-engine tile "fallback from X"; self-check WARN when only a fallback model or key answered |
| **Engine failover**: the same BUILD/FIX continues on the next available engine of `engines.order` | An engine stopped for an account or service reason: usage limit, ended subscription, credits, login, refused keys, unreachable or overloaded service, retired model (`engines.failover`, on). Task failures never fail over | `engine.failover {from, to, reason, failure_kind, kind, iteration, pinned, tree_changed}`; log line "code engine X stopped (<kind>: <reason>) - continuing with Y" (plus "its partial work is kept and the next engine is told about it" when files had changed); code-engine tile "after X stopped (<kind>)"; `run.json` `extra.engine_failovers`, `extra.engine_failures[].failover_to`, and one `extra.engine_runs` entry per attempt; slides and the deck name every model that served the run |
| **The Kyora agent engine instead of Claude** | It is first available in `engines.order`, the run picked it, or a failover reached it | `engine.session.backend: "openai_agent"`; the code-engine tile is named "Kyora agent", shows no Claude effort and "cost: not reported"; the capability chip "Kyora agent: ready" (tooltip) and the selected-engine line say "not a Claude model: its output passes the same measured quality gate"; self-check's *Selected code engine* adds "a non-Claude model; its output passes the same measured quality gate", and a missing Claude Code CLI is WARN saying that BUILD/FIX run on the Kyora agent engine instead |
| **Automatic resume** after a usage limit | No other engine is left (or failover is off) and the engine named the reset time (`engines.auto_resume_on_limit`, on) | `run.blocked {reason, stage, auto_resume_at}` (the reason names the reset in UTC); `run.auto_resume {at, in_s, reset_at, reason}` and the log line "will resume by itself at <time> (in <duration>) - <reason>; Resume starts it earlier"; `GET /api/status` `runs.auto_resume`; `run.json` `extra.auto_resume_at`, `extra.auto_resumes`, `extra.partial_work_kept` |
| **Claude Code session continuation** | Claude Code comes back to the same BUILD/FIX and iteration after its usage limit | `stage.progress` "claude_code: continuing session <id> that stopped at a usage limit"; `run.engine.resumed_session_id`; the prompt starts "Continue the task from where you stopped." |

What a substitution does **not** change: the gate threshold, the checks, the browser tests and the
review/fix loop. A failover or a fallback model is never counted as success by itself; TEST and GATE
measure the result as always.

## 4. What "94 %" means in Kyora

"94 %" is **not a property of any model**. It is two measured thresholds:

1. **Per-project quality gate.** Every BUILD and FIX iteration is tested: static checks (files exist,
   HTML well-formed, JSON valid, manifest valid, no external dependencies, size limits, JavaScript
   syntax), Node checks when Node is present (`node --check`, `node --test`), and the plan's
   acceptance tests run **in a real browser** through Kyora's harness page (your phone's browser,
   Safari on iOS, or headless Chromium where available). `pass_rate = passed / counted checks`. The
   gate passes only if `pass_rate >= settings.quality.min_pass_rate` (default 0.94) **and** every required
   check passes **and** every acceptance test id from the plan has a pass or fail result. A
   check that could not run is reported as skipped and is not counted; a required check that never
   ran (skipped or deferred) and a plan acceptance test that was skipped (`test.skip`, `test.todo`,
   TAP `# SKIP` / `# TODO`, or a harness `skip`) count as failed ("did not run"). The planner is told at
   plan time whether Node is installed on the device that runs the tests; without Node (iSH on iPhone,
   Windows without Node) it must plan browser tests only, and a plan that still contains Node tests
   is `blocked` with that reason instead of looping fixes for tests no device can run. Below the
   threshold, the Kimi role reviews the results and screenshots and the code engine fixes, for at most
   `settings.quality.max_fix_iterations` (default 3) rounds. If the gate still fails, the slide is
   committed and labelled **below <threshold> % - not complete** (the run's threshold, 94 % by default;
   state `failed_gate`). The gate is the same for every code engine: a game built by the Kyora agent
   engine is measured exactly like one built by Claude Code.
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
project finished until **your project's** checks pass. Kyora publishes no score for the Kyora agent
engine's models and does not claim that they match Claude.

## 5. Without a Claude subscription: what changes and what does not

* **Anthropic API key**: the same Claude models at the same effort through the Messages API, billed per
  token to your key. The model capability is the same; the harness is Kyora's own judged tool loop
  instead of the Claude Code CLI.
* **Kyora agent engine**: no Claude at all. A different model (default `z-ai/glm-5.3`, then its
  fallbacks) on your NVIDIA keys. Kyora does not claim equal capability. The quality bar is unchanged, so
  a game is accepted only at the same measured bar; it may take more fix rounds or more time, or end
  below the gate.
* **A Kyora Hub on another machine**: that machine's engine and its owner's quota.
* The live CI job "live pipeline without Claude (NVIDIA models + Kyora agent engine)" runs one game from
  prompt to gate with only the agent engine allowed; its result is reported in that job, not here.

Not measured here: whether a fallback model of the Kimi role reads reference images (they are sent as
image parts to whichever model answers); which agent-engine models call tools reliably on a given day
(the full self-check sends one real tool-call request on your setup).

## 6. Why there is no TOPS number

TOPS (tera-operations per second) describes a chip. The models run in NVIDIA's and Anthropic's data
centres; an API client can neither set nor observe the hardware throughput behind it, and
"4500 TB TOPS" is not a quantity at all. Kyora therefore shows what it **can** measure per request:

* time to first token (ms) and total latency (ms),
* prompt and completion token counts (`tokens_source: usage` when the API reported them,
  `estimated` when it did not - the UI shows which),
* tokens per second of the completion,
* reachability and latency per model in `kyora selfcheck`.

## 7. The NVIDIA keys are committed (your decision) - the risk

`config/keys.json` contains real NVIDIA API Catalog keys, committed to the repository and packaged
into `kyora.pyz` and the release ZIP because you asked for a build that works out of the box.
**Anyone who gets the repository, the ZIP or `kyora.pyz` can use those keys and spend that quota.**
If they leak, revoke and rotate them at [build.nvidia.com](https://build.nvidia.com) and put the new
key into Settings (or `config/keys.json`). As measured on 2026-10-18, the key in the Nemotron slot gets
HTTP 403 "Authorization failed" for every model (its account lacks inference access) and the key in the
Kimi slot answers; with `nvidia.share_keys` on, both roles use the Kimi-slot key and Kyora says so
(section 3). The UI masks keys unless you explicitly reveal them, and the launchers never print them.
The keys are NVIDIA keys only: they do not work for Claude. Claude Code uses your Claude subscription
login (`claude auth login`), which never leaves the Claude Code CLI; no Anthropic API key is stored
unless you add one for the optional Messages-API engine. By default the Kyora agent engine sends the
Kimi-slot key, then the Nemotron-slot key, to `nvidia.base_url`; a key set in `agent.api_key` goes only
to the agent endpoint, and an agent endpoint other than NVIDIA's never receives the NVIDIA keys. Shell
commands of the Messages-API and agent engines run with API keys, tokens and other secrets removed
from their environment.

## 8. Platform limits, stated up front

* **Windows**: everything, with Claude Code running natively. Without a subscription, the Anthropic
  API engine or the Kyora agent engine runs BUILD/FIX (the agent engine's shell tool needs Git Bash;
  without it the engine works with its file editor only).
* **Android (Termux)**: Claude Code has no official Android build. BUILD/FIX run on the phone with the
  Kyora agent engine (NVIDIA models, not Claude), or through Hub mode on your PC for Claude. A community
  glibc-runner route exists but is unofficial and unsupported by Kyora.
* **iOS (iSH)**: no Node and no Claude Code. BUILD/FIX run on the phone with the Kyora agent engine when
  the NVIDIA endpoint is reachable, or through a Kyora Hub; with neither, the run is BLOCKED. iSH
  emulates an x86 CPU in software, so everything, including a local BUILD, is slow. Browser tests run
  in Safari through the harness.

Details: [`platforms.md`](platforms.md). Hub protocol: [`hub-mode.md`](hub-mode.md). Agent engine:
[`agent-engine.md`](agent-engine.md).

## 9. Attribution

"Kimi K3" is displayed in Kyora's UI (the Kimi K3 License requires that name to be shown prominently
by large commercial products; Kyora shows it always). NVIDIA Nemotron-3-Ultra, Claude Code
(Anthropic), the models that answer as fallbacks or for the Kyora agent engine, and NVIDIA ACE /
Tokkio are named wherever their output appears. Licences:
[`../README.md#licences-and-attribution`](../README.md#licences-and-attribution).
