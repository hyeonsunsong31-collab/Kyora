# Kyora - AI game-development studio

Kyora is an **orchestration application** for making browser games with AI. It calls external systems
over their own APIs, each in a fixed role: **NVIDIA Nemotron-3-Ultra** writes the design (game design
document, narrative, UI spec, copy, narration), **Moonshot Kimi K3.1 Pro** (through OpenRouter) plans the
work, judges the code engine's file writes and shell commands that Kyora's local rules do not settle (rules allow standard game
paths and read-only commands; reads are not judged) and reviews failed results, a **code engine** writes,
tests and fixes the game - **Claude Code** (CLI, `--effort max` by default, your Claude subscription), or
the same Claude models through an Anthropic API key, or the **Kyora agent engine** on your NVIDIA keys
(a different, non-Claude model) - and **NVIDIA Tokkio** (optional, on your own GPU cluster) presents the
run as a talking avatar. Kimi K3.1 Pro needs your OpenRouter key, which is never committed to this
public repository ([Kimi K3.1 Pro on OpenRouter](#kimi-k31-pro-on-openrouter)); without it, or when
OpenRouter cannot answer, the Kimi role's fallback models answer on NVIDIA's endpoint and Kyora shows
which model answered ([NVIDIA status](#nvidia-status-measured-2026-10-18)). Kyora runs the real tests in a real browser and
only calls a project finished when at least **the configured pass rate (default 94 %,
`quality.min_pass_rate`) of that project's measured checks pass**, whichever engine built it. It is not a
merged model, it never fakes output, metrics or verdicts, and when a piece is missing it says
**BLOCKED** instead of pretending. The core is Python 3.10+ standard library only and ships as one file,
`kyora.pyz`, for Windows, Android (Termux) and iOS (iSH).

- [What Kyora is / is not](#what-kyora-is--is-not)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [First run](#first-run)
- [Without a Claude subscription](#without-a-claude-subscription)
- [Kimi K3.1 Pro on OpenRouter](#kimi-k31-pro-on-openrouter)
- [NVIDIA status (measured 2026-10-18)](#nvidia-status-measured-2026-10-18)
- [Hub mode (phones and tablets)](#hub-mode-phones-and-tablets)
- [Tokkio presenter](#tokkio-presenter)
- [Kyora Bench](#kyora-bench)
- [Settings and keys](#settings-and-keys)
- [Effort and models](#effort-and-models)
- [Troubleshooting](#troubleshooting)
- [Licences and attribution](#licences-and-attribution)
- [Development](#development)

## What Kyora is / is not

| Kyora is | Kyora is not |
|---|---|
| An app that chains Nemotron-3-Ultra, Kimi K3.1 Pro, a code engine (Claude Code, the Anthropic API or the Kyora agent engine) and (optionally) Tokkio through their APIs, with real tool execution, builds and tests | A new or merged model. The weights cannot be combined: Nemotron-3-Ultra is 550B parameters, Kimi K3.1 Pro is served by its provider through OpenRouter, Claude is closed, Tokkio is a GPU Kubernetes workflow |
| A studio with projects, attachments, live progress, previews on your phone, per-project "slides" (every generation or modification is a git commit + tag you can preview, branch from, stack on and download as a ZIP) and an auto-built presentation deck | A system that invents results: no canned games, no simulated avatar, no estimated benchmark scores |
| A **measured 94 % quality gate** per project, the same for every code engine, and a **Kyora Bench** whose 94 % threshold is the certification rule | A claim that any model scores 94 % (published: Nemotron-3-Ultra SWE-Bench Verified 71.9, Kimi K3 Terminal-Bench 2.1 88.3), or that the Kyora agent engine's model is as capable as Claude |
| A display of **measured** time to first token, latency, token counts and tokens/s per model call, and of the model and key that actually answered | A TOPS number: TOPS describes data-centre chips and cannot be set or measured over an API |
| One `kyora.pyz` for Windows, Android/Termux and iOS/iSH, with honest per-platform limits | A Claude Code port to phones: there is no official Android or iOS build; phones build with the Kyora agent engine or through a Hub |

Full statement: [`docs/honesty.md`](docs/honesty.md).

## Architecture

```
            +------------- Kyora UI (vanilla HTML/JS, mobile-first, Server-Sent Events) --------------+
            | prompt + attachments | pipeline chart | live log | preview | slides | deck | settings   |
            | self-check | bench | presenter (Tokkio UI) | hub link (QR)                              |
            +-------------------------------------------+---------------------------------------------+
                                                        | HTTP + SSE (stdlib ThreadingHTTPServer)
+-------------------------------------------------------+---------------------------------------------+
| kyora.pyz (Python standard library only)                                                            |
|  pipeline: INTAKE -> PLAN -> DESIGN -> BUILD -> TEST -> GATE -> [REVIEW -> FIX]* -> FINALIZE        |
|  models/:  nim_client (OpenAI-compatible SSE, key + model fallback) | nemotron | kimi | tokkio      |
|            code engines: claude_code | anthropic_api | openai_agent (Kyora agent) | hub client      |
|  engines:  settings.engines.order -> failover on account failures -> auto-resume after a limit      |
|  quality:  static checks + Node checks + in-browser acceptance tests -> gate (>= min_pass_rate)     |
|  judge:    Write/Edit/Bash -> hook POST /api/judge (Claude Code) or in-process -> rules/cache/Kimi  |
|  projects: store | slides (git or snapshot) | deck | zip      hub: /api/code-engine/* (LAN, token)  |
+---------+------------------------+---------------------------+-------------------------+------------+
          |                        |                           |                         |
   Nemotron role         Kimi role                code engine (engines.order)    NVIDIA Tokkio 5.0.0-ga
   Nemotron-3-Ultra      Kimi K3.1 Pro            Claude Code CLI (subscription, (your GPU k8s cluster;
   (NVIDIA API Catalog;  (OpenRouter, your key;   --effort max) | Anthropic API  overlay processor
   fallback when busy)   fallbacks on NVIDIA)     key | Kyora agent engine       reads Kyora's
   design, UI spec,      intake, plan, tests,     (NVIDIA models, not Claude)    event stream)
   copy, narration       judge, review            code, tests, fixes
```

Contracts (routes, events, schemas): [`docs/architecture.md`](docs/architecture.md). Code engines and
judge: [`docs/claude-code-engine.md`](docs/claude-code-engine.md) and
[`docs/agent-engine.md`](docs/agent-engine.md). UI: [`docs/ui-design.md`](docs/ui-design.md).

## Quick start

Download `Kyora-AGI-Studio-<version>.zip` (or clone this repository) and unpack it. The release
folder contains `kyora.pyz`, `config/`, `launchers/`, the source and the docs. Per-platform details
and limits: [`docs/platforms.md`](docs/platforms.md).

**Windows** (PowerShell 5.1 or 7; full functionality):

```powershell
cd Kyora-AGI-Studio-0.1.0
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\install.ps1 -WithNode
claude auth login            # once; log in with your Claude subscription (no API key)
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 selfcheck
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 serve
```

`install.ps1` installs Python 3.12, Git, Node.js LTS (with `-WithNode`) and Claude Code via winget
(falling back to Anthropic's official installer), and checks that Claude Code is logged in.
`kyora.ps1 [serve|hub|selfcheck|bench|version] [args...]` starts Kyora; `serve` opens
http://127.0.0.1:8765. Games are served from a second port, 8766 (see [Security model](#security-model)).
No Claude subscription? Skip `claude auth login` (`install.ps1 -SkipClaude` skips Claude Code) and read
[Without a Claude subscription](#without-a-claude-subscription).

**Android** (Termux; Claude Code has no official Android build, so BUILD/FIX run on the Kyora agent
engine with your NVIDIA keys, or through a linked PC Hub):

```sh
sh launchers/install-termux.sh          # python, git, nodejs-lts (--with-claude: optional attempt)
sh launchers/kyora-termux.sh selfcheck
sh launchers/kyora-termux.sh            # serves and opens the UI
```

**iOS / iPadOS** (iSH; no Node and no Claude Code; slow x86 emulation; BUILD/FIX run on the Kyora agent
engine when the NVIDIA endpoint is reachable from the phone, or through a linked PC Hub):

```sh
sh launchers/install-ish.sh             # python3, git
sh launchers/kyora-ish.sh               # wait for "Open: http://127.0.0.1:8765", then open it in Safari
```

On iSH, Kyora keeps answering while Safari is in front: iOS pauses an app a few seconds after another
app comes to the front, so Kyora holds iSH's location stream open (iOS asks once for location access
for iSH: choose *Allow While Using App*; Kyora keeps no location) and opens its port again if iOS took
it away. Details and the opt-out: [`docs/platforms.md`](docs/platforms.md#ios--ipados-ish).

**Anywhere with Python 3.10+**: `python3 kyora.pyz serve --open` (or `python3 -m kyora serve --open`
in a source checkout).

## First run

1. **Self-check** - `kyora.ps1 selfcheck` (or the *Self-check* tab) measures, on your device: Python,
   platform, Git, Node, the Claude Code version and login state, the Nemotron and Kimi roles (which
   model and which key answered, with latency), the optional Anthropic engine, the Kyora agent engine
   (the full check sends it one tool-call request), the selected code engine, Tokkio and Hub
   reachability, and the LAN URLs. Fix anything marked FAIL first; WARN says what was substituted.
2. **Create a project** - *New*, give it a name. Optionally drop attachments: reference images (sent
   as images only to a model that takes images - Kimi K3.1 Pro or a vision model; when OpenRouter has no
   provider for images it is asked again without them, and a text-only fallback is told they were left
   out), design notes, code, or a game ZIP to import as the base.
3. **Run** - write the prompt ("a one-button endless runner with a day/night cycle, touch controls")
   and press *Run pipeline*. The pipeline chart shows each stage with measured times; the live log
   streams model output, the code engine's tool calls, every judge verdict and every model, key or
   engine substitution; the metrics tiles show time to first token, tokens/s and token counts per model.
4. **Test and gate** - when BUILD finishes, the browser test harness runs the acceptance tests in your
   browser (keep the tab open). The gate shows `passed / total` and the pass rate against the configured
   threshold (default 94 %, `quality.min_pass_rate`). Below it, the Kimi role reviews (the log names the
   model that answered; when none can, Kyora's rules turn the failing checks into the fix instructions)
   and the code engine fixes, up to `quality.max_fix_iterations` rounds (default 3).
5. **Preview on your phone** - *Open on another device* shows a LAN link and QR code (the PC must run
   `kyora hub` or `serve --host 0.0.0.0` with *Settings -> hub.expose_ui* on, the phone must be on
   the same network, and the PC firewall must let in both ports, 8765 for the UI and 8766 for games).
6. **Download** - every finished run becomes a **slide** (`slide/v1`, `v2`, ...). From the slide strip
   you can preview any slide, branch from it, stack a modification on it (*Modify current slide*) or
   download it as a ZIP.
7. **Open deck** - each project has an auto-generated HTML presentation (concept, design tokens,
   screenshots, test and gate results, changelog, attribution; print it to PDF from the browser).
   It is written into the game as `kyora/deck.html`, so slide ZIPs carry it.

## Without a Claude subscription

Only BUILD and FIX use Claude. They keep working without a Claude subscription (or after it ends) on one
of three routes, which differ in what changes:

1. **An Anthropic API key - the same Claude models, billed per use.** Put the key in *Settings ->
   Anthropic* (`anthropic.api_key`, or `ANTHROPIC_API_KEY` in the environment) and run
   `pip install anthropic`. The `anthropic_api` engine then runs BUILD and FIX on the pay-per-use
   Messages API with the models and effort of the `claude_code` settings (`claude-fable-5-1`, then
   `claude-opus-5-5`, `claude-sonnet-5-5`; effort `max` by default). Same models at the same effort, so
   no loss of model capability; the harness is Kyora's own tool loop (judged `bash` and file-editor
   tools) instead of the Claude Code CLI. It is billed per token to that key; the API reports no cost
   to Kyora, so none is shown.
2. **The Kyora agent engine on your NVIDIA keys - no Claude at all.** `agent_engine` (on by default)
   runs BUILD and FIX as Kyora's own tool loop on an OpenAI-compatible endpoint, by default NVIDIA's API
   Catalog with the keys in `keys.json`: model `z-ai/glm-5.3`, then the configured fallbacks
   `nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-super-vl-120b-a12b` and
   `nvidia/nemotron-3-super-120b-a12b`.
   Its tools are the judged `bash` and a file editor confined to the workspace, plus `kyora_check`, which
   runs Kyora's static checks and Node logic tests so the model can fix failures before TEST. **This is
   a different model, so Kyora does not claim it is as capable as Claude.** What does not change is the
   bar: the same gate (at least `quality.min_pass_rate`, default 94 %, every required check), the same
   tests in a real browser and the same review/fix loop decide, so a game is accepted only when it meets
   the same measured bar. It may need more fix rounds or more time, or end below the gate. On NVIDIA's
   free tier, rate limits and the occasional overload are handled by retries, a cooldown of the busy
   model and the fallback models, and every substitution is shown. The CI job "live pipeline without
   Claude (NVIDIA models + Kyora agent engine)" runs one game from prompt to gate with
   `engines.order = ["openai_agent"]`; its result is reported there. On 2026-10-24 (one run, not a
   benchmark) it ended `complete`: gate 32/32 (threshold 94 %) after two fix iterations, about 30 minutes,
   BUILD/FIX answered by GLM-5.3 and Nemotron-3-Ultra, the Kimi and Nemotron roles by their fallback
   Nemotron-3-Ultra (one Kimi-role call by GLM-5.3), 73 judged tool calls allowed. A second run that day,
   after the sidelining change in the CHANGELOG (another generated game), also ended `complete`: gate
   29/29 after two fix iterations, about 47 minutes; BUILD and the first FIX iteration used all 80 turns,
   and Nemotron-3-Ultra answered 178 of the 213 agent turns, Nemotron-3.5-Super-VL 31, GLM-5.3 4. Details:
   [`docs/agent-engine.md`](docs/agent-engine.md).
3. **A Kyora Hub on another machine.** Link a Hub (scan its QR code, or *Settings -> Hub*) and BUILD/FIX
   run on that machine's first available engine: its Claude Code login, its Anthropic API key or its
   Kyora agent engine. The usage is that machine's, so link only machines whose owner agrees. See
   [Hub mode](#hub-mode-phones-and-tablets).

### Engine order and automatic failover

* **Order.** `engines.order` (default `["hub", "claude_code", "anthropic_api", "openai_agent"]`) decides
  which available engine runs a BUILD/FIX; a run can pick one in its *Code engine* field (the *Auto*
  label follows the order). `hub` counts only while a Hub is linked.
* **Failover** (`engines.failover`, on). When an engine stops for an account or service reason - a
  usage limit, an ended subscription, no credits, a login problem, refused keys, an unreachable or
  overloaded service, a retired model - the same BUILD/FIX continues on the next available engine in
  that order (also when the run picked that engine: the event then carries `pinned: true`). If the stopped engine had
  already changed files, the next engine is told to continue that partial work instead of starting over.
  The log shows `engine.failover` ("code engine X stopped (quota: ...) - continuing with Y"), the
  code-engine tile notes "after X stopped", and `run.json` records every attempt (`extra.engine_runs`,
  `extra.engine_failures`, `extra.engine_failovers`). Task failures (max turns, timeouts, refusals, judge
  denials) never fail over, and a failover is never counted as success: TEST and GATE measure the result
  as always.
* While Claude Code still counts as available (installed and logged in), each BUILD/FIX tries it first
  and fails over only after it stopped. To skip that attempt, put another engine first in
  `engines.order` or pick it in the run's *Code engine* field. To wait for Claude instead of continuing
  on another model, turn `engines.failover` off.
* **Automatic resume** (`engines.auto_resume_on_limit`, on). When no other engine is left (or failover
  is off) and the engine said when its usage limit resets (Claude Code's "... resets 1:20pm (UTC)"
  message, its rate-limit event or an explicit reset time), the run is BLOCKED with the reset time in
  UTC, any partial work is committed (`extra.partial_work_kept`) and Kyora resumes the run by itself a
  minute after the reset (`run.auto_resume` in the log; `extra.auto_resume_at`, `extra.auto_resumes`).
  The timer survives a restart of Kyora (a reset that passed while Kyora was stopped resumes the run at
  the next start), re-checks the clock at least every 10 minutes, stops after 24 automatic resumes of a
  run, and is dropped by *Resume* or *Cancel*. Without a known reset time the run stays BLOCKED until
  you press *Resume*.
* **Claude Code session continuation.** When a run comes back to Claude Code for the same BUILD/FIX and
  iteration after a usage limit, Claude Code continues its own session (`--resume <session>`) with
  "Continue the task from where you stopped."; if that session no longer exists, a fresh session starts
  and is told about the kept partial work.

## Kimi K3.1 Pro on OpenRouter

Since 2026-11-03 the Kimi role (intake, plan, judge, review) runs **Kimi K3.1 Pro**
(`moonshotai/kimi-k3.1-pro`) through OpenRouter's OpenAI-compatible API
(`https://openrouter.ai/api/v1/chat/completions`), with the request you specified:

* `reasoning: {"enabled": true}` at effort `max`, the default for planning, judging and reviewing
  (`high` and `low` send `reasoning: {"enabled": true, "effort": "high"}` or `"low"`);
* `provider: {"only": ["moonshotai/mxfp4"], "allow_fallbacks": false}` (`nvidia.kimi.provider` in
  `keys.json`), sent only with Kimi K3.1 Pro, so OpenRouter routes the request to that provider and to
  no other;
* the assistant message's `reasoning_details` go back unmodified in the next turn of the same
  conversation (for example the repair request after an answer that could not be used). Streamed fragments are put back together into the
  blocks OpenRouter sent. A fallback model on NVIDIA's endpoint gets `reasoning_content` instead, never
  `reasoning_details`.

**Your OpenRouter key is not in this repository.** The repository is public: anyone who can read it
could spend your OpenRouter credits with a committed key, and GitHub's secret scanning has a pattern for
OpenRouter keys. Kyora takes the key from, in order:

1. `config/keys.local.json`, next to `keys.json` - git-ignored and never packed into `kyora.pyz` or the
   public release ZIP: `{"nvidia": {"kimi": {"api_key": "sk-or-v1-..."}}}`. The private release ZIP
   (`Kyora-AGI-Studio-<version>-private.zip`, sent to you, not published) already contains this file.
2. Settings -> *Model endpoints* -> *Kimi API key (OpenRouter)*, which is saved to `keys.local.json`,
   never to `keys.json`. Every secret entered in Settings (the Nemotron, Kimi, Anthropic and agent keys
   and the hub token) is saved there.
3. The `OPENROUTER_API_KEY` environment variable, used only for a Kimi role on OpenRouter and never saved.

`python3 scripts/build_release.py --local-keys config/keys.local.json` builds the public ZIP without the
key and a private one with it in `dist/private/` (git-ignored; owner-readable only on Linux/macOS).

**Without the key, or when OpenRouter cannot serve the role** - key refused (401/403), no credits (402),
unreachable, or no allowed provider (404 "No endpoints found") - the role continues on its fallback models
at `nvidia.kimi.fallback_base_url` (empty = NVIDIA's endpoint) with the NVIDIA key, never with the
OpenRouter key: `nvidia/nemotron-3-ultra-550b-a55b`, `z-ai/glm-5.3`,
`nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3-super-120b-a12b`. Kyora asks OpenRouter again
after a wait (unreachable 5 minutes, no provider 10 minutes, key or credits 30 minutes). The log
(`model.fallback`), the Kimi tile, every `model.*` event, the judge journal and self-check (WARN, naming
the model and host that answered) say so. When OpenRouter refuses only tool calls or images for this
provider, the role stays on Kimi K3.1 Pro and asks without a forced tool call (then without tools) or
without the images.

**Cost and data.** OpenRouter bills your key per token: listed at about $2 per million input and $10 per
million output tokens for this model on 2026-11-03, with reasoning tokens billed as output; see
[openrouter.ai/moonshotai/kimi-k3.1-pro](https://openrouter.ai/moonshotai/kimi-k3.1-pro) for today's
price. A run asks the Kimi role for intake, plan, judge verdicts and reviews; set
`kimi.judge_reasoning_effort` to `low` for fewer reasoning tokens per verdict. What you send to the role
(prompt, plan, attachments, screenshots) goes to OpenRouter and the pinned provider under their terms.

**Not measured from here.** The sandbox this change was made in cannot reach openrouter.ai, so Kyora's
tests use a local mock that speaks OpenRouter's request and streaming format, and no live OpenRouter call
has been measured yet. `kyora selfcheck` makes the first one on your device and shows the model that
answered, the `reasoning` object and provider routing sent, and the latency. CI makes it as well once the
repository secret `OPENROUTER_API_KEY` is set (job *live model APIs*; only that job gets the key).

## NVIDIA status (measured 2026-10-18)

Measured on 2026-10-18 against NVIDIA's API Catalog (`https://integrate.api.nvidia.com/v1`) with the keys
then in `config/keys.json`. NVIDIA can change any of this; self-check and
`python3 scripts/nvidia_diagnose.py --all-models` (which reports what each key can call, never the key)
show the current state.

* **Kimi K3 is retired on NVIDIA's free endpoint.** `moonshotai/kimi-k3` answers HTTP 410 (end of life
  2026-10-15) and `moonshotai/kimi-k2.6` answers HTTP 404 ("Not found for account"). Since 2026-11-03 the
  Kimi role runs Kimi K3.1 Pro on OpenRouter ([Kimi K3.1 Pro on OpenRouter](#kimi-k31-pro-on-openrouter));
  NVIDIA's endpoint serves its fallback models, in order `nvidia/nemotron-3-ultra-550b-a55b`,
  `z-ai/glm-5.3`, `nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3-super-120b-a12b`
  (`nvidia.kimi.fallback_models`), and Kyora sends each model its own controls (a Nemotron model gets its
  thinking switch, not Kimi's reasoning settings). A fallback is shown in the log (`model.fallback`), on
  the Kimi tile (the model that answered and "fallback from moonshotai/kimi-k3.1-pro"), in every
  `model.*` event (`requested_model`, `fallback: true`), in the judge journal (`judge_model`) and in
  self-check (WARN). Static labels (navigation, prompt help, the deck's architecture slide) name the
  role's model, Kimi K3.1 Pro. The role's model and endpoint are settings: another OpenAI-compatible
  endpoint (for example Moonshot's API `https://api.moonshot.ai/v1`) works with its own key in
  `nvidia.kimi.api_key` and its model id in `nvidia.kimi.model`. A key is only ever sent to the endpoint
  it is configured for: the Nemotron role and the Kyora agent engine use the NVIDIA key, never the
  OpenRouter key.
* **The keys changed on 2026-11-03.** On 2026-10-18 the key in the Nemotron slot was refused for
  inference (HTTP 403 "Authorization failed" for every model: its account lacked inference access) and
  the key in the Kimi slot answered. With the Kimi role on OpenRouter, the answering NVIDIA key moved to
  the Nemotron slot and the refused one was removed, so the Nemotron role, the Kimi role's fallbacks and
  the Kyora agent engine all use the key that answers. With two keys on the same endpoint, a refused one
  falls back to the other (`nvidia.share_keys`, on) and Kyora shows it (`model.fallback` with
  `kind: key`, WARN in self-check); a new key from [build.nvidia.com](https://build.nvidia.com) goes in
  Settings.
* **The NVIDIA key (then in the Kimi slot) answered 14 models**, among them `z-ai/glm-5.3`,
  `nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3-super-120b-a12b` and `poolside/laguna-xs-2.1`.
  On 2026-10-24 it answered 13: `nvidia/nemotron-3-super-120b-a12b` answered HTTP 404 ("not found for
  account") although `/models` still lists it, so the default fallback lists now put
  `nvidia/nemotron-3.5-super-vl-120b-a12b` (it answered, also with a function call) before it.
* **Nemotron-3-Ultra is often overloaded on the free tier**: HTTP 503 "Service temporarily overloaded",
  also as an error chunk inside a 200 stream. Kyora retries the request, then lets the model cool down
  for 120 s and the Nemotron role continues on `nvidia/nemotron-3.5-super-vl-120b-a12b` (then
  `nvidia/nemotron-3-super-120b-a12b`); after the cooldown it returns to Ultra. `model.fallback`
  says `temporary: true`.
* **A live UI-spec call once spent all 12288 tokens in thinking mode and arrived truncated.** Nemotron's
  design and UI-spec calls are now sampled at the model card's defaults (temperature 1.0, top_p 0.95);
  an answer that is cut off or unusable is asked for once more without thinking, as plain JSON text with
  the same budget, and then once from the next model of the Nemotron role's chain (`model.fallback`).
  The Kimi role's repair of a cut-off answer gets twice the budget (at most 65536) and effort `low`.

## Hub mode (phones and tablets)

Claude Code runs only on a PC (Windows, macOS, Linux). Hub mode lends a machine's code engine - its
Claude Code login, else its Anthropic API engine, else its Kyora agent engine - to phones and tablets.
A phone can also build on its own with the Kyora agent engine; the Hub is how a phone uses Claude:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 hub
```

The Hub binds `0.0.0.0`, creates a hub token and prints the LAN URL, a pairing link and a QR code.
Start Kyora on the phone, scan the QR code (or open the link) and the phone saves the Hub URL and
token. The phone then runs intake, plan, design, tests, gate, review, slides and deck itself and
sends only BUILD and FIX to the Hub (`/api/code-engine/*`, bearer token). The Hub judges the engine's
Write/Edit/Bash calls itself (local rules, then its own Kimi settings). The token grants use of the Hub's
Claude subscription, Anthropic key or NVIDIA quota (whichever its engine uses): share it only with your
own devices. Details: [`docs/hub-mode.md`](docs/hub-mode.md).

## Tokkio presenter

NVIDIA Tokkio 5.0.0-ga is vendored **unmodified** under `vendor/tokkio-5.0.0-ga/` (639 files,
sha256-verified: `python scripts/vendor_tokkio.py --verify`). It runs only on an **NVIDIA-GPU
Kubernetes cluster** with NGC containers - 2 GPUs for the 1-stream profiles, 4 for 3- and 6-stream -
deployed with NVIDIA's one-click scripts for AWS, AWS-lite, Azure, GCP or bare metal, which require
`NGC_CLI_API_KEY` and non-empty `NVIDIA_API_KEY`, `OPENAI_API_KEY` and `ELEVENLABS_API_KEY` secrets.
The Tokkio UI is then on port 30111 and the ACE Configurator on 30180. Kyora's overlay
(`tokkio-kyora/`) adds a frame processor that makes the avatar narrate Kyora's runs and show previews
in Tokkio's custom view, and points the avatar's LLM at Nemotron-3-Ultra:

```sh
python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180              # dry run
python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180 --apply \
    --events-url http://<kyora-lan-ip>:8765 --token <hub token> [--kubectl-fallback]
```

Kyora must be reachable from the cluster (`kyora hub` on the LAN/VPN, `hub.expose_ui` on). Without a
deployment the Presenter panel says **"Tokkio not deployed"**. Tokkio presents; it does not generate
UI. Guide: [`docs/tokkio-overlay.md`](docs/tokkio-overlay.md) and
[`tokkio-kyora/README.md`](tokkio-kyora/README.md).

## Kyora Bench

`kyora bench --list`, `kyora bench --tasks <ids> --dry-run` (estimate) and `kyora bench --tasks <ids>`
run the **real** pipeline on fixed game tasks with bench-owned browser tests. It costs NVIDIA tokens,
OpenRouter tokens (the Kimi role, billed to your key) and code-engine usage (Claude subscription,
Anthropic API tokens, or the Kyora agent engine's NVIDIA tokens), so it asks for confirmation (`--yes` to skip). A task passes when >= 94 % of the bench's own
tests pass and every required check passes (the threshold is fixed, whatever Settings say); the
"certified" badge appears only from a measured run on your setup in which one engine/model served every
engine call, shown with date, engine and model. This README contains no bench number on purpose. Rules:
[`docs/bench.md`](docs/bench.md).

## Settings and keys

Kyora reads `keys.json` (endpoints, secrets), `keys.local.json` (your private keys, merged over
`keys.json`; git-ignored) and `settings.json` (behaviour) from the first of: `--config DIR`,
`$KYORA_CONFIG_DIR`, `./config`, the `config/` folder next to `kyora.pyz`, and `KYORA_HOME/config`
(seeded from the copy packaged inside `kyora.pyz`). Runtime data lives in `KYORA_HOME` (default
`~/.kyora`). The *Settings* tab edits them; secrets are masked in the UI and saved to `keys.local.json`.
From the environment, `OPENROUTER_API_KEY` fills an empty key of a role on OpenRouter, `NVIDIA_API_KEY`
one of a role on an NVIDIA endpoint, and `ANTHROPIC_API_KEY` the Anthropic key; they are never saved.
Model endpoints, keys, models and fallbacks and the agent engine's settings apply without a restart;
`server.*` needs one.

> **Warning - the NVIDIA key is committed.** By your explicit decision, `config/keys.json` contains a
> real NVIDIA API Catalog key and is committed to this repository and packaged into `kyora.pyz` and
> the release ZIP. **Anyone with the repository, the ZIP or `kyora.pyz` can use that key and spend
> that quota.** If it leaks, revoke and rotate it at [build.nvidia.com](https://build.nvidia.com)
> and enter the new key in Settings. It is an NVIDIA key: it works for NVIDIA-hosted models (the
> Nemotron role, the Kimi role's fallbacks and the Kyora agent engine), not for Claude or OpenRouter.
> Your OpenRouter key is **not** committed ([Kimi K3.1 Pro on OpenRouter](#kimi-k31-pro-on-openrouter)).

Claude Code uses your **Claude subscription login** (`claude auth login`); Kyora needs no Anthropic
API key. The optional Messages-API engine activates only if you set `ANTHROPIC_API_KEY` (or
`anthropic.api_key`) **and** `pip install anthropic`. The Kyora agent engine needs neither: it uses the
NVIDIA key of the roles on its endpoint (never the OpenRouter key), or `agent.base_url` / `agent.api_key`
for another OpenAI-compatible endpoint (a key set there is sent only to that endpoint).

Important settings (`config/settings.json`): `server.port` (8765), `server.preview_port` (`null` =
port + 1, i.e. 8766; `kyora serve --preview-port N`), `server.allowed_hosts` (`[]`), `claude_code.model`,
`claude_code.fallback_models`, `claude_code.effort`, `claude_code.max_turns` (60),
`claude_code.timeout_s` (3600), `claude_code.command` (e.g. a wrapper for a proot-distro install) and
`claude_code.extra_args` (both edited only in `settings.json`, never over HTTP; restart Kyora after a
change), `engines.order` (`["hub", "claude_code", "anthropic_api", "openai_agent"]`), `engines.failover`
(`true`), `engines.auto_resume_on_limit` (`true`), `agent_engine.enabled` (`true`), `agent_engine.model`
(`z-ai/glm-5.3`), `agent_engine.fallback_models`, `agent_engine.max_turns` (80), `agent_engine.timeout_s`
(5400; all agent settings: [`docs/agent-engine.md`](docs/agent-engine.md#settings)),
`kimi.reasoning_effort` / `kimi.judge_reasoning_effort` (`max`),
`kimi.effort_field` (for a Kimi model on an NVIDIA-style endpoint; OpenRouter gets its `reasoning`
object instead: `top_level` sends `reasoning_effort`, Kimi's documented control there; `extra_body` sends
`chat_template_kwargs.reasoning_effort`, the top-level field an OpenAI SDK `extra_body` would carry, which
Kimi's template is not documented to read: best-effort, the model may ignore it),
`nemotron.thinking_for_design`,
`quality.min_pass_rate` (0.94), `quality.max_fix_iterations` (3), `quality.browser_wait_s` (1800),
`quality.require_plan_approval`, `hub.expose_ui`.
In `config/keys.json`, per role: `nvidia.<role>.model`, `.api_key`, `.base_url` (empty = `nvidia.base_url`)
and `.fallback_models`; for the Kimi role also `nvidia.kimi.fallback_base_url` (where its fallback models
are asked, with that endpoint's keys; empty = `nvidia.base_url`) and `nvidia.kimi.provider` (OpenRouter
provider routing, sent with the role's own model only); plus `nvidia.share_keys` (`true`: a role whose own
key is refused with 401/403 uses the other role's key on the same endpoint).

## Security model

Kyora is a local app: the API has no login for clients on the same machine, so the HTTP server
decides who counts as "the same machine" carefully.

* **Two origins.** The UI and the API are on `server.port` (8765). Generated and imported game code
  is untrusted, so it is served only by a second listener on `server.preview_port` (default port + 1,
  8766): `/preview/...`, the browser test harness `/runner/...`, its scripts and the two
  browser-results endpoints, nothing else. A game's `fetch('/api/settings')` reaches the preview
  listener, which has no such route. On the main port `/preview/...` and `/runner/...` only redirect
  to the preview port. The hub-token cookie is `HttpOnly`, so game code cannot read it.
* **Host allow-list (DNS rebinding).** A request whose `Host` header is not this machine is refused
  with `403 host not allowed`. Allowed: `localhost`, `127.0.0.1`, `::1`, the address the request
  arrived on, and, when Kyora listens beyond loopback (`kyora hub`, `--host 0.0.0.0`), this machine's
  LAN addresses, hostname, `<hostname>.local` and FQDN. Add other names to `server.allowed_hosts`
  (for example a reverse proxy, a WSL or Docker host name, or the name a Tokkio pod uses to reach
  Kyora).
* **Origin check.** A POST/PUT/DELETE that carries an `Origin` header must come from the listener's
  own origin; anything else, including `null`, gets `403 cross-origin request refused`. Clients that
  send no `Origin` (the judge hook, the hub client, the Tokkio overlay, curl) are unaffected. Kyora
  never sends `Access-Control-Allow-*` headers.
* **JSON must say so.** A JSON body must be sent as `Content-Type: application/json`, so a web page
  cannot post one as a CORS-free `text/plain` form.
* **No command execution over HTTP.** `claude_code.command` and `claude_code.extra_args` are the
  argv of a process Kyora runs, so `PUT /api/settings` refuses to change them; edit `settings.json`.
* **Engine tools.** The Anthropic API engine and the Kyora agent engine run shell commands with API
  keys, tokens and other secrets removed from the environment, and their file editor is confined to the
  workspace; each write and command passes the judge first.
* **Static files** are read only from `kyora/web`. A backslash counts as a path separator; `..`, `.`,
  empty segments, drive letters and streams (`:`) and NUL are rejected on every platform, and on a
  filesystem install the resolved file must stay inside `kyora/web`.
* **One Kyora per home.** `kyora serve`, `kyora hub` and `kyora bench` take a lock on
  `KYORA_HOME/kyora.lock` before they start, so a second Kyora on the same home (another port, a bench
  next to a running server) exits with a clear message instead of marking the first one's runs
  interrupted. On Windows the listeners also claim their ports exclusively (`SO_EXCLUSIVEADDRUSE`), so a
  second Kyora on the same port fails clearly instead of silently sharing it.

## Effort and models

* **Claude Code**: `claude -p ... --effort max --model claude-fable-5-1 --fallback-model
  claude-opus-5-5,claude-sonnet-5-5 --output-format stream-json --permission-mode acceptEdits
  --settings <run>/claude_settings.json ...` with the judge hook. Which models your account
  may use depends on your Claude plan; Kyora retries with the next model when one is refused and the
  UI shows the model that **actually** served each run (`engine.model_resolved`), never the requested
  one as if it had served. Details: [`docs/claude-code-engine.md`](docs/claude-code-engine.md).
* **Anthropic API engine** (`anthropic_api`): the same `claude_code.model`, `fallback_models` and
  `effort` on the Messages API, with Kyora's judged `bash` and file-editor tools.
* **Kyora agent engine** (`openai_agent`): `z-ai/glm-5.3`, then `nvidia/nemotron-3-ultra-550b-a55b`,
  `nvidia/nemotron-3.5-super-vl-120b-a12b` and `nvidia/nemotron-3-super-120b-a12b`, at `temperature` 0.6, `top_p` 0.95 (at most 1), at most 16384
  output tokens per answer, at most 80 turns. It has no Claude effort level; `agent_engine.thinking`
  (default off: in one live comparison on 2026-10-18 GLM-5.3 without thinking passed the gate 28/28,
  with thinking it had not finished BUILD after 85 minutes) is sent to the models that read a thinking
  switch (Nemotron, and GLM's switch-off). Details: [`docs/agent-engine.md`](docs/agent-engine.md).
* **Kimi K3.1 Pro** (`moonshotai/kimi-k3.1-pro` on OpenRouter, see
  [Kimi K3.1 Pro on OpenRouter](#kimi-k31-pro-on-openrouter)): temperature 1.0, top_p 1.0,
  `reasoning: {"enabled": true}` (effort max) and the provider routing `nvidia.kimi.provider` for
  planning, judging and reviewing. A fallback model gets its own controls instead: a Nemotron model
  `chat_template_kwargs.enable_thinking` (off for effort `low`), other families no effort field. Only
  Write, Edit, MultiEdit, NotebookEdit and Bash calls pass through the judge (Read, Glob and Grep are not
  judged). Local rules answer the obvious ones instantly (planned files, standard game paths such as
  `index.html`, `src/`, `assets/`, `tests/`, read-only commands; path escapes and destructive commands
  are denied), a per-run cache repeats earlier Kimi verdicts, and the Kimi role decides the rest. A Kimi
  verdict at max effort takes seconds; lower `kimi.judge_reasoning_effort` to `high` or `low` for faster
  builds. The Pipeline view's Judge card counts how many verdicts came from rules, the cache and Kimi.
* **Nemotron-3-Ultra** (`nvidia/nemotron-3-ultra-550b-a55b`, fallbacks
  `nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3-super-120b-a12b`): design and UI spec with
  thinking on (`nemotron.thinking_for_design`) at temperature 1.0, top_p 0.95; an answer that is cut off
  or unusable is asked for once more without thinking as plain JSON, then once from the next model of
  the chain. Narration with thinking off at temperature 0.2.
  Reference and published scores: [`docs/nemotron-reference.md`](docs/nemotron-reference.md).

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| Run stops in **BLOCKED: no code engine** | No engine is available: no Hub linked, Claude Code missing or not logged in, no Anthropic key, and the Kyora agent engine off (`agent_engine.enabled`) or without an NVIDIA key. Turn on the agent engine with an NVIDIA key, install Claude Code (`install.ps1`) and run `claude auth login`, add an Anthropic key, or link a Hub. Then press *Resume*. |
| `engine.failover` in the log | An engine stopped for an account or service reason (usage limit, subscription, credits, login, refused key, unreachable, retired model) and the same BUILD/FIX continues on the next engine. Nothing to do; the line names the reason. |
| **BLOCKED** "... hit a usage limit ... resets at <UTC time>" | No other engine was available (or `engines.failover` is off). Kyora resumes the run by itself a minute after the reset (`run.auto_resume`); press *Resume* to retry sooner, or make another engine available. |
| `claude auth status` says not logged in | Run `claude auth login` and choose your Claude subscription, or use one of the routes in [Without a Claude subscription](#without-a-claude-subscription). |
| Model restricted / not available (e.g. `claude-fable-5-1`) | Your account cannot use that model. Kyora falls back to `claude-opus-5-5`, then `claude-sonnet-5-5`, and shows which one served. Change `claude_code.model` / `fallback_models` in Settings to match your plan. |
| `engine.compat_retry` in the log, `unknown option '--max-turns'` | Your CLI version hides or lacks `--max-turns` / `--append-system-prompt-file`. Kyora retried once without them (own turn watchdog, inline system prompt). Nothing to do; updating Claude Code (`claude update`) removes the retry. |
| NVIDIA **401** in self-check | The key is missing, mistyped, expired or revoked. Create a new one at build.nvidia.com and enter it in Settings. |
| NVIDIA **403** "Authorization failed" | The key's account lacks inference access (measured on 2026-10-18 for the key then in the Nemotron slot, since removed). With two keys on the same endpoint and `nvidia.share_keys` on, the role uses the other one and self-check says WARN. Create a key at build.nvidia.com -> Get API Key on an account with inference access (or ask NVIDIA to enable it) and enter it in Settings. |
| NVIDIA **410** / **404** for a model | The model is retired (410, e.g. `moonshotai/kimi-k3` since 2026-10-15) or not served for this account (404). The role continues on its fallback models (WARN in self-check, `model.fallback` in the log). Change the model or `fallback_models`, or point the role at another endpoint. |
| NVIDIA **503** "Service temporarily overloaded" | The free tier is busy for that model. Kyora retried, then uses the next fallback model for 120 s and returns afterwards (`model.fallback`, `temporary: true`). If every model stays busy, the stage blocks with the reason; press *Resume* later. |
| Kyora agent engine: "model did not call the tool" | The full self-check's tool-call request was answered without a tool call. Choose another `agent_engine.model` or add fallback models that support function calling. |
| OpenRouter **402** / "Insufficient credits" | Your OpenRouter key has no credits left. Add credits at openrouter.ai/credits. Meanwhile the Kimi role answers with its fallback models on NVIDIA (WARN in self-check, `model.fallback` in the log) and Kyora asks OpenRouter again after 30 minutes. |
| OpenRouter **401** / **403**, or self-check: Kimi role "answered by the fallback model ..." | The OpenRouter key is missing, mistyped or revoked. Enter it in Settings -> *Kimi API key (OpenRouter)* (saved to `keys.local.json`), put it in `config/keys.local.json`, or set `OPENROUTER_API_KEY`; then re-run self-check. |
| OpenRouter **404** "No endpoints found ..." | The pinned provider (`nvidia.kimi.provider`, `allow_fallbacks: false`) cannot serve this request: the model is not available there right now, or the request needs a feature it lacks. For tool calls Kyora retries without a forced tool call, for images without the images; otherwise the role answers with its fallback models and asks OpenRouter again after 10 minutes. To let OpenRouter choose the provider, set `"provider": null` under `nvidia.kimi` in `keys.json` (an empty `{}` keeps the default routing); do the same when you change `nvidia.kimi.model` to a model that provider does not serve. |
| Kimi role **400** mentioning `reasoning_effort` (NVIDIA-style endpoint) | The gateway rejected the top-level field. Set Settings -> `kimi.effort_field` to `extra_body` (the effort is then sent as `chat_template_kwargs.reasoning_effort`) and re-run self-check, which names the field it sent and shows the provider's error body. "Accepted" means the gateway answered 200 to that body; a 1-token probe cannot show that the effort was applied. The `extra_body` mode is best-effort: Kimi's documented control there is the top-level `reasoning_effort`, its template is not documented to read the keyword, so the model may ignore it and run at its default effort (self-check and the `model.request` / `model.metrics` events say so). |
| `judge.unavailable` events | The judge could not be reached or the Kimi role failed; the tool ran (fail-open) and the run records it. Check the OpenRouter and NVIDIA keys and the network. |
| Tests wait for the browser / run BLOCKED after `quality.browser_wait_s` | The harness page needs an open browser tab on a device that can reach Kyora. Keep the Kyora tab open in Safari. |
| iSH: Safari does not load `http://127.0.0.1:8765` | iOS paused iSH (it pauses an app a few seconds after Safari comes to the front) and a paused iSH cannot answer. Kyora prevents that by holding iSH's location stream open: allow location access for iSH (*Allow While Using App*; Settings > Privacy & Security > Location Services > iSH if you refused) and restart Kyora. Wait for Kyora's `Open:` line before opening Safari, and type the whole address with `http://`. If iOS took the port away while iSH was paused, Kyora listens again within about ten seconds of running again; reload the page. Without location access, use Split View on an iPad (iSH and Safari side by side). |
| iSH is very slow | Expected: iSH emulates x86 in software. Let the first start finish, expect a local BUILD with the Kyora agent engine to be slow too (or build through a Hub), skip Bench there. |
| Phone cannot open the LAN link | Run `kyora hub` (binds 0.0.0.0), enable `hub.expose_ui`, allow Python through the PC firewall on private networks (ports 8765 and 8766), use the same Wi-Fi. |
| Preview stays blank on another device | Games come from the preview port (8766 by default, `preview_port` in `/api/status`). Let it through the firewall too, or set `server.preview_port`. |
| `403 host not allowed: <name>` | Kyora was reached by a name it does not know as its own (DNS rebinding protection). Use the IP address or `localhost`, or add the name to `server.allowed_hosts` in `settings.json` and restart. |
| `Kyora cannot listen on ...` at start | Another Kyora (or another program) already uses the port or the preview port. Stop it, or start with `--port N --preview-port M`. |
| PowerShell refuses to run the scripts | Start them with `powershell -NoProfile -ExecutionPolicy Bypass -File ...` as shown, or `Unblock-File .\launchers\*.ps1`. |

## Licences and attribution

* **Kyora's own code**: MIT ([`LICENSE`](LICENSE)).
* **Kimi K3.1 Pro** (Moonshot AI): used through OpenRouter with your key, under OpenRouter's and the
  serving provider's terms; nothing of it is vendored. The model's name is displayed in the UI. Kimi K3
  was used through NVIDIA's hosted API until NVIDIA retired it there on 2026-10-15 (its licence asks
  large commercial products to show the name "Kimi K3"; Kyora showed it while it used that model).
* **NVIDIA Nemotron-3-Ultra** and the other NVIDIA-hosted models Kyora calls (the fallback models and the
  Kyora agent engine's models, e.g. `z-ai/glm-5.3`, `poolside/laguna-xs-2.1`,
  `nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3-super-120b-a12b`): used through NVIDIA's API Catalog under NVIDIA's terms and the
  model providers' terms; nothing of them is vendored. The Nemotron developer repository is Apache-2.0
  and is not vendored.
* **Claude Code** (Anthropic): installed and logged in by you, used under Anthropic's terms. Nothing
  from the Claude Code repository is copied into Kyora. The optional Messages-API engine uses the
  official `anthropic` package, installed by you.
* **NVIDIA ACE / Tokkio 5.0.0-ga**: vendored unmodified in `vendor/tokkio-5.0.0-ga/`. The ACE
  repository and `src/llm-rag/LICENSE` are Apache-2.0, but several files carry an NVIDIA proprietary
  header; the discrepancy is recorded in [`vendor/VENDOR.md`](vendor/VENDOR.md). Tokkio's containers,
  charts and assets come from NGC under NVIDIA's licence terms and are not included.

## Development

```sh
python3 -m unittest discover -s tests -v          # unit tests (no network; mock NVIDIA server under tests/)
for f in tests/e2e/*.mjs; do node "$f"; done      # end-to-end browser tests (Node 22 + Playwright)
python3 scripts/build_release.py --check           # build build/kyora.pyz + dist/Kyora-AGI-Studio-<v>.zip and verify them
python3 scripts/vendor_tokkio.py --verify          # vendored Tokkio tree vs vendor/VENDOR.md
python3 scripts/ps1_lint.py                        # PowerShell launcher lint (no pwsh needed)
python3 scripts/nvidia_diagnose.py --all-models    # live: what each configured NVIDIA key can call (never prints keys)
```

Run the unit tests in a git checkout of this repository. In an unpacked release folder about ten of them
fail because the release leaves out files they read (`.gitattributes`, the test fixtures' `.kyora/`
folders, the release builder's inputs). Either way the tests never use your own keys: they drop
`OPENROUTER_API_KEY`, `NVIDIA_API_KEY` and `ANTHROPIC_API_KEY` from their environment and never copy
`keys.local.json`.

`.github/workflows/verify.yml` runs the unit tests on Windows, macOS and Linux, the PowerShell
launchers, Android Termux and 32-bit Alpine (the iSH userland), the browser e2e tests, the release
build, the live NVIDIA API, the live pipeline without Claude and the Tokkio overlay in the real
libraries; each job reports its own result.

Rules for contributors: standard library only inside `kyora/` (the optional `anthropic` package is
imported lazily by `kyora/models/anthropic_api.py`); no fake model output in product code (mocks only
under `tests/`); a missing capability is reported, never simulated. Changes: [`CHANGELOG.md`](CHANGELOG.md).
