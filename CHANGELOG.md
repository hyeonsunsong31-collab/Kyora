# Changelog

All notable changes to Kyora. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Changed - 2026-10-23: resilience and honest attribution

- **Every surface names the role and the model that answered.** NVIDIA retired Kimi K3 on 2026-10-15, so
  its fallback models answer the Kimi role there. The pipeline chart, metrics tiles, log lines, plan
  notice, judge counters and chips, the deck and the Honesty card name the role ("Kimi role", "Nemotron
  role") and the model that answered, not a model that did not. The deck no longer labels an
  agent-engine build "Claude Code" (a long label was replaced by that name); after an engine failover
  each engine is listed with its own model. The judge verdict, the review result and
  `run.extra.role_models` record the model that answered each call. The model prompts no longer tell a
  fallback model that it is Kimi K3, and the FIX prompt says who asked for the fixes.
- **Screenshots go only to models that take images.** Live on 2026-10-18 Nemotron-3-Ultra answered a
  review with screenshots with an error 500 inside a 200, and GLM-5.3 with HTTP 400; both answer the same
  request as text. A model without vision gets one line saying the images were left out (and a system
  prompt that does not mention them); a request with images that fails is sent once more without them,
  without marking the model busy. The review records `images_sent`.
- **A review no model can give no longer stops the run.** With the gate measured and fix iterations left,
  the failing checks (required first) become the fix instructions of a `fix` decision with
  `source: "rules"` and `model: null`; the log says "review by rules (no model answered)".
- **Temporary failures resume by themselves.** An unreachable endpoint, 429 or 5xx after the client's
  retries and the role's fallbacks blocks the stage with an automatic resume after 120 s. A code engine
  stopped by a rate limit (HTTP 429 without credit or billing wording, measured live on 2026-10-23: the
  NVIDIA free tier answered 429 for minutes and gave no reset time) or an outage, with no other engine
  left, blocks with an automatic resume after 2, 5, 15, then 30 minutes; the resumed run uses that engine
  again. At most 24 automatic resumes per run. A connection reset or DNS failure before any answer is
  retried by the client.
- The live CI pipeline runs only the chosen default set-up, one per branch at a time (a new push cancels
  the older live run): the three set-ups of every push shared one free-tier key.
- **Keys never go to another endpoint.** A role's key is sent to the agent endpoint only when the role
  uses that endpoint (a Moonshot key in the Kimi slot never reaches NVIDIA). Endpoint and keys change in
  one step in Settings, and a retry whose endpoint changed meanwhile is not resent. A role whose own key
  is empty uses the shared key at once (`nvidia.share_keys`), and self-check says so.
- **Settings apply live** to Kimi's effort and effort field and to Nemotron's design thinking (they were
  saved but only applied after a restart).
- **Self-check** reports the selected agent engine as failing when its live probe fails, and then keeps
  Claude Code's missing CLI or login a failure instead of "not needed". The agent probe sends the run's
  temperature and top_p. `agent_engine.top_p` must be greater than 0 and at most 1.
- **Agent engine**: bash and the Node logic tests run with an allowlisted environment, bounded output, the
  run's cancel and deadline, and their output is redacted; `kyora_check`'s Node run is judged; tool calls
  written as text move to the next model, then fail as a model failure; a `Retry-After` above 60 s fails
  over at once; a refused `max_tokens` increase after a thought cut off is reported as such; the
  background-process note says what holds on Windows.
- **Judge**: heredoc bodies are not read as paths; Kyora's key files and config directory are denied to
  every engine (Claude Code also through `--settings` deny rules), and a search rooted above them too.
- **Engine failover**: only a quota failure is waited out; a reset already past waits a back-off; the
  highest-ranked usable engine is taken again; a hard kill keeps and announces only what the attempt
  changed. Process sessions are killed before their leader is reaped, so a reused PID is never signalled.
- The metrics tile drops "fallback from X" when X answers again.

### Added - 2026-10-18: working without a Claude subscription

- **The Kyora agent engine** (engine name `openai_agent`, `kyora/models/openai_agent.py`): BUILD and FIX
  without Claude, as Kyora's own tool loop on an OpenAI-compatible endpoint - by default NVIDIA's API
  Catalog with the NVIDIA keys, model `z-ai/glm-5.3`, then `nvidia/nemotron-3-ultra-550b-a55b`,
  `poolside/laguna-xs-2.1`, `nvidia/nemotron-3-super-120b-a12b`. Tools: the judged `bash` (fresh shell in
  the workspace, secrets scrubbed from its environment, Git Bash on Windows, not offered when no POSIX
  shell exists), the judged file editor confined to the workspace (with backups outside it) and
  `kyora_check` (the TEST stage's static checks and Node logic tests on the work tree). Nudges when
  `index.html` or planned files are missing, "continue" after a cut-off answer, a character budget that
  elides old tool output without breaking tool-call pairing, max turns and a wall-clock limit, key and
  model fallbacks, overload cooldowns, a model that refuses function tools skipped. Failures carry
  `failure_kind` (`auth`, `quota`, `unavailable`, `model`) for engine failover. It runs a different
  model: its capability is not claimed equal to Claude's, and the gate measures its games exactly like
  every other engine's. Settings `agent_engine.*` and `keys.agent {base_url, api_key}`; docs:
  `docs/agent-engine.md`. The shared tool loop moved to `kyora/models/agent_tools.py`
  (`ToolLoopEngine`); the Messages-API engine subclasses it unchanged.
- **Engine order and failover.** `settings.engines.order` (default `hub, claude_code, anthropic_api,
  openai_agent`) decides which engine runs a BUILD/FIX. `models.base.classify_failure` tells account and
  service failures (usage or session limit, ended subscription, credits, login, refused keys, unreachable
  or overloaded service, retired model) from task failures; with `engines.failover` (on) the same
  BUILD/FIX continues on the next available engine, told to continue the partial work when the stopped
  engine had changed files. Published as `engine.failover`; every attempt is recorded in `run.json`
  (`engine_runs`, `engine_failures`, `engine_failovers`). Task failures never fail over, and TEST/GATE
  measure the result as always.
- **Automatic resume after a usage limit.** When no engine is left (or failover is off) and the engine
  named its reset time (`reset_time`: `resets_at`, Claude Code's rate-limit `resetsAt`, texts such as
  "resets 1:20pm (UTC)", "resets at <ISO>", "try again in 3h 20m"), the run blocks with the reset in UTC,
  the partial work is committed (`partial_work_kept`) and the run manager resumes it a minute after the
  reset (`engines.auto_resume_on_limit`, on; `run.auto_resume`, `auto_resume_at`, `auto_resumes`).
  Timers survive a restart, re-check the clock at least every 10 minutes, stop after 24 automatic
  resumes of a run and are dropped by Resume or Cancel.
- **Claude Code session continuation**: a run that comes back to Claude Code for the same BUILD/FIX and
  iteration after its usage limit continues the limited session (`--resume`) with "Continue the task
  from where you stopped."; an expired session falls back to a fresh one told about the kept work.
  `claude_code.py` records `failure_kind` and `resets_at`.
- **NVIDIA key and model fallbacks.** `NIMClient` takes labelled alternate keys: a key refused with
  401/403 is replaced by the other role's key on the same endpoint (`nvidia.share_keys`, on), sticky,
  published as `model.fallback {kind: "key"}` with labels only. `ModelChain` +
  `call_with_model_fallback`: a role's model the endpoint reports gone (404/410) is skipped for its
  `fallback_models`, one that stays overloaded after retries cools down for 120 s behind the healthy
  ones (`model.fallback {kind: "model", temporary}`); every `model.*` event names the model that was
  called and, for a fallback, the requested one. Per-role `nvidia.<role>.base_url` (for example an
  endpoint that still serves Kimi K3) and `fallback_models`. `explain_http_failure` turns 401, 402, 403,
  404, 410, 429 and 5xx into what they mean and what to do (`model.error.hint`, self-check). Fallback
  chains from the 2026-10-18 measurement: Kimi role -> `nvidia/nemotron-3-ultra-550b-a55b`,
  `z-ai/glm-5.3`, `nvidia/nemotron-3-super-120b-a12b`; Nemotron role ->
  `nvidia/nemotron-3-super-120b-a12b`, `nvidia/nemotron-3.5-super-vl-120b-a12b`.
- **Free-tier resilience.** An error object inside a 200 stream that arrives before any output (NVIDIA's
  "Service temporarily overloaded", seen on 2026-10-18) is retried like an HTTP 503; one after output is
  raised, never retried (it would repeat the output). Errors record how many attempts were made, so
  messages claim only retries that happened.
- **Self-check**: new item "Kyora agent engine (no Claude needed)" (availability; with the full check one
  real tool-call request: WARN when the model answers without calling the tool, FAIL with the provider's
  explanation for refused keys or gone models). A role that answers only through a fallback key or model
  is WARN and says which; a refused key or a gone model without a working fallback is FAIL. A missing
  Claude Code CLI is WARN (not FAIL) while the agent engine is the selected engine, SKIP while a Hub or
  the Messages-API engine is.
- **UI**: engine choice "Kyora agent (NVIDIA models, no Claude)"; the *Auto* label follows
  `engines.order`; Settings groups for the agent engine, the engine order/failover/auto-resume and the
  per-role NVIDIA endpoints, fallback models and key sharing (with range checks); log lines for
  `model.fallback`, `engine.failover` and `run.auto_resume`; the code-engine tile is named after the
  backend that actually ran (Kyora agent: "not a Claude model", "cost: not reported").
- A hub runs jobs on the Kyora agent engine when neither Claude Code nor the Messages-API engine is
  available on it.
- **Real-platform verification**: `.github/workflows/verify.yml` runs the unit tests on Windows, macOS and
  Linux (Python 3.10 and 3.13), the PowerShell 5.1 and 7 launchers, Android Termux (bionic, real `pkg`),
  32-bit Alpine (the iSH userland, real `apk`), the browser e2e tests, the release build, the live NVIDIA
  API (`scripts/nvidia_diagnose.py`, `scripts/live_nvidia_check.py`), the live pipeline without Claude
  (`engines.order = ["openai_agent"]`) and the Tokkio overlay in the real `nvidia-pipecat` 0.2.0 /
  `pipecat-ai` 0.0.68 libraries (`scripts/tokkio_overlay_live_check.py`,
  `scripts/tokkio_bot_import_check.py`). `scripts/live_pipeline_check.py` runs one game end to end with
  only the agent engine allowed; `scripts/nvidia_diagnose.py --tool-probe` asks each listed model for one
  function call. Each job reports its own result.

### Changed - 2026-10-18

- **No engine means BLOCKED only when the Kyora agent engine is unavailable too** (switched off, or no key
  for its endpoint). With the committed NVIDIA keys a phone (Termux, iSH) or a PC without Claude Code
  builds locally on the agent engine when its endpoint is reachable.
- The Kimi role sends each model its own controls: Kimi's `reasoning_effort` only to a Kimi model, the
  thinking switch (`chat_template_kwargs.enable_thinking`, off for effort `low`) to a Nemotron fallback,
  no effort field to other families; `effort_sent` says which value was sent.
- **A cut-off or unusable model answer is asked for again.** Live on 2026-10-18 a Nemotron UI spec in
  thinking mode spent all 12288 tokens and arrived without its `tokens` object, and a forced-tool retry at
  temperature 0.2 repeated itself. Nemotron's design and UI-spec calls are now sampled at the model card's
  defaults (temperature 1.0, top_p 0.95); an answer that is truncated (`finish_reason: length`) or invalid
  is asked for once more without thinking as plain JSON text (no forced tool call, same budget), then once
  the same way from the next model of the role's chain, published as `model.fallback`. The Kimi role's
  repair after a cut-off answer gets twice the budget (at most 65536) and effort `low`.
- NVIDIA endpoints, keys, models and fallbacks changed in Settings apply without a restart (they needed
  one before, silently).
- The metrics tile is chosen by the role: a Kimi role answered by a Nemotron fallback model stays on the
  Kimi K3 tile, which shows the answering model and "fallback from moonshotai/kimi-k3"; key notes show on
  the tiles too.
- README, `docs/honesty.md`, `docs/platforms.md`, `docs/architecture.md` and `docs/hub-mode.md` describe the
  three routes without a Claude subscription, the engine order, failover and automatic resume, every
  substitution and how it is shown, and NVIDIA's state as measured on 2026-10-18.

### Fixed - 2026-10-18

- **Windows** (GitHub runner, PowerShell 5.1, Python 3.10/3.13/3.14): content types no longer come from
  the Windows registry (`.zip` came back as `application/x-zip-compressed`, `.js` can be `text/plain`,
  which browsers refuse for module scripts); `kyora/util/mime.py` answers from Kyora's table and a
  registry-free `MimeTypes`, and ZIP uploads under the Chrome/Edge alias are treated as `application/zip`.
  The headless test driver's output is decoded as UTF-8, not in the locale's code page. Tests remove their
  temp dirs after the app and lock cleanups (Windows cannot delete a held lock or an open SQLite file),
  handle 8.3 short temp paths, decode Node output as UTF-8 and no longer assert a POSIX-only ZIP name.
  `scripts/vendor_tokkio.py` prints POSIX paths.
- **Termux**: Android has no `/bin/sh` or `/usr/bin/env`. When a process starts without `termux-exec`,
  `kyora.util.procs` runs a script whose shebang names a missing `/bin/...` or `/usr/bin/...` interpreter
  through `$PREFIX/bin` (npm's `claude` shim is `#!/usr/bin/env node`). `install-termux.sh` no longer ends
  with a bare exit 127 when `node` is missing after the install; installer test stubs name the real shell.
- **macOS** (GitHub runner): `GET /api/status` resolved this host's name on every call and mDNS made that
  so slow that the UI, the release check and several tests timed out. LAN addresses come from the default
  route; name lookups run with a 1 s budget and are cached. Kyora's HTTP server (and the test mock) bind
  without the reverse lookup `http.server` runs between `bind()` and `listen()`, which on the macOS runner
  took long enough for connections to time out. `node --check` errors kept no line number because Node
  prints the `/private/var/...` real path of the temp dir; both forms are mapped back.
- **iSH userland**: `tests/test_procs.py` used a renamed symlink to `sh` as a stand-in interpreter, which
  BusyBox does not run as a shell (it picks its applet from the name it is started under); Python stands
  in now.
- **Hub expiry**: `expire(hours=0)` also expires a job that finished within the clock's resolution (failed
  on the Windows runner with Python 3.10, where Windows' coarse clock made such a job look newer than the
  cutoff).
- **CI exit codes**: the Termux and Alpine jobs piped the unit tests into `tail`, which hid two Termux
  failures behind a green job; the test exit code is kept now. The Windows self-check step no longer
  fails on the expected exit code 1 of a runner without Claude Code.
- **Live NVIDIA check**: it reported "Nemotron narration OK" for an empty answer and skipped Nemotron
  entirely when Kimi failed; a probe whose 1-token chat failed now fails the check. The agent-engine
  self-check item is judged by its own status (it fails only on FAIL), not by `chat_ok`.
- **A usage-limit reset already in the past** (a stale value, clock skew) no longer lets TEST/GATE grade
  the half-finished work as the result: the run blocks and resumes after the usual grace (still at most
  24 automatic resumes).
- `POST /api/projects/{id}/runs` rejected `engine_pref: "openai_agent"` with 400; the allowed values now
  come from the engine table, so every engine can be requested for a run. Narration names the engine a
  Hub really runs.
- The release check starts the server unbuffered and, when `GET /api/status` does not answer, prints the
  server's output, its start-up time, whether the process runs and what `lsof` sees on the port.

### Known limitations - 2026-10-18

- Measured on 2026-10-18: `moonshotai/kimi-k3` is retired on NVIDIA's free endpoint (HTTP 410, end of life
  2026-10-15) and `moonshotai/kimi-k2.6` answers 404, so the Kimi role runs on its fallback models there.
  The real Kimi K3 needs an endpoint that serves it (`nvidia.kimi.base_url` with that endpoint's key and
  model id). Since 2026-10-23 the UI, the log and the deck name the role and the answering model.
- Measured on 2026-10-18: the committed key in the Nemotron slot gets HTTP 403 "Authorization failed" for
  every model (its account lacks inference access); both roles use the Kimi-slot key through
  `nvidia.share_keys`. Nemotron-3-Ultra is often overloaded on the free tier.
- The Kyora agent engine runs a different model than Claude. No capability comparison is claimed or
  published; each game passes or fails the same measured gate, possibly after more fix rounds.
- When every model of a role stays overloaded or unreachable, the stage blocks and resumes by itself after
  120 s (since 2026-10-23); a refusal (401/403, 400) waits for Resume. A code engine's rate limit or outage
  resumes by itself after a back-off (2 to 30 minutes); a credits or billing failure waits for Resume.

### Security

- Game code runs on its own origin: a second listener on `server.preview_port` (default port + 1,
  `kyora serve --preview-port N`) serves only `/preview/...`, the test harness `/runner/...`, its
  scripts and the browser-results endpoints. On the main port those paths redirect there, the UI
  frames games from that origin, accepts test-result messages only from it and asks the new
  `GET /api/projects/{id}/preview-check` whether a ref has an `index.html`. `/api/status` reports
  `preview_port` and `preview_url`. Previously a generated or imported game ran on the API's origin
  and could read every key and change the engine command.
- DNS rebinding: requests whose `Host` is not this machine get `403` (new `server.allowed_hosts` for
  extra names); state-changing requests with a foreign `Origin` get `403`; JSON bodies must be sent
  as `application/json`.
- `claude_code.command` and `claude_code.extra_args` can no longer be changed through
  `PUT /api/settings` (the Settings tab shows them read-only); edit `settings.json` instead.
- The `kyora_token` cookie is `HttpOnly`; main-origin HTML is sent with `X-Frame-Options: SAMEORIGIN`.
- `GET /static/...` treats a backslash as a path separator and rejects `..`, drive letters, streams and NUL,
  so a Windows source install can no longer be made to read files outside `kyora/web` (for example
  `config/keys.json`).
- **The judge no longer waves through program-spawning "read-only" shell commands.** A segment with an
  environment assignment (`export GIT_CONFIG_*=`, `GIT_EXTERNAL_DIFF=`, `GIT_PAGER=`, `LD_PRELOAD=`,
  `PATH=`, `BASH_ENV=`, `NODE_OPTIONS=`, a bare `NAME=value`, ...) and tool flags that run another
  program or write a file (`rg --pre`/`--hostname-bin`, `sort --compress-program`/`-o`, `git -c`/
  `--config-env`/`--ext-diff`/`--textconv`, `find -exec`/`-ok`/`-delete`/`-fprint*`, `node`/`py_compile`
  with code-loading flags, output redirections, `less`/`man`/`xargs`/`awk`/`sed`/`tar --to-command`, ...)
  are no longer classed read-only; they defer to Kimi. Previously a command such as
  `export GIT_CONFIG_COUNT=1; export GIT_CONFIG_KEY_0=core.fsmonitor; export GIT_CONFIG_VALUE_0=...; git status`
  was approved as "read-only" while git ran the injected program outside the workspace. Genuinely
  read-only commands stay on the instant path.
- `rg -z`/`--search-zip` (also inside a short-flag cluster such as `-nz`) is no longer classed
  read-only: it spawns external decompressors, so it now defers to Kimi like `rg --pre`.
- **The judge hook reaches the judge over a LAN bind.** `POST /api/judge` is authenticated by the
  per-run hook token (constant-time compare) instead of requiring a loopback peer, so running
  `kyora serve`/`hub --host <lan-ip>` no longer makes each judged call get a 403 at the route and run unjudged;
  the hook now also fails **closed** (blocks the tool) on a 401/403, which means the server rejected the
  token, not that the judge is down.
- Windows ZIP import could write into `.git` through its 8.3 short name: an entry `GIT~1/config` (with a
  `[filter]` driver) reached `<repo>\.git\config` and ran at the next `git add -A`. Game ZIP imports and
  the Hub's unzip now refuse components that alias `.git` (`GIT~<n>`, `GI7EBA~<n>`, `.git` with trailing
  dots or spaces, any case) and entries that resolve into a `.git` directory; on Windows also any
  component ending in a dot or space.

