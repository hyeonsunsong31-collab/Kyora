# Kyora UI design

This describes the interface that ships in `kyora/web/`, as it is implemented today. The
tokens live in `kyora/web/css/tokens.css`, the components in `kyora/web/css/app.css`, and the
behaviour in `kyora/web/js/*.js` (plain ES2017, no framework, no build step, no web fonts, no
CDN). The same files are served from the source tree and from `kyora.pyz`, so the UI looks and
behaves identically on Windows, Android (Termux) and iOS (iSH + Safari).

## Principles

1. **A studio, not a chat.** The screen is organised around the work (prompt, pipeline,
   game, slides), with the four external systems shown in the roles they actually have.
2. **Measured or blank.** Every number on screen comes from an event or an API response. When
   nothing has been measured the UI shows `-` and says what would produce the number.
3. **Phone first.** The smallest supported layout is a 390 px wide phone held in one hand;
   the desktop layout is the same content with more room.
4. **Quiet surfaces, loud states.** Neutral blue-grey surfaces; colour is reserved for state
   (ok, warning, danger, running) and for identifying the four systems.

## Colour

Dark is the default studio surface; light follows `prefers-color-scheme: light`. Both themes
define the same token names, so components never branch on the theme.

| Token | Dark | Light | Role |
|---|---|---|---|
| `--bg` | `#0b0f14` | `#f4f6f8` | page background |
| `--bg-elev` | `#121821` | `#ffffff` | cards, nav, modals |
| `--bg-elev-2` | `#182029` | `#f0f3f6` | buttons, tiles, chips |
| `--bg-sunken` | `#070a0e` | `#e7ebf0` | inputs, log, gauge track, preview stage |
| `--line` / `--line-strong` | `#223040` / `#31465c` | `#d6dde6` / `#b9c4d1` | borders, chart edges |
| `--fg` | `#e8eef4` | `#101820` | primary text |
| `--fg-muted` | `#9fb0c0` | `#4b5a69` | secondary text, labels |
| `--fg-subtle` | `#6b7c8c` | `#7a8896` | captions, timestamps, hints |
| `--accent` | `#2dd4bf` | `#0f8f85` | primary action, selection, running stage, focus ring |
| `--ok` | `#3ddc97` | `#1a9d63` | done, passed gate, available engine |
| `--warn` | `#f5b846` | `#a86b0b` | blocked, below gate, partial |
| `--danger` | `#ff6b6b` | `#cf3540` | failed, denied, BLOCKED engine, destructive buttons |
| `--info` | `#60a5fa` | `#2563eb` | configured-but-unverified, informational notices |

Per-system hues identify where a signal came from (metric tile top borders, log badges,
stream tail): `--sys-nemotron` green, `--sys-kimi` violet, `--sys-claude` orange,
`--sys-tokkio` cyan. They are never used for state, so "green" always means "passed".

Pipeline stage states map to their own tokens (`--stage-idle`, `--stage-running` = accent,
`--stage-done` = ok, `--stage-failed` = danger, `--stage-blocked` = warn, `--stage-skipped`)
and are additionally distinguished by stroke style (blocked is dashed, skipped is dotted), so
the chart does not rely on colour alone.

**Contrast (WCAG 2.x, measured from the token values).** Dark theme: `--fg` on cards 15.3:1,
`--fg-muted` 8.0:1, `--accent` 9.6:1, state colours 6.4-10.1:1, `--fg-subtle` 4.2:1 on cards
(used only for captions and timestamps, never for essential text). Light theme: `--fg` 17.9:1
and `--fg-muted` 7.1:1 on white, but several small-text colours fall below 4.5:1 on white:
`--fg-subtle` 3.6:1, `--accent` 4.0:1 (also white text on accent buttons), `--ok` 3.5:1,
`--sys-nemotron` 3.7:1, `--sys-claude` 4.2:1, `--warn` 4.4:1. These are a known gap. Darker
values that clear 4.5:1 on white, `--bg-elev-2` and `--bg-sunken` are: `--fg-subtle #606b76`,
`--accent #0c766e`, `--ok #14784c`, `--sys-nemotron #277923`, `--sys-claude #a3540d`,
`--warn #935e09`.

