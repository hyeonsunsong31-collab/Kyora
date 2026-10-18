# Platforms

One codebase runs everywhere: the Kyora core is **Python 3.10+ standard library only** and ships as a
single `kyora.pyz` (a zipapp; `python kyora.pyz serve`). No compiled wheels are needed, which is what
makes iSH (32-bit x86 Alpine emulation) and Termux possible. What differs per platform is which
external tools exist there - above all the Claude Code CLI, the default code engine. The Kyora agent
engine ([`agent-engine.md`](agent-engine.md)) needs only Python and HTTPS to its endpoint, so BUILD and
FIX can run on every platform without Claude, on a different (non-Claude) model and judged by the same
gate.

## Matrix

| Capability | Windows (PowerShell 5.1 / 7) | Android (Termux) | iOS / iPadOS (iSH) |
|---|---|---|---|
| Kyora server + UI (projects, attachments, live pipeline chart, slides, deck, ZIP download) | yes | yes | yes (slow) |
| Nemotron and Kimi roles over the NVIDIA API (with key and model fallbacks) | yes | yes | yes |
| Code engine: Claude Code CLI | **yes, native** (winget `Anthropic.ClaudeCode` or the official installer) | **no official Android build** -> Kyora agent engine or Hub mode; community glibc-runner route exists, unofficial and unsupported | **no** (no Node) -> Kyora agent engine or Hub mode |
| Optional Messages-API engine (`ANTHROPIC_API_KEY` + `pip install anthropic`; same Claude models, billed per use) | yes | possible if the `anthropic` package installs | not practical (compiled dependencies) |
| Kyora agent engine (no Claude; NVIDIA models by default) | yes (shell tool through Git Bash; without it the file editor only) | yes (Termux `bash`, Node logic tests with `nodejs-lts`) | yes when the endpoint is reachable (BusyBox `sh` unless `bash` is installed; no Node: static checks only before TEST); slow |
| With none of these engines and no Hub | BUILD is **BLOCKED** | BUILD is **BLOCKED** | BUILD is **BLOCKED** |
| Judge (local rules, cache, Kimi role) | yes: hook for Claude Code, in process for the API and agent engines | in process for the agent engine; on the Hub for Hub builds | in process for the agent engine; on the Hub for Hub builds |
| Static checks (stdlib) | yes | yes | yes |
| Node checks (`node --check`, `node --test`) | with Node (`install.ps1 -WithNode`) | yes (`nodejs-lts`) | no: skipped and not counted; JS syntax is checked in the browser instead |
| Browser acceptance tests (harness page) | in your browser; headless Chromium where Playwright exists | in the Android browser | in Safari |
| Run as a Hub for other devices (`hub`) | yes | yes, with the Kyora agent engine (or a working Claude Code) | not recommended (slow) |
| Kyora Bench | yes | with the Kyora agent engine or via a Hub | not recommended (slow) |
| Tokkio presenter | connect to a cluster you deployed (see [`tokkio-overlay.md`](tokkio-overlay.md)) | same | same |
| Git (slides) | Git for Windows | `pkg install git` | `apk add git` |

"Hub mode": a PC runs `kyora hub`; the phone links to it (QR / link) and delegates only BUILD and FIX
to the PC's engine (Claude Code, else the Messages-API engine, else the Kyora agent engine); everything
else runs on the phone. See [`hub-mode.md`](hub-mode.md). Which engine a run uses follows
`settings.engines.order` (default hub, claude_code, anthropic_api, openai_agent); an engine that stops
for an account reason fails over to the next one (`engine.failover`).

## Windows

```powershell
# from the folder that contains kyora.pyz
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\install.ps1 -WithNode
claude auth login                       # once, with your Claude subscription (no API key needed)
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 selfcheck
powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 serve      # opens http://127.0.0.1:8765
```

* `install.ps1` (PowerShell 5.1 and 7, StrictMode, idempotent) uses winget for `Python.Python.3.12`,
  `Git.Git`, optionally `OpenJS.NodeJS.LTS`, and `Anthropic.ClaudeCode`; if that package fails it runs
  Anthropic's official installer (`irm https://claude.ai/install.ps1 | iex`, in a child PowerShell -
  the only `Invoke-Expression` in Kyora). It refreshes PATH from the registry, prints the versions and
  reads `claude auth status --json`, telling you to run `claude auth login` if needed. `-SkipClaude`
  skips Claude Code (for a PC that links to another Hub, or builds with the Anthropic API engine or the
  Kyora agent engine).