### Fixed

- Windows: a second Kyora on the same port no longer starts silently next to the first one (and marks
  its running builds interrupted); listeners claim their ports with `SO_EXCLUSIVEADDRUSE` and
  `kyora serve`/`hub` report a busy port clearly and exit.
- **One Kyora per `KYORA_HOME`.** `kyora serve`, `kyora hub` and `kyora bench` hold an OS lock on
  `KYORA_HOME/kyora.lock` (flock / msvcrt) for their lifetime and refuse to start when another Kyora holds
  it, so a second instance on the same home - on another port, a bench next to a running server, or a
  listener macOS let share the port - can no longer mark the first one's live runs interrupted. The
  bench's embedded server falls back to ephemeral main and preview ports when the configured ones are busy.
- **Slides keep ignored game files**: slide commits (and so ZIP downloads and previews) now include
  files named by the game's `.gitignore`, `.git/info/exclude` or the user's global excludes file, as
  the snapshot backend always did; the gate measured them, so the saved slide now matches the
  measured game. Resets and checkouts also remove ignored leftovers, so a cancelled run's ignored
  files cannot reach the next slide.
- **Unfinished runs no longer share a lineage branch**: a run whose branch (`lineage/main` for a
  first generation, or the head branch of the parent slide) is held by another unfinished run, or
  an import in that situation, gets its own `lineage/<rid>` branch. Each run records the commit it
  last left on its branch (`options.tip`); resume restores exactly that commit and blocks with a
  clear reason when the branch moved on, so a resumed run never tests or commits another run's game.
  Rollback only resets a branch whose tip is still the run's own commit, so cancelling an older run
  can no longer delete the branch that holds a newer slide.
