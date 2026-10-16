# Changelog

All notable changes to Kyora. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Fixed

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

## 0.1.0 - 2026-10-15

First release.

### Added

- **Orchestration core** (Python 3.10+ standard library only, single `kyora.pyz`): configuration
  (`config/keys.json`, `config/settings.json`, packaged defaults), event bus with SQLite persistence
  and SSE replay, stdlib HTTP server, CLI `kyora serve | hub | selfcheck | bench | version`.
- **Model roles over their own APIs**: NVIDIA Nemotron-3-Ultra (`nvidia/nemotron-3-ultra-550b-a55b`)
  for design document, narrative, UI spec, copy and narration; Moonshot Kimi K3 (`moonshotai/kimi-k3`,
  `reasoning_effort` max) for intake with vision, plan and acceptance tests, instant judgement of every
  code-engine tool call and review; both through `https://integrate.api.nvidia.com/v1` with measured
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