* `kyora.ps1 [serve|hub|selfcheck|bench|version] [args...]` runs `<root>\kyora.pyz` (or
  `python -m kyora` in a source checkout), sets `KYORA_HOME` to `%USERPROFILE%\.kyora` unless set,
  adds `--open` for `serve` (`--no-open` suppresses it), passes every other argument through and exits
  with Kyora's exit code.
* Claude Code on Windows uses Git Bash for its Bash tool; if it cannot find `bash.exe`, set
  `CLAUDE_CODE_GIT_BASH_PATH`. The `bash` tool of the Messages-API engine and of the Kyora agent engine
  finds Git Bash the same way: `CLAUDE_CODE_GIT_BASH_PATH`, then the `bash.exe` next to `git.exe`
  (`<Git>\cmd\git.exe` -> `<Git>\bin\bash.exe`), then `bash`/`sh` on PATH, `%ProgramW6432%`,
  `%ProgramFiles%` and `%ProgramFiles(x86)%\Git\bin\bash.exe` and
  `%LOCALAPPDATA%\Programs\Git\bin\bash.exe`. It never uses the WSL launcher
  `%SystemRoot%\System32\bash.exe` (or a WindowsApps alias), which would run the command inside the Linux
  distro. Without Git Bash, every `bash` call of the Messages-API engine returns an error that says so,
  and the Kyora agent engine does not offer a `bash` tool at all: its model is told so and works with the
  file editor and `kyora_check` only.
* If `claude` resolves to the npm shim `claude.cmd` (`npm install -g @anthropic-ai/claude-code`),
  Windows runs it through `cmd.exe`, which cuts an argument at its first line break and expands `%`.
  Kyora therefore sends every prompt to a `.cmd`/`.bat` shim on stdin, passes the system prompt only
  with `--append-system-prompt-file` (no inline retry for CLIs that lack that flag: update Claude Code),
  and refuses to start the shim when any other argument contains a line break, `%`, `"`, `&`, `|`,
  `<`, `>` or `^` (for example a `KYORA_HOME` path or `claude_code.extra_args`). The native
  `claude.exe` (winget or the official installer) has none of these limits.
* NTFS reaches `.git` also through its 8.3 short name (`GIT~1`) and through `.git` with trailing dots
  or spaces. A game ZIP import, a slide import and a Hub workspace or result therefore refuse any path
  component like `GIT~<n>`, `GI7EBA~<n>` or `.git.` (any case, on every platform) and any entry that
  resolves into a `.git` directory; on Windows they also refuse a component that ends in a dot or a
  space (Windows strips it, so the entry would land on another name) and reserved names such as `CON`.