- **Cancelling a queued or blocked run no longer resets a running run's work**: a never-started
  run needs no rollback, and while another run of the project is executing the cancel only moves
  the cancelled run's own branch ref; it never resets, cleans or checks out the shared work tree.
- **Stopping Kyora keeps runs resumable**: Ctrl+C (`App.stop()` / `RunManager.stop()`) used to cancel
  active runs, rolling back their built work. Runs now end `blocked` with the reason
  "interrupted: Kyora stopped ...", their work and iteration commits kept, and Resume continues them.
  A queued run that was waiting for its project when Kyora stopped is no longer started during
  shutdown; the next start marks it `blocked` (resumable).
- **Honest build narration**: the presenter announces the build only after a code engine was
  selected, naming the real backend (Claude Code, the Anthropic API engine or Claude Code on the
  Kyora Hub) and the configured effort; it says "maximum effort" only when the effort is `max`, and
  says nothing when the run blocks for lack of an engine.
- **A failed code-engine call no longer finishes a run.** When BUILD or FIX returns an error (usage
  limit, HTTP 429, lost Hub contact, expired login) and the engine changed no file of the work tree
  (content hash before and after the call), the run is `blocked` with the engine error and can be
  resumed; before, a modify run measured the unchanged parent game and ended `complete` with a new
  slide. Every failed call is recorded in `run.json` (`extra.engine_failures`, `tree_changed`).