## Type

System font stacks only (`-apple-system, Segoe UI, Roboto, ...` and `ui-monospace, SF Mono,
Menlo, Consolas, ...`): nothing to download, native rendering on every platform.

| Token | Size | Use |
|---|---|---|
| `--fs-0` | 12 px | captions, badges, chip text, log |
| `--fs-1` | 13 px | tables, card titles (uppercase, tracked), help text |
| `--fs-2` | 15 px | body |
| `--fs-3` | 17 px | prompt textarea, lead |
| `--fs-4` | 20 px | run state headline |
| `--fs-5` | 24 px | view titles, judge counters |
| `--fs-6` | 32 px | metric tile numbers |
| `--fs-7` | 44 px | bench score |

Numbers use `font-variant-numeric: tabular-nums` so live values do not jitter. Model names,
paths, hashes and the log are monospaced. Card titles are small uppercase labels with
`--tracking-caps`; hints inside a title stay in normal case.

## Space, shape, depth

A 4 px grid (`--s-1` 4 px through `--s-8` 64 px), a page gutter of 16 px on phones and 24 px
from 960 px. Radii: 6 px (small controls), 10 px (buttons, inputs), 16 px (cards), 24 px,
pill. Three shadows (`--shadow-1` hairline for cards, `--shadow-2` for floating bars and
toasts, `--shadow-3` for modals).

## Motion

Durations are tokens (`--t-fast` 120 ms, `--t-base` 200 ms, `--t-slow` 360 ms) with an
ease-out curve. Motion is used for: view entry (fade + 4 px rise), the running stage pulse,
the live connection dot, gauge and progress transitions, toasts. Under
`prefers-reduced-motion: reduce` every duration token becomes 0 ms and the pulses stop; the
spinner slows down instead of disappearing so "busy" stays visible.

## Interaction and accessibility

- **Touch targets.** `--touch` is 44 px: buttons, inputs, selects, nav items, checkboxes rows
  and the topbar controls are at least 44 px tall; compact `sm` buttons (36 px) are used only
  in toolbars with spacing around them.
- **Focus.** `:focus-visible` draws a two-ring focus indicator (`--focus-ring`: a background
  gap plus a 2 px accent ring) on every interactive element; mouse clicks do not show it.
- **Keyboard.** A skip link jumps to the content; `Ctrl/Cmd+Enter` runs the pipeline from the
  Studio; modals trap focus, close on `Escape`, and restore focus to the opener.
- **Semantics.** Views are `section`s with labelled headings; the bottom/side nav marks the
  current view with `aria-current="page"`; the connection indicator, the capability strip, the
  log and notices are live regions; toggle buttons use `aria-pressed`; the pipeline chart is an
  `img` with a text summary of every stage state in its label.
- **Screen readers and chips.** Capability chips carry their detail as visually hidden text,
  not only in a tooltip.

## Layout

**Phone (< 960 px).** A sticky top bar (logo, current project, connection state) and a fixed
bottom navigation with eight icon + label items, respecting the safe-area insets of notched
phones. Content is a single column; the "Run pipeline" button and the settings save bar float
just above the bottom nav. The capability strip scrolls horizontally inside itself; the page
itself never scrolls sideways (the smoke test checks this on every view at 390 px). The
pipeline chart switches to a vertical layout with the fix loop drawn on the right edge.

**Desktop (>= 960 px).** The navigation becomes a 224 px left rail with a short honesty
footer; views use two-column splits (7:5, or 2:1 for Preview) capped at 1360 px; metric tiles
sit three across; toasts move to the bottom-right; modals are centred instead of bottom
sheets. The chart switches to the horizontal layout (main row plus review/fix loop) once its
own container is at least 640 px wide, so it adapts to the column, not the window.

**Print.** Navigation, toasts and floating bars are hidden.

## Views

