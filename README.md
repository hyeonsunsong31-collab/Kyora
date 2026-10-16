# Kyora - AI game-development studio

Kyora is an **orchestration application** for making browser games with AI. It calls four external
systems over their own APIs, each in a fixed role: **NVIDIA Nemotron-3-Ultra** writes the design
(game design document, narrative, UI spec, copy, narration), **Moonshot Kimi K3** plans the work,
judges every action of the code engine in real time and reviews failed results, **Claude Code** (CLI,
`--effort max`, your Claude subscription) writes, tests and fixes the game, and **NVIDIA Tokkio**
(optional, on your own GPU cluster) presents the run as a talking avatar. Kyora runs the real tests
in a real browser and only calls a project finished when at least **94 % of that project's measured
checks pass**. It is not a merged model, it never fakes output, metrics or verdicts, and when a piece
is missing it says **BLOCKED** instead of pretending. The core is Python 3.10+ standard library only
and ships as one file, `kyora.pyz`, for Windows, Android (Termux) and iOS (iSH).

- [What Kyora is / is not](#what-kyora-is--is-not)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [First run](#first-run)
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
| An app that chains Nemotron-3-Ultra, Kimi K3, Claude Code and (optionally) Tokkio through their APIs, with real tool execution, builds and tests | A new or merged model. The weights cannot be combined: Nemotron-3-Ultra is 550B parameters, Kimi K3 2.8T, Claude is closed, Tokkio is a GPU Kubernetes workflow |
| A studio with projects, attachments, live progress, previews on your phone, per-project "slides" (every generation or modification is a git commit + tag you can preview, branch from, stack on and download as a ZIP) and an auto-built presentation deck | A system that invents results: no canned games, no simulated avatar, no estimated benchmark scores |
| A **measured 94 % quality gate** per project and a **Kyora Bench** whose 94 % threshold is the certification rule | A claim that any model scores 94 % (published: Nemotron-3-Ultra SWE-Bench Verified 71.9, Kimi K3 Terminal-Bench 2.1 88.3) |
| A display of **measured** time to first token, latency, token counts and tokens/s per model call | A TOPS number: TOPS describes data-centre chips and cannot be set or measured over an API |
| One `kyora.pyz` for Windows, Android/Termux and iOS/iSH, with honest per-platform limits | A Claude Code port to phones: there is no official Android or iOS build; phones use a Hub |

Full statement: [`docs/honesty.md`](docs/honesty.md).

## Architecture

```
            +------------- Kyora UI (vanilla HTML/JS, mobile-first, Server-Sent Events) -------------+
            | prompt + attachments | pipeline chart | live log | preview | slides | deck | settings  |
            | self-check | bench | presenter (Tokkio UI) | hub link (QR)                              |
            +-------------------------------------------+---------------------------------------------+
                                                        | HTTP + SSE (stdlib ThreadingHTTPServer)
+-------------------------------------------------------+---------------------------------------------+
| kyora.pyz (Python standard library only)                                                            |
|  pipeline: INTAKE -> PLAN -> DESIGN -> BUILD -> TEST -> GATE (>= 94 %) -> [REVIEW -> FIX]* -> FINALIZE |
|  models/:  nim_client (OpenAI-compatible SSE) | nemotron | kimi | claude_code | anthropic_api | tokkio |
|  quality:  static checks + Node checks + in-browser acceptance tests -> gate                         |
|  judge:    Claude Code PreToolUse hook -> POST /api/judge -> rules / cache / Kimi K3 verdict         |
|  projects: store | slides (git or snapshot) | deck | zip      hub: /api/code-engine/* (LAN, token)  |
+---------+------------------------+---------------------------+-------------------------+-----------+
          |                        |                           |                         |
   Nemotron-3-Ultra            Kimi K3                   Claude Code CLI           NVIDIA Tokkio 5.0.0-ga
   (NVIDIA API Catalog)        (NVIDIA API Catalog)      (subscription login,      (your GPU k8s cluster;
   design, UI spec, copy,      intake, plan, tests,      --effort max)             overlay processor reads
   narration                   judge, review             code, tests, fixes        Kyora's event stream)
```

Contracts (routes, events, schemas): [`docs/architecture.md`](docs/architecture.md). Code engine and
judge: [`docs/claude-code-engine.md`](docs/claude-code-engine.md). UI: [`docs/ui-design.md`](docs/ui-design.md).

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
http://127.0.0.1:8765.

**Android** (Termux; everything except Claude Code, which has no official Android build):

```sh
sh launchers/install-termux.sh          # python, git, nodejs-lts (--with-claude: optional attempt)
sh launchers/kyora-termux.sh selfcheck
sh launchers/kyora-termux.sh            # serves and opens the UI; link a PC Hub for BUILD
```

**iOS / iPadOS** (iSH; everything except Node and Claude Code; slow x86 emulation):

```sh
sh launchers/install-ish.sh             # python3, git
sh launchers/kyora-ish.sh               # "Open Safari at http://localhost:8765"; link a PC Hub for BUILD
```

**Anywhere with Python 3.10+**: `python3 kyora.pyz serve --open` (or `python3 -m kyora serve --open`
in a source checkout).

## First run

1. **Self-check** - `kyora.ps1 selfcheck` (or the *Self-check* tab) measures, on your device: Python,
   platform, Git, Node, the Claude Code version and login state, Nemotron-3-Ultra and Kimi K3
   reachability with latency, the optional Anthropic engine, the selected code engine, Tokkio and Hub
   reachability, and the LAN URLs. Fix anything marked FAIL first.
2. **Create a project** - *New*, give it a name. Optionally drop attachments: reference images (Kimi
   K3 looks at them), design notes, code, or a game ZIP to import as the base.
3. **Run** - write the prompt ("a one-button endless runner with a day/night cycle, touch controls")
   and press *Run pipeline*. The pipeline chart shows each stage with measured times; the live log
   streams model output, Claude Code's tool calls and every Kimi K3 verdict; the metrics tiles show
   time to first token, tokens/s and token counts per model.
4. **Test and gate** - when BUILD finishes, the browser test harness runs the acceptance tests in your
   browser (keep the tab open). The gate shows `passed / total` and the pass rate against 94 %. Below
   94 %, Kimi K3 reviews and Claude Code fixes, up to 3 rounds.
5. **Preview on your phone** - *Open on another device* shows a LAN link and QR code (the PC must run
   `kyora hub` or `serve --host 0.0.0.0` with *Settings -> hub.expose_ui* on, and the phone must be on
   the same network).
6. **Download** - every finished run becomes a **slide** (`slide/v1`, `v2`, ...). From the slide strip
   you can preview any slide, branch from it, stack a modification on it (*Modify current slide*) or
   download it as a ZIP.
7. **Open deck** - each project has an auto-generated HTML presentation (concept, design tokens,
   screenshots, test and gate results, changelog, attribution; print it to PDF from the browser).
   It is written into the game as `kyora/deck.html`, so slide ZIPs carry it.

## Hub mode (phones and tablets)

Claude Code runs only on a PC (and on Linux/macOS). Hub mode lends the PC's code engine to phones:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 hub
```

The Hub binds `0.0.0.0`, creates a hub token and prints the LAN URL, a pairing link and a QR code.
Start Kyora on the phone, scan the QR code (or open the link) and the phone saves the Hub URL and
token. The phone then runs intake, plan, design, tests, gate, review, slides and deck itself and
sends only BUILD and FIX to the Hub (`/api/code-engine/*`, bearer token). The Hub judges every tool
call with its own Kimi K3 settings. The token grants use of your Claude Code and NVIDIA quota: share
it only with your own devices. Details: [`docs/hub-mode.md`](docs/hub-mode.md).

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
run the **real** pipeline on fixed game tasks with bench-owned browser tests. It costs NVIDIA tokens
and Claude usage, so it asks for confirmation (`--yes` to skip). A task passes at >= 94 % of its
checks; the "certified" badge appears only from a measured run on your setup, shown with date,
engine and model. This README contains no bench number on purpose. Rules:
[`docs/bench.md`](docs/bench.md).

## Settings and keys

Kyora reads `keys.json` (endpoints, secrets) and `settings.json` (behaviour) from the first of:
`--config DIR`, `$KYORA_CONFIG_DIR`, `./config`, the `config/` folder next to `kyora.pyz`, and
`KYORA_HOME/config` (seeded from the copy packaged inside `kyora.pyz`). Runtime data lives in
`KYORA_HOME` (default `~/.kyora`). The *Settings* tab edits both files; secrets are masked in the UI.
`NVIDIA_API_KEY` and `ANTHROPIC_API_KEY` from the environment fill empty key slots.

> **Warning - the NVIDIA keys are committed.** By your explicit decision, `config/keys.json` contains
> real NVIDIA API Catalog keys and is committed to this repository and packaged into `kyora.pyz` and
> the release ZIP. **Anyone with the repository, the ZIP or `kyora.pyz` can use those keys and spend
> that quota.** If they leak, revoke and rotate them at [build.nvidia.com](https://build.nvidia.com)
> and enter the new key in Settings. These keys work for Nemotron and Kimi K3 only, not for Claude.

Claude Code uses your **Claude subscription login** (`claude auth login`); Kyora needs no Anthropic
API key. The optional Messages-API engine activates only if you set `ANTHROPIC_API_KEY` (or
`anthropic.api_key`) **and** `pip install anthropic`.

Important settings (`config/settings.json`): `server.port` (8765), `claude_code.model`,
`claude_code.fallback_models`, `claude_code.effort`, `claude_code.max_turns` (60),
`claude_code.timeout_s` (3600), `claude_code.command` (e.g. a wrapper for a proot-distro install),
`claude_code.extra_args`, `kimi.reasoning_effort` / `kimi.judge_reasoning_effort` (`max`),
`kimi.effort_field` (`top_level` sends `reasoning_effort`; `extra_body` sends
`chat_template_kwargs.reasoning_effort`, the top-level field an OpenAI SDK `extra_body` would carry),
`nemotron.thinking_for_design`,
`quality.min_pass_rate` (0.94), `quality.max_fix_iterations` (3), `quality.browser_wait_s` (1800),
`quality.require_plan_approval`, `hub.expose_ui`.

## Effort and models

* **Claude Code**: `claude -p ... --effort max --model claude-fable-5-1 --fallback-model
  claude-opus-5-5,claude-sonnet-5-5 --output-format stream-json --permission-mode acceptEdits
  --settings <run>/claude_settings.json ...` with the Kimi K3 judge hook. Which models your account
  may use depends on your Claude plan; Kyora retries with the next model when one is refused and the
  UI shows the model that **actually** served each run (`engine.model_resolved`), never the requested
  one as if it had served. Details: [`docs/claude-code-engine.md`](docs/claude-code-engine.md).
* **Kimi K3** (`moonshotai/kimi-k3`): temperature 1.0, top_p 1.0, `reasoning_effort` max for
  planning, judging and reviewing. Judging every tool call at max effort is slow (seconds per call);
  lower `kimi.judge_reasoning_effort` to `high` or `low` for faster builds. Local rules and a verdict
  cache answer obvious cases instantly.
* **Nemotron-3-Ultra** (`nvidia/nemotron-3-ultra-550b-a55b`): thinking on (temperature 1.0, top_p
  0.95) for design and UI spec, off (temperature 0.2) for narration. Reference and published scores:
  [`docs/nemotron-reference.md`](docs/nemotron-reference.md).

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| Run stops in **BLOCKED: no code engine** | No Claude Code on PATH, Claude Code not logged in, no Hub linked and no Anthropic key. Install Claude Code (`install.ps1`), run `claude auth login`, or link a Hub. Then press *Resume*. |
| `claude auth status` says not logged in | Run `claude auth login` and choose your Claude subscription. |
| Model restricted / not available (e.g. `claude-fable-5-1`) | Your account cannot use that model. Kyora falls back to `claude-opus-5-5`, then `claude-sonnet-5-5`, and shows which one served. Change `claude_code.model` / `fallback_models` in Settings to match your plan. |
| `engine.compat_retry` in the log, `unknown option '--max-turns'` | Your CLI version hides or lacks `--max-turns` / `--append-system-prompt-file`. Kyora retried once without them (own turn watchdog, inline system prompt). Nothing to do; updating Claude Code (`claude update`) removes the retry. |
| NVIDIA **401** / "invalid key" in self-check | The NVIDIA key is wrong, revoked or out of credits. Create a new one at build.nvidia.com and enter it in Settings (both Nemotron and Kimi K3 slots). |
| Kimi K3 **400** mentioning `reasoning_effort` | The gateway rejected the top-level field. Set Settings -> `kimi.effort_field` to `extra_body` (the effort is then sent as `chat_template_kwargs.reasoning_effort`) and re-run self-check, which names the field it sent and shows the provider's error body. "Accepted" means the gateway answered 200 to that body; a 1-token probe cannot show that the effort was applied. |
| `judge.unavailable` events | The judge could not be reached or Kimi K3 failed; the hook let the tool run (fail-open) and the run records it. Check the NVIDIA key and network. |
| Tests wait for the browser / run BLOCKED after `quality.browser_wait_s` | The harness page needs an open browser tab on a device that can reach Kyora. Keep the Kyora tab open (on iOS keep Safari and iSH in the foreground). |
| iSH is very slow | Expected: iSH emulates x86 in software. Let the first start finish, keep iSH in the foreground, do BUILD through a Hub, skip Bench there. |
| Phone cannot open the LAN link | Run `kyora hub` (binds 0.0.0.0), enable `hub.expose_ui`, allow Python through the PC firewall on private networks, use the same Wi-Fi. |
| PowerShell refuses to run the scripts | Start them with `powershell -NoProfile -ExecutionPolicy Bypass -File ...` as shown, or `Unblock-File .\launchers\*.ps1`. |

## Licences and attribution

* **Kyora's own code**: MIT ([`LICENSE`](LICENSE)).
* **Kimi K3** (Moonshot AI): used through NVIDIA's hosted API. The name "Kimi K3" is displayed in the
  UI, as the Kimi K3 License asks of large commercial products (Kyora shows it always).
* **NVIDIA Nemotron-3-Ultra**: used through NVIDIA's API Catalog under NVIDIA's terms; the Nemotron
  developer repository is Apache-2.0 and is not vendored.
* **Claude Code** (Anthropic): installed and logged in by you, used under Anthropic's terms. Nothing
  from the Claude Code repository is copied into Kyora.
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
```

Rules for contributors: standard library only inside `kyora/` (the optional `anthropic` package is
imported lazily by `kyora/models/anthropic_api.py`); no fake model output in product code (mocks only
under `tests/`); a missing capability is reported, never simulated. Changes: [`CHANGELOG.md`](CHANGELOG.md).