- **Skipped acceptance tests no longer pass the gate.** A plan acceptance test reported as skipped
  (`test.skip`, `test.todo`, TAP `# SKIP` / `# TODO`, harness `skip`) and any required check that was
  skipped or deferred now count as failed with the detail "did not run"; an acceptance id counts as
  reported only with a pass or fail result.
- **Node acceptance tests without Node.** Kimi K3's plan input now says whether Node is installed on
  the device that runs TEST; without Node only browser acceptance tests are accepted (one repair
  round, then the run blocks), and a plan with Node tests blocks at PLAN or TEST with an explicit
  reason instead of spending every fix iteration on tests that can never report.
- **The deck in a slide describes that slide's run.** FINALIZE passes the finalizing run to the deck
  builder; before, a run queued for the same project (state `created`) was shown as "this build".
- **An invalid Kimi K3 intake or review answer blocks the run** (resumable, like PLAN and DESIGN)
  instead of failing it; before, a twice-unparseable review after a long build ended the run
  `failed`, with no slide and no way to resume.
- **Kyora Bench measures the bench, at the bench's threshold.** A bench run's gate is now scored on the
  bench's own test ids (`passed / bench ids`, required checks still required); the tautological
  `tests_present` checks of the injected test file and the engine's own Node tests no longer count, so
  a task can no longer pass at "94 %" with 2 of 14 bench tests failing. The gate threshold of a bench
  run is the task's `pass_rule.min_pass_rate` (never below 0.94) regardless of Settings, `task_passed`
  also requires the recorded `pass_rate` and `threshold` to reach it, and `results.json` records the
  threshold per task.