* Line endings: `.gitattributes` keeps `*.sh`, `*.py`, `*.mjs` and `*.js` LF in every checkout (also with
  Git for Windows' default `core.autocrlf=true`), checks `*.ps1` out with CRLF (PowerShell reads both)
  and never converts `vendor/**`, so a folder cloned on Windows and copied to Termux or iSH still starts,
  and a release built from it keeps the vendored tree byte-identical to `vendor/VENDOR.md`.
* Content types (game previews, the UI's static files, uploads) come from Kyora's own table, never from
  the Windows registry, which can map `.js` to `text/plain` (browsers then refuse module scripts) and
  `.zip` to `application/x-zip-compressed`; ZIP uploads under that alias, as Chrome and Edge send them on
  Windows, are treated as `application/zip`. The headless test driver's output is read as UTF-8, not in
  the console's code page.
* To reach the PC from a phone, allow Python through Windows Defender Firewall on private networks
  when Windows asks (first `kyora hub`).

## Android (Termux)

Install Termux (the Termux project publishes it on F-Droid and GitHub), then:

```sh
sh launchers/install-termux.sh            # pkg update -y; pkg install -y python git nodejs-lts
sh launchers/install-termux.sh --with-claude   # optional attempt; see below
sh launchers/kyora-termux.sh selfcheck
sh launchers/kyora-termux.sh              # serve and open the UI with termux-open-url
sh launchers/kyora-termux.sh --hub        # Hub mode: lends this phone's engine (e.g. the Kyora agent engine)
```

* `kyora-termux.sh` sets `TMPDIR=$PREFIX/tmp` (Termux has no `/tmp`), defaults `KYORA_HOME` to
  `~/.kyora`, waits until the server answers and then opens it with `termux-open-url`.
* **BUILD and FIX on the phone**: the Kyora agent engine runs them on the phone with the NVIDIA keys
  (a different model than Claude; the same gate decides). Its `bash` tool is Termux's `bash`, and
  `nodejs-lts` lets `kyora_check` and the TEST stage run the Node logic tests. The judge runs on the
  phone too, with the phone's NVIDIA keys.
* **Claude Code**: there is no official Android build. `--with-claude` tries
  `npm install -g @anthropic-ai/claude-code` and `claude --version` and reports honestly when it does
  not work. The supported route to Claude is Hub mode from a PC. A community glibc-runner setup exists
  but is unofficial and unsupported by Kyora. An advanced, user-managed alternative is Claude Code inside
  a proot-distro Ubuntu, wired up with Settings -> `claude_code.command` (a command that runs `claude`
  inside the distro; Kyora's workspace path must be visible at the same path there). With no engine at
  all (agent engine off or without a key, no Hub), BUILD is BLOCKED.
* Termux has no `/bin/sh` or `/usr/bin/env`; normally `termux-exec` rewrites such shebangs. When a
  process starts without it (seen in termux-docker), Kyora runs a script whose shebang names a missing
  `/bin/...` or `/usr/bin/...` interpreter through `$PREFIX/bin/<interpreter>` instead (npm's `claude`
  shim starts with `#!/usr/bin/env node`).
* Self-check reports a missing `claude` as SKIP ("not needed: the selected code engine is hub") while a
  linked Hub (or the Messages-API engine) is the selected engine, and as WARN, naming the Kyora agent
  engine and saying it is not a Claude model, while that engine is selected. Neither is a FAIL, so a
  missing CLI alone does not make `kyora-termux.sh selfcheck` exit 1 (the same holds on a PC installed
  with `install.ps1 -SkipClaude`). With no engine at all both `Claude Code CLI` and `Selected code
  engine` stay FAIL.
* Android may stop Termux in the background; `termux-wake-lock` keeps it alive during long runs.

## iOS / iPadOS (iSH)

Install iSH from the App Store, copy or `git clone` the Kyora folder into it, then:

```sh
sh launchers/install-ish.sh               # apk update; apk add python3 git
sh launchers/kyora-ish.sh selfcheck
sh launchers/kyora-ish.sh                 # prints "Open Safari at http://localhost:8765"
```

* iSH emulates a 32-bit x86 CPU in software: everything works but slowly; the first start takes a
  while. Kyora Bench is not recommended there.
* Node.js does not run usefully in iSH, so there is **no Claude Code and no Node test runner** on the
  phone. BUILD and FIX run on the phone with the Kyora agent engine when the NVIDIA endpoint is
  reachable from it (a different model than Claude; the same gate decides): its `bash` tool is Alpine's
  BusyBox `sh` unless you `apk add bash`, and `kyora_check` reports the static checks and lists the Node
  checks as deferred (JavaScript syntax is checked later in Safari). Every model turn and tool call runs
  under the emulation, so a local build is slow. For Claude, link a Hub: on the PC run `kyora hub`, then
  scan its QR code with the iPhone camera (or open the printed link in Safari). With neither, runs stop
  in **BLOCKED** with the reason.
* The CI job "iSH userland (32-bit Alpine, musl, real apk)" runs `install-ish.sh`, the unit tests (the
  agent engine's included, against the mock endpoint) and the launcher on 32-bit Alpine; it is not the
  iSH app itself.
* Keep iSH in the foreground while Kyora works: iOS suspends background apps, and the local server
  stops answering until you switch back.
* Browser acceptance tests run in Safari through Kyora's harness page, which the UI opens for you.
* If Alpine's `python3` is older than 3.10, point `/etc/apk/repositories` at a newer Alpine release
  and run `apk update && apk upgrade`; `install-ish.sh` checks the version.

## Linux / macOS (from source)

Not a launcher target, but the same code runs: `python3 kyora.pyz serve --open` or, in a checkout,
`python3 -m kyora serve --open`. Install Claude Code with Anthropic's installer and log in with
`claude auth login`, or use the Anthropic API engine or the Kyora agent engine without it.

* macOS: resolving the machine's own name can take seconds (`<name>.local` over mDNS), which made
  `GET /api/status` so slow on the GitHub macOS runner that the UI, the release check and several tests
  timed out. LAN addresses now come from the default route,
  and name lookups run with a 1 s budget and are cached. `node --check` errors keep their line numbers
  although Node prints the `/private/var/...` real path of the temp directory.

## Runtime data

`KYORA_HOME` (default `~/.kyora`, on Windows `%USERPROFILE%\.kyora`) holds the event database,
projects (each with its own git repository of slides), runs, logs and the extracted judge hook.
Configuration is read from `--config DIR`, `$KYORA_CONFIG_DIR`, `./config`, the `config/` folder next
to `kyora.pyz`, and finally `KYORA_HOME/config` (seeded from the defaults packaged in `kyora.pyz`).