- **Studio**: capability strip and engine banner (from `/api/status`), project picker
  (synchronised with the top bar), prompt, attachments (drop zone, image thumbnails, tap to
  include/exclude, optional "upload ZIPs as base game"), run kind (generate / modify a slide),
  parent slide, engine preference, import-ZIP choice, recent runs.
- **Pipeline**: run picker, state headline with engine/model chips (a fallback model is
  flagged), the stage chart, controls that match the state (approve, resume, cancel, run tests
  on this device), state notices, the quality gate gauge, judge counters, measured model
  tiles, and the live log with category filters, a 2000-line cap and a follow toggle.
- **Preview**: the real game in a phone/tablet/desktop frame from a slide checkout or the live
  work tree, reload, fullscreen, open in a tab, and "Run tests" which loads the in-browser test
  harness and renders its posted results (per test, syntax checks, screenshots).
- **Slides**: the version strip with lineage connectors (branches on their own row), gate
  badge per slide, Preview / Stack change or Branch from here / ZIP / Deck, ZIP import.
- **Presenter**: the deployed Tokkio UI in a frame (only when its URL answered 2xx/3xx), or
  "Tokkio not deployed" with the reason (no answer, or the HTTP error status) and what Tokkio needs;
  below, the views and narration the overlay sends to the avatar.
- **Check**: the self-check table, platform facts, LAN addresses with QR codes, hub link.
- **Bench**: task selection, cost/time confirmation, progress, results, and the score.
- **Settings**: grouped form; only patchable paths are editable.

## Honesty patterns

These are deliberate, visible design rules rather than copy choices:

- **BLOCKED, not hidden.** With no available code engine the Studio banner turns red and reads
  `BLOCKED: no code engine` with the exact remedy (install Claude Code and run `claude login`,
  link a Kyora Hub, or add an Anthropic API key) and each engine's reason. A blocked run shows
  an amber BLOCKED notice with its reason and a Resume button.
- **`-` for unknown.** Metric tiles, the gauge, judge counters, the iteration counter and
  bench scores show `-` until a measurement exists; nothing is estimated in the browser. When
  the backend says token counts were not reported by the API, the tile says so.
- **Configured is not reachable.** The capability strip shows Nemotron and Kimi K3 as
  "configured" in the info colour, not green: reachability and latency are only claimed after
  Self-check measured them.
- **Resolved model, not requested model.** The Claude tile and run chips show the model the
  engine actually used, and flag "fallback from ..." when it differs.
- **Estimates say so.** The code-engine tile shows Claude Code's `total_cost_usd` as
  "API-equiv. est." (`~$`), explained as the CLI's list-price estimate, never as a measured cost.
- **Labels from the run, not from constants.** The stage chart's Gate node and fix-loop label, the
  gauge and the gate texts use the threshold the run recorded (`run.gate.threshold`, the gate
  result's `threshold`); Build/Fix and the code-engine tile name the run's backend (`engine.session`,
  `run.engine.backend`) and its recorded effort (`run.engine.effort`, the BUILD/FIX
  `stage.progress{backend, effort}`). Texts about the next run (prompt help, Honesty card) show the
  configured Settings values. No label says "94 %", "effort max" or "Claude Code" unless the run did.
- **No badge without a measurement.** A slide shows a green gate badge only when its gate
  passed at or above the threshold; otherwise "below 94%" with the measured rate, or "no gate"
  for imports. The Bench certified badge and the large pass-rate number appear only when the
  API reports `certified: true`; otherwise the panel says "Not certified" and why.
- **No fake avatar.** Without a reachable Tokkio deployment the Presenter draws no face, video
  or canvas; it explains the GPU/Kubernetes/NGC requirement and lists the exact payloads that
  would be sent.
- **No TOPS.** Compute in remote datacentres cannot be measured over an API, so no such number
  exists anywhere in the UI.
- **Secrets stay masked.** Keys are shown only as the server's masked form in placeholders;
  a secret is sent back only when the user types a new one. A hub link from the address bar is
  removed from the URL immediately and applied only after the user confirms the hub address.
- **Graceful gaps.** A backend module that is not loaded (HTTP 404/405) turns into an empty
  state that names the missing module instead of an error.