- **One engine/model means every engine call.** Bench certification reads every entry of the run's
  `extra.engine_runs` (BUILD and each FIX), so a task served by two models is mixed and blocks
  certification; results list `served` pairs. Slides and the deck name every model that served a run.
- **The deck credits only what ran.** An imported-ZIP slide's architecture slide now says "imported ZIP -
  no code engine ran" (or "not built yet") instead of drawing the Build step as Claude Code, and the
  tests slide lists the code-engine errors recorded for the run.
- **Judge statistics count unjudged calls.** When the hook cannot reach the judge, the engine now counts
  the call on the run's `unavailable` counter (the Messages-API engine does the same when `judge.decide`
  raises), so `run.json`'s `judge` block matches the `judge.unavailable` events instead of recording zero.
  A Hub build folds the hub's per-job judge delta into `run.judge`, and a resumed run restores the
  counters persisted with the run instead of overwriting them with the smaller counts of the resumed leg.
- **A late judge verdict never governs a call that already ran.** The hook sends a `deadline_ms` budget
  (a little under its timeout); a Kimi verdict slower than that arrives after the hook has failed open and
  the tool has run, so it is journaled as `late` (and still seeds the cache) but is not counted and its
  `tool_use_id` is not registered. The call is counted as unavailable, as the documented guarantee
  requires; before, such a verdict was recorded as the deciding `deny`/`allow` with `unavailable: 0`.
