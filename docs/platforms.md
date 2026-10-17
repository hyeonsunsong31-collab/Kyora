# Platforms

One codebase runs everywhere: the Kyora core is **Python 3.10+ standard library only** and ships as a
single `kyora.pyz` (a zipapp; `python kyora.pyz serve`). No compiled wheels are needed, which is what
makes iSH (32-bit x86 Alpine emulation) and Termux possible. What differs per platform is which
external tools exist there - above all the Claude Code CLI, which is the code engine.

## Matrix

| Capability | Windows (PowerShell 5.1 / 7) | Android (Termux) | iOS / iPadOS (iSH) |
|---|---|---|---|
| Kyora server + UI (projects, attachments, live pipeline chart, slides, deck, ZIP download) | yes | yes | yes (slow) |
| Nemotron-3-Ultra and Kimi K3 over the NVIDIA API | yes | yes | yes |
| Code engine: Claude Code CLI | **yes, native** (winget `Anthropic.ClaudeCode` or the official installer) | **no official Android build** -> use Hub mode; community glibc-runner route exists, unofficial and unsupported | **no** (no Node) -> Hub mode, otherwise BUILD is **BLOCKED** |
| Optional Messages-API engine (`ANTHROPIC_API_KEY` + `pip install anthropic`) | yes | possible if the `anthropic` package installs | not practical (compiled dependencies) |
| Instant judge (Kimi K3 via the PreToolUse hook) | yes | on the Hub | on the Hub |
| Static checks (stdlib) | yes | yes | yes |
| Node checks (`node --check`, `node --test`) | with Node (`install.ps1 -WithNode`) | yes (`nodejs-lts`) | no: skipped and not counted; JS syntax is checked in the browser instead |
| Browser acceptance tests (harness page) | in your browser; headless Chromium where Playwright exists | in the Android browser | in Safari |
| Run as a Hub for other devices (`hub`) | yes | only if Claude Code runs there | no |
| Kyora Bench | yes | via a Hub | not recommended (slow, needs a Hub) |
| Tokkio presenter | connect to a cluster you deployed (see [`tokkio-overlay.md`](tokkio-overlay.md)) | same | same |
| Git (slides) | Git for Windows | `pkg install git` | `apk add git` |

"Hub mode": a PC with Claude Code runs `kyora hub`; the phone links to it (QR / link) and delegates
only BUILD and FIX; everything else runs on the phone. See [`hub-mode.md`](hub-mode.md).

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
  skips Claude Code (for a PC that only links to another Hub).
* `kyora.ps1 [serve|hub|selfcheck|bench|version] [args...]` runs `<root>\kyora.pyz` (or
  `python -m kyora` in a source checkout), sets `KYORA_HOME` to `%USERPROFILE%\.kyora` unless set,
  adds `--open` for `serve` (`--no-open` suppresses it), passes every other argument through and exits
  with Kyora's exit code.
* Claude Code on Windows uses Git Bash for its Bash tool; if it cannot find `bash.exe`, set
  `CLAUDE_CODE_GIT_BASH_PATH`.
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
* To reach the PC from a phone, allow Python through Windows Defender Firewall on private networks
  when Windows asks (first `kyora hub`).

## Android (Termux)

Install Termux (the Termux project publishes it on F-Droid and GitHub), then:

```sh
sh launchers/install-termux.sh            # pkg update -y; pkg install -y python git nodejs-lts
sh launchers/install-termux.sh --with-claude   # optional attempt; see below
sh launchers/kyora-termux.sh selfcheck
sh launchers/kyora-termux.sh              # serve and open the UI with termux-open-url
sh launchers/kyora-termux.sh --hub        # Hub mode (only useful if Claude Code runs here)
```

* `kyora-termux.sh` sets `TMPDIR=$PREFIX/tmp` (Termux has no `/tmp`), defaults `KYORA_HOME` to
  `~/.kyora`, waits until the server answers and then opens it with `termux-open-url`.
* **Claude Code**: there is no official Android build. `--with-claude` tries
  `npm install -g @anthropic-ai/claude-code` and `claude --version` and reports honestly when it does
  not work. The supported route is Hub mode from a PC. A community glibc-runner setup exists but is
  unofficial and unsupported by Kyora. An advanced, user-managed alternative is Claude Code inside a
  proot-distro Ubuntu, wired up with Settings -> `claude_code.command` (a command that runs `claude`
  inside the distro; Kyora's workspace path must be visible at the same path there). Without any
  engine, BUILD is BLOCKED.
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
  phone. Link a Hub: on the PC run `kyora hub`, then scan its QR code with the iPhone camera (or open
  the printed link in Safari). Without a Hub, runs stop in **BLOCKED** with the reason.
* Keep iSH in the foreground while Kyora works: iOS suspends background apps, and the local server
  stops answering until you switch back.
* Browser acceptance tests run in Safari through Kyora's harness page, which the UI opens for you.
* If Alpine's `python3` is older than 3.10, point `/etc/apk/repositories` at a newer Alpine release
  and run `apk update && apk upgrade`; `install-ish.sh` checks the version.

## Linux / macOS (from source)

Not a launcher target, but the same code runs: `python3 kyora.pyz serve --open` or, in a checkout,
`python3 -m kyora serve --open`. Install Claude Code with Anthropic's installer and log in with
`claude auth login`.

## Runtime data

`KYORA_HOME` (default `~/.kyora`, on Windows `%USERPROFILE%\.kyora`) holds the event database,
projects (each with its own git repository of slides), runs, logs and the extracted judge hook.
Configuration is read from `--config DIR`, `$KYORA_CONFIG_DIR`, `./config`, the `config/` folder next
to `kyora.pyz`, and finally `KYORA_HOME/config` (seeded from the defaults packaged in `kyora.pyz`).
