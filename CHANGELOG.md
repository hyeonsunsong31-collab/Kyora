# Changelog

All notable changes to Kyora. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

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
- **The Tokkio docs no longer say Kimi K3 grades the game.** `tokkio-kyora/README.md` and
  `docs/tokkio-overlay.md` now say what happens: the code engine implements the UI, the measured gate
  decides, and Kimi K3 reviews failed gates and judges the writes and shell commands the local rules
  do not settle. `tests/test_ui_labels.py` catches the old phrasing.

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