- NVIDIA streaming parser: a delta that carries both `reasoning_content` and `reasoning` (vLLM 0.11
  and later sends one as a copy of the other) is now taken once, so the reasoning trace, the UI stream
  and the assistant message Kimi K3 echoes back on the next turn are no longer doubled.
- `kimi.effort_field = extra_body` sent a literal `"extra_body"` key that no server reads (the OpenAI
  SDK merges `extra_body` into the top level), so the effort was silently dropped. It now sends the
  top-level `chat_template_kwargs.reasoning_effort`. Self-check names the field and value it sent
  (`measured.effort_sent`) and no longer suggests the mode that was just rejected.
- The `extra_body` effort mode is now reported as best-effort: Kimi K3's documented control is the
  top-level `reasoning_effort` and its template is not documented to read
  `chat_template_kwargs.reasoning_effort`, so the model may ignore it. Self-check's detail line
  (`measured.effort_best_effort`), Kimi's `model.request`/`model.metrics`/`model.done`/`model.error`
  events (new `effort_sent`, plus `effort_best_effort` and `effort_note` in that mode), the Settings
  help, README and docs/honesty.md say so; none claims the level was applied.
- Live Judge card: a call the engine found unjudged (the hook never reached the judge) was counted
  only as unavailable in the UI, while the server counts it allowed + unavailable. The engines'
  (Claude Code, Messages API, relayed unchanged by the Hub) per-call `judge.unavailable` events now
  carry `ran_unjudged: true`, and the card counts those as allowed too; the judge's own
  `judge.unavailable` + `judge.verdict` (source `unavailable`) pair is still counted once.
- Windows npm install (`claude.cmd`): the prompt went into argv, where `cmd.exe` cut it at the first
  line break and dropped every later flag (stream-json, `--tools`, `--settings` with the judge hook).
  A `.cmd`/`.bat` shim now always gets the prompt on stdin and the system prompt only as a file, and
  an attempt is refused with a clear error when another argument holds characters `cmd.exe` mangles.
- A clone made with Git for Windows' default `core.autocrlf=true` checked the launchers out with CRLF, so
  `sh launchers/kyora-ish.sh` / `kyora-termux.sh` stopped at `set -eu\r` on a phone, and a release built
  from it changed 615 vendored files. `.gitattributes` now keeps `*.sh`, `*.py`, `*.mjs` and `*.js` LF,
  checks `*.ps1` out as CRLF and marks `vendor/**` `-text` (no conversion).
- Self-check no longer fails (exit status 1) on a Termux phone or an `install.ps1 -SkipClaude` PC whose
  linked Hub is the selected, reachable code engine: a missing Claude Code CLI is then reported as SKIP
  ("not needed: the selected code engine is hub"). With no engine at all it still fails.
- Windows, Messages-API engine: the `bash` tool took the first `bash` on PATH, which is WSL's
  `System32\bash.exe` (commands ran inside the Linux distro, with the C:/ workspace path meaningless
  there), or found none with Git for Windows' default PATH. It now uses Git Bash
  (`CLAUDE_CODE_GIT_BASH_PATH`, the `bash.exe` next to `git.exe`, PATH, `%ProgramFiles%\Git`), never a
  `bash.exe` under `%SystemRoot%` or WindowsApps, and says "install Git for Windows" when there is none.
