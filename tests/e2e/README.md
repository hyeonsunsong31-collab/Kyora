# End-to-end tests

## `preview_runner.mjs`: browser test harness in headless Chromium

This test checks the in-browser test harness (`kyora/web/runner/harness.html`,
`kyora-test-harness.js`, `kyora-test-api.js`) and the optional headless driver
(`kyora/runners/headless_browser.mjs`). It does not need the Kyora server: a small
`node:http` server inside the script serves only the routes the harness uses.

| Route | Served from |
|---|---|
| `GET /runner/p1/<ref>` | `kyora/web/runner/harness.html` |
| `GET /static/runner/*` | `kyora/web/runner/*` |
| `GET /preview/p1/v1/*` | `tests/fixtures/harness-game` (the good game) |
| `GET /preview/p1/v2/*` | temporary copy of the harness game with syntax errors in `src/game.js` and `src/util.mjs` |
| `GET /preview/p1/v3/*` | temporary copy of the harness game without `tests/browser-tests.js` |
| `GET /preview/p1/v4/*`, `v5/*` | `tests/fixtures/game-ok`, `tests/fixtures/game-broken` (only when present) |
| `POST /api/runs/<rid>/browser-results`, `POST /api/projects/p1/slides/<n>/browser-results` | stub that records the posted body |

### Run it

From the repository root:

```sh
NODE_PATH=$(npm root -g) PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/e2e/preview_runner.mjs
```

You need Node 18 or newer, the `playwright` npm package, a Playwright Chromium build and
`python3` on `PATH` (one scenario passes the real browser payload to
`kyora.pipeline.quality`). The script finds `playwright` next to the script, then in
`NODE_PATH`, then under `npm root -g`. If `PLAYWRIGHT_BROWSERS_PATH` is unset and
`/opt/pw-browsers` exists, the script uses that folder. If the default launch fails, it
falls back to `/opt/pw-browsers/chromium-*/chrome-linux/chrome`.

Exit code 0 means every scenario passed. Each scenario prints an `ok` or `FAIL` line.

### What it checks

1. **Good game.** `summary.total > 0` and all tests pass. The run produces two
   screenshots (`start`, `end`) as PNG data URLs. The syntax checks pass for the script
   (`new Function`) and for both modules (`import()` trampoline). The POST body equals
   `window.__kyoraResult`.
2. **The Python side accepts the payload.** `BrowserResultsCollector` validates the real
   payload, stores the screenshots as PNG files plus `iter-1/browser.json`, and
   `to_checks()` maps it to gate checks.
3. **Broken game.** The failed syntax checks for `src/game.js` and `src/util.mjs` are
   reported, the tests fail, and the harness still finishes and posts.
4. **Missing tests file.** The harness reports a failed `harness:no-tests` result with
   `summary.total == 0`. With no `run` parameter, it posts to the slide endpoint
   (`ref` `v3`).
5. **Preview not found.** The harness reports `harness:load` (HTTP 404) and still sends a
   complete message.
6. **Manual mode.** Without `auto=1` nothing runs until the "Run tests" button is pressed.
   Without `post=1` nothing is posted.
7. **`headless_browser.mjs`.** It prints the result JSON and exits 0.
8. **`game-ok` / `game-broken` (when present).** For `game-ok` every test and syntax check
   passes. For `game-broken` failures are reported and the harness does not crash.

Runtime is about 15 s; most of it is the broken game's `waitFor` timeouts.

## Headless driver (optional)

Kyora never needs a headless browser. Any phone or desktop browser that opens
`/runner/<pid>/<ref>?run=<rid>&iter=<n>&auto=1&post=1` runs the same tests and posts the
same result. On machines with Node and Playwright, the driver can run the harness without
a visible browser:

```sh
node kyora/runners/headless_browser.mjs "http://127.0.0.1:8765/runner/<pid>/<ref>?run=<rid>&iter=1&auto=1&post=1" --timeout 120000
```

It prints `window.__kyoraResult` as one JSON line and exits 0. If no result arrived, it
prints `{"error": ...}` and exits 1.

## Other files here

`ui_driver.py` and `ui_smoke.mjs` are the UI smoke test, maintained with the web UI; see
their file headers.