- Tokkio overlay: the avatar's real-time Nemotron-3-Ultra LLM ran with reasoning on (the model's
  default), and the Tokkio LLM service speaks only `delta.content`, so the avatar stayed silent for the
  whole reasoning trace. `bot.kyora.py` now builds it with `temperature 0.2` and
  `chat_template_kwargs.enable_thinking=false` (through the OpenAI SDK's `extra_body`).
- Tokkio overlay links: the custom view showed preview URLs as Kyora reported them (`127.0.0.1` or a
  Kyora-relative path, which mean nothing on the cluster or the kiosk), and **Open preview** / **Download
  ZIP** did nothing because the overlay dropped the URL `POST /api/actions` returned. `/api/actions` now
  answers absolute URLs under the host the caller used (previews and the harness on the preview listener,
  the ZIP on the main one), the overlay rewrites a loopback host to its `events_url` host, and a pressed
  button adds `<label>: <url>` to the view.
- **Honest judge wording.** The Judge card, the capability chip, README, docs, the deck's
  architecture slide and the engine's system prompt no longer say that Kimi K3 judges each tool call.
  They now say what happens: Write, Edit, MultiEdit, NotebookEdit and Bash pass through the judge,
  local rules allow planned files, standard game paths and read-only commands (and deny escapes and
  destructive commands), a per-run cache repeats Kimi verdicts, Kimi K3 decides the rest, and Read,
  Glob and Grep are not judged. The Judge card also counts verdicts by rule, cache and Kimi K3.
- **Gate, engine and effort labels come from the run.** The stage chart's Gate node and fix-loop
  label use the run's recorded threshold (they always said 94 %), Build/Fix and the code-engine tile
  name the run's backend (Claude Code, Anthropic API, via the Hub) and recorded effort (the tile
  always said "effort max"), and the prompt help and Honesty card show the configured effort and
  gate from Settings. The engine call records its effort in `run.json` (`engine.effort`) and announces
  `backend`/`effort` in its first `stage.progress`. A bench run records the bench threshold from the
  start, and the gate narration and Tokkio slide view name the gate the run applied, not Settings.
- **Claude Code's cost is labelled as an estimate.** The code-engine tile showed the CLI's
  `total_cost_usd` as "cost", as if Kyora had measured a charge. It is computed by the CLI at API list
  prices and a subscription login is not billed per run, so the tile now shows it as
  "API-equiv. est." (`~$`) with that explanation.
- **An HTTP error from the Tokkio URL is not a deployment.** The Presenter framed the Tokkio URL and
  said "Served by your Tokkio deployment" whenever the URL answered at all, including `502` from an
  ingress with no running pods or `404` from another service. It now shows the Tokkio UI only for a
  2xx/3xx answer (`ui.ok`) and otherwise says "Tokkio not deployed" with the HTTP status.
- **A resumed fresh generation starts empty again.** A generation on `lineage/main` whose base is an
  earlier failed run's commit cleared the work tree when it was prepared, but resuming it before its first
  commit checked that commit out again, so the new game was built on the earlier run's files. Resume now
  clears the tree again while the run has not committed.
- **Slides leave out `node_modules`.** Since slide commits force-add ignored game files, a
  `.gitignore`'d `node_modules/` (or `__pycache__/`) at any depth went into the slide, its ZIP export and
  its preview although the gate never measures it. Both backends now leave the directories in
  `quality.SKIP_DIRS` (`node_modules`, `__pycache__`; `.git` as before) out of every commit, and a git
  commit also drops such entries an older commit tracked; other ignored game files are still kept.
- **A FIFO in the work tree can no longer hang a run.** The "did the engine change anything" fingerprint
  read every non-symlink entry, so a named pipe (or socket/device) an engine left behind blocked the run
  thread forever. It now `lstat`s each entry, reads only regular files, counts other entries by type and
  size, and skips the directories the gate never measures (`.git`, `node_modules`, `__pycache__`) at any depth.
- **A slide never hides a failed engine call.** When a code-engine call failed but had changed files that
  then passed the gate, the slide looked like a clean build. Its `engine` now carries `ok: false` and
  `engine_errors` (kind, iteration, backend, error, whether files changed; from `run.extra.engine_failures`),
  also in the tag meta, and the slide card shows a warning chip "engine reported an error during this build"
  with the errors in its tooltip. The run stays `complete` and the gate badge keeps the measured result.
- **The Tokkio docs no longer say Kimi K3 grades the game.** `tokkio-kyora/README.md` and
  `docs/tokkio-overlay.md` now say what happens: the code engine implements the UI, the measured gate
  decides, and Kimi K3 reviews failed gates and judges the writes and shell commands the local rules
  do not settle. `tests/test_ui_labels.py` catches the old phrasing.
- **The bench confirm dialog names the configured effort and the selected engine.** It always said
  "Claude subscription usage at max effort"; it now names what the engine the server would select
  spends (Claude subscription, Anthropic API tokens or the linked Hub) at the configured
  `claude_code.effort`. The API's `estimate.warning` and `docs/bench.md` no longer say "max effort".
- **The Tokkio presenter persona no longer fixes the gate or the engine.** It said the gate passes at
  ninety four percent and that Claude Code writes the code; it now says the gate passes at the
  threshold Kyora reports for the run (94 % by default) and names a code engine, Claude Code by default.
- **The docs no longer present the gate threshold and the fix rounds as fixed.** `README.md`,
  `docs/honesty.md` and `docs/ui-design.md` say "at least the configured pass rate (default 94 %,
  `quality.min_pass_rate`)", "up to `quality.max_fix_iterations` rounds (default 3)" and that a slide
  below the gate is labelled "below <threshold> %".
- **`docs/hub-mode.md` describes the single-instance rules like the README.** Instead of "a second
  Kyora on a port in use stops with an error" it says that on Windows the listeners claim their ports
  exclusively and that on every platform one Kyora runs per `KYORA_HOME` (`kyora.lock`).

## 0.1.0 - 2026-10-15

First release.

### Added

- **Orchestration core** (Python 3.10+ standard library only, single `kyora.pyz`): configuration
  (`config/keys.json`, `config/settings.json`, packaged defaults), event bus with SQLite persistence
  and SSE replay, stdlib HTTP server, CLI `kyora serve | hub | selfcheck | bench | version`.
- **Model roles over their own APIs**: NVIDIA Nemotron-3-Ultra (`nvidia/nemotron-3-ultra-550b-a55b`)
  for design document, narrative, UI spec, copy and narration; Moonshot Kimi K3 (`moonshotai/kimi-k3`,
  `reasoning_effort` max) for intake with vision, plan and acceptance tests, judgement of the code
  engine's Write/Edit/Bash calls that local rules do not settle, and review; both through `https://integrate.api.nvidia.com/v1` with measured
  time to first token, latency, token counts and tokens/s.
- **Code engine**: Claude Code CLI at `--effort max` with the user's subscription login, model
  fallback chain (`claude-fable-5-1` -> `claude-opus-5-5` -> `claude-sonnet-5-5`) with the served model
  shown, compatibility retry for hidden CLI flags, PreToolUse judge hook; optional Messages-API engine
  (`ANTHROPIC_API_KEY` + `anthropic` package); missing engine reported as BLOCKED.
- **Quality gate**: static checks, Node checks and in-browser acceptance tests; a project is complete
  only at >= 94 % of its measured checks with every required check passing, after at most 3 Kimi
  K3 review / Claude Code fix rounds.
- **Projects**: attachments, per-project git repository with slides (`slide/vN` tags, branch and stack
  modifications, ZIP download), auto-generated presentation deck, previews on other devices over
  the LAN.
- **Hub mode**: a PC lends its code engine to phones and tablets on the LAN (token, QR pairing).
- **Kyora Bench**: real pipeline runs on fixed tasks with bench-owned tests; certification only from
  a measured run at >= 94 %.
- **NVIDIA Tokkio 5.0.0-ga** vendored unmodified (639 files, sha256 manifest, `scripts/vendor_tokkio.py
  --verify`) plus the `tokkio-kyora/` overlay: `KyoraProcessor` (narration via TTS, custom views,
  action buttons), overlay `config.yaml` pointing the avatar's LLM at Nemotron-3-Ultra, `bot.py` with
  marked insertions only, and `deploy/upload_overlay.py` (dry run by default, ACE Configurator upload
  or `kubectl cp` fallback, verified results only).
- **Launchers**: `launchers/install.ps1` and `launchers/kyora.ps1` (Windows PowerShell 5.1 and 7),
  `install-termux.sh` / `kyora-termux.sh` (Android, Termux), `install-ish.sh` / `kyora-ish.sh`
  (iOS, iSH).
- **Release tooling**: `scripts/build_release.py` (reproducible `kyora.pyz` and
  `dist/Kyora-AGI-Studio-0.1.0.zip` with the vendored symlinks preserved; `--check` runs and verifies
  the build), `scripts/ps1_lint.py` (PowerShell lint without PowerShell).
- **Docs**: README, honesty statement, platforms, architecture, Claude Code engine, Hub mode, Bench,
  UI design, Nemotron reference, Tokkio overlay guide.

### Fixed

- `kyora/config.py`: the defaults packaged inside `kyora.pyz` are now read through
  `importlib.resources`, so a `kyora.pyz` started outside the release folder seeds
  `KYORA_HOME/config` with the packaged keys and settings instead of empty defaults.

### Known limitations

- Claude Code has no official Android or iOS build: phones build through a Hub, otherwise runs are
  BLOCKED.
- iSH is a slow x86 emulator; Bench is not recommended there.
- Tokkio needs an NVIDIA-GPU Kubernetes cluster (2 GPUs for 1-stream, 4 for 3/6-stream) and NGC
  access; without it the Presenter panel shows "Tokkio not deployed".
- The NVIDIA keys in `config/keys.json` are committed by the owner's decision; anyone with the
  repository or release can use them (rotate at build.nvidia.com if leaked).
