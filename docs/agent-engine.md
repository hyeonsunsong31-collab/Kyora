# The Kyora agent engine (BUILD and FIX without Claude)

The Kyora agent engine (engine name `openai_agent`, `kyora/models/openai_agent.py`) runs the BUILD and
FIX stages as Kyora's own tool loop on any OpenAI-compatible `/chat/completions` endpoint. By default
that is NVIDIA's API Catalog (`https://integrate.api.nvidia.com/v1`) with the NVIDIA keys from
`keys.json`, model `z-ai/glm-5.3`, then the configured fallback models. It needs no Claude
subscription and no Anthropic API key.

What it is not:

* **It is not Claude.** It runs a different model. Kyora does not claim that this model is as capable as
  Claude, and publishes no comparison number. If you want the same Claude models without a
  subscription, use an Anthropic API key (the `anthropic_api` engine, billed per token); see
  [`../README.md#without-a-claude-subscription`](../README.md#without-a-claude-subscription).
* **It does not lower the bar.** The quality gate (at least `quality.min_pass_rate` of the measured
  checks, 94 % by default, every required check, every planned acceptance test reported), the tests in
  a real browser, the review and the fix loop are the same for every engine. A game built by this
  engine is accepted only when it reaches the same measured bar. It may need more fix rounds or more
  time than another engine, or end below the gate (`failed_gate`).

## When it runs

`settings.engines.order` decides (default `["hub", "claude_code", "anthropic_api", "openai_agent"]`):
the first *available* engine runs a BUILD/FIX. With the defaults the agent engine therefore runs

* when no Hub is linked, Claude Code is missing or not logged in, and the Anthropic API engine is not set
  up (no key or no `anthropic` package): a phone, or a PC without Claude Code;
* when a run picks it in the *Code engine* field ("Kyora agent (NVIDIA models, no Claude)", run option
  `engine_pref: "openai_agent"`);
* after a failover: another engine stopped for an account or service reason (usage limit, ended
  subscription, credits, login, refused keys, unreachable service, retired model) and
  `engines.failover` is on. See [Failure kinds and failover](#failure-kinds-and-failover).

It counts as available when `agent_engine.enabled` is true, an endpoint is configured and an API key for
it exists. Availability is checked without network traffic; whether the endpoint really answers is
measured by the full self-check and by the run itself.

## How one BUILD or FIX runs

1. Kyora writes the same contract files into the work tree as for every engine (`CLAUDE.md`,
   `KYORA_PLAN.md`, `KYORA_TESTSPEC.md`, `DESIGN.md`, `.kyora/manifest.json`).
2. The system prompt is Kyora's engine prompt (`kyora/pipeline/templates/claude_system.md`, the same
   for every engine) plus a short environment note: the workspace path, how each tool behaves on this
   device, the per-answer token limit and the `kyora_check` tool. The user message is the BUILD or FIX
   prompt (for a FIX: the failing checks and the review - by the Kimi-role model, or by Kyora's rules
   from the failing checks when no review model could answer). Both are saved as `<run>/system_prompt.md`
   and `<run>/prompt.txt`.
3. Each turn is one streamed request with the conversation so far, the function tools, `tool_choice:
   "auto"`, `agent_engine.max_tokens`, `temperature` and `top_p`. The tool calls of an answer run in
   the order given; every result goes back as a `tool` message with the call's id (missing or
   duplicate ids are made unique, so the pairing stays valid).
4. The loop ends when the model answers without a tool call. Before it accepts that:
   * If `index.html` or a file the plan names does not exist yet, the model is asked to create it
     (and to run `kyora_check`), at most 2 times per call. If files are still missing, the engine
     finishes and says which (`stage.progress`: "still missing: ... (the quality gate measures it)").
   * An answer whose **text** was cut off at `max_tokens` without a tool call is answered with
     "continue", at most 3 times in a row; then the call ends with an error. Arguments of a tool call
     that were cut off are returned to the model as an "invalid JSON arguments" error that says so and
     asks for smaller parts.
   * An answer cut off **while the model was still thinking** (no text, no tool call) is not answered
     with "continue" (chat templates drop old reasoning, so the same request would come back): the
     same turn is sent again with thinking switched off (when the model has a switch); when thinking
     was off already, the next model of the chain continues the turn (the first one cools down for
     120 s, 30 minutes after an early stop, see below); only when no other model is left is `max_tokens` doubled (at most 32768); at most 3 such
     retries per turn. When the endpoint refuses the larger `max_tokens` (HTTP 400/422, the model's
     output or context limit), the call ends with an error that says the answer was cut off while
     thinking; no context retry is used up for it.
   * A model that was sent the thinking switch-off and still streams more than 8000 characters of
     reasoning with no text and no tool call, or more than 2000 once its request is 90 s old, is
     **stopped early** (live on 2026-10-23 GLM-5.3 reasoned 16384 tokens - 29 minutes - twice in one
     turn, in 2 of 2 runs that day; on 2026-10-24 it took 135-185 s to reach the 8000 characters): the
     next model of the chain that is not sidelined continues the turn, and the stopped model is
     **sidelined** for 30 minutes: it waits behind the others, an answer does not end that wait, and it
     does not stand in for a model that is only rate limited, overloaded or slow - the turn waits and
     retries that model instead (see the retries below). The sidelined model gets the turn's last try:
     after the last retry, or at once when the endpoint asks for a longer wait than a retry may take; a
     model that is gone (404/410) still hands over to it. These switches use none of the turn's 3
     thinking retries; the sidelining bounds them.
     When no model that is not sidelined is left, or a sidelined model stops the same way again, the
     call fails with `failure_kind: "model"` and the error names the models that could not take over.
     (Live on 2026-10-24, before this rule: Nemotron-3-Ultra answered 429 for a few seconds at a time,
     each time the turn went back to the stopped GLM-5.3, which reasoned again for 2-3 minutes, and the
     third time BUILD ended early; the same ended the first FIX iteration without progress.)
   * An answer that **writes a tool call as text** (`<tool_call>`, `<|tool_call...`, `<function=`, or a
     JSON `{"name": <tool>, "arguments": ...}`) ran nothing: the model is reminded to use function
     calling, at most 3 times. After that the model is skipped for this endpoint (its tool calls are not
     parsed there) and the next model of the chain continues the same conversation; with none left the
     call fails with `failure_kind: "model"`, so the pipeline can fail over.
   * A **first** final answer that called no tool at all, or is empty, is answered once with a
     reminder to do the work through the tools.
   * An answer ended with `finish_reason` `error` or `abort` (the server gave up on it) is dropped -
     its tool calls are not run - and the same turn is retried like a transient failure (below). An
     answer stopped with `content_filter` or `cancelled` ends the call with an error; its tool calls
     are never run.
5. Limits: `agent_engine.max_turns` model turns (`error_max_turns`) and `agent_engine.timeout_s` of wall
   clock for the whole call (`error_timeout`); one request may take at most 600 s (less when the wall
   clock has less left); at most 32 tool calls of one answer are run. Cancel is honoured while a
   request streams, between tool calls and inside a running `bash` command or `kyora_check` (the
   process tree is stopped at once); a tool never runs past the call's deadline.
6. There are no server-side sessions: every BUILD and every FIX starts a new conversation from the
   prompt and the files in the work tree. A FIX is never "resumed" (`engine.session` says so when a
   resume was asked for).

Malformed tool arguments are repaired only where nothing can be lost (an integer sent as a string, a
JSON-encoded `view_range`, arguments encoded twice); anything else goes back to the model as an error.

## Tools

| Tool | What it does | Judged as |
|---|---|---|
| `bash` | Runs one command with `<shell> -c` in the workspace root. Every call starts a fresh shell (`cd` and exported variables do not persist), runs at most 120 s (then its process tree is killed) and returns stdout and stderr, each captured up to 60000 characters while it runs and cut to 30000 characters (the middle is elided), plus the exit code when it is not 0. On macOS, Linux, Termux and iSH what the command left running in the background is stopped when it returns (its whole session); on Windows it is not (only a timeout or cancel ends the tree), and the tool description tells the model so. | `Bash` |
| `str_replace_based_edit_tool` | `view` (a file with line numbers, optional `view_range`; or a directory listing two levels deep, hidden entries and `node_modules` left out, at most 1000 entries), `create` (the whole file), `str_replace` (`old_str` must occur exactly once), `insert` (`new_str` after line `insert_line`, 0 = top). `view` and the edits read UTF-8 text files of at most 4 MB. | `create` = `Write`, `str_replace`/`insert` = `Edit`, `view` = `Read` (not judged) |
| `kyora_check` | Runs Kyora's static checks on the work tree (entry page, manifest, HTML, JSON and JavaScript syntax, local-only dependencies, size, test ids) with the run's plan, and, when Node is installed, the Node logic tests (`node --test tests/logic-tests.mjs`), as the TEST stage does. Returns the failing checks (at most 40 lines), or "all N checks pass", the checks deferred because Node is missing, and a reminder that the in-browser acceptance tests are not measured here. Off with `agent_engine.self_check_tool: false`. | the Node run as `Bash` (`node --test tests/logic-tests.mjs`, with the test file's content); the static checks run no model code and are not judged |

`bash` and the editor are the same code as the Anthropic Messages-API engine's tools
(`kyora/models/agent_tools.py`, class `ToolLoopEngine`).

**Workspace confinement (editor).** Paths are taken relative to the workspace root; an absolute path
must lie inside it. Symlinks are resolved first (dangling ones too), so a link that points outside is
an escape. Paths with `~`, NUL or URL-encoded traversal (`%2e`, `%2f`, `%5c`) are refused, and so is
any write under `.git/`. Before a file is overwritten or edited, its previous content is copied to
`<run>/engine/editor-backups/`, outside the workspace.

**Shell (bash).** The command starts in the workspace root but a shell is not a sandbox: what keeps it
in its lane is the judge (local rules deny path escapes and destructive commands such as `sudo`,
`git push`, writes into `.git/` and reads of Kyora's key files; the Kimi-role model decides what the
rules do not settle). Its environment is an **allowlist** (`kyora/util/envsafe.py`): what a shell, Node
and npm need (`PATH`, `HOME`, the locale, temp directories, the Windows system variables, `PREFIX`,
`NODE_PATH`, the proxy and CA variables, `LC_*`, `ANDROID_*`, `TERMUX_*`), minus any value that looks
like a credential whatever its name (`sk-...`, `nvapi-...`, `ghp_...`, a URL with a password). Nothing
else of Kyora's environment reaches it, and the keys in `keys.json` are never put into it. Everything a
command prints is passed through Kyora's redaction before the model, the log or the journal sees it:
every configured key and token, and every credential-looking value of Kyora's own environment, is
replaced by `[redacted <label>]`.

**kyora_check** writes no file itself. The Node logic tests it runs are files in the workspace that
were written through the judged editor or a judged command, and running them is judged once more as
the `Bash` command `node --test tests/logic-tests.mjs` with the file's content (a denial returns the
judge's reason instead of a result). Node runs them as the TEST stage does: with the allowlisted
environment, bounded output (200000 characters per stream, head and tail kept), the run's cancel, and
the output redacted. Its result helps the model; it is not part of the gate: TEST and GATE measure
everything again after the engine has finished.

## The judge

Each `create`, `str_replace` and `insert` (as `Write` / `Edit`) and each `bash` command (as `Bash`) is
decided by Kyora's judge service before it runs, in process (no hook): the same local rules, verdict
cache and Kimi role as for the Claude Code hook ([`claude-code-engine.md`](claude-code-engine.md)).
`view` and the static part of `kyora_check` are not judged; its Node run is (see above). A denied call returns `Denied by the Kyora judge: <reason>`
to the model as an error and is listed in the run's `permission_denials`. When the judge itself fails,
the call runs (fail-open), `judge.unavailable` is published with `ran_unjudged: true` and the run's
judge statistics count it.

On a phone the agent engine runs on the phone, so the phone's own judge (and its NVIDIA keys) decide.

## Endpoint, keys and models

### Which endpoint and key

| `keys.agent.base_url` | `keys.agent.api_key` | Endpoint | Keys, in the order tried |
|---|---|---|---|
| empty | empty (default) | `nvidia.base_url` | the Kimi-slot key ("kimi key"), then the Nemotron-slot key ("nemotron key") - each only when that role's own endpoint is `nvidia.base_url` |
| empty | set | `nvidia.base_url` | that key only ("agent key") |
| another URL | set | that URL | that key only; no NVIDIA key is ever sent there |
| another URL | empty | that URL | none: the engine is unavailable ("no API key for the agent endpoint") |
| same as `nvidia.base_url` | empty | `nvidia.base_url` | the NVIDIA keys, as in the default row |
| another URL | empty, and a role's endpoint is that URL | that URL | that role's key |

Only the host is shown in events and in self-check (`endpoint`), never the path or the key; keys are
named by their label. A role's key is sent to the agent endpoint only when that role uses the same
endpoint: with the Kimi role moved to `https://api.moonshot.ai/v1` and a Moonshot key in its slot, the
agent engine on NVIDIA's endpoint uses the Nemotron-slot key alone, and the Moonshot key never reaches
NVIDIA.

### Key fallback

A key the endpoint refuses (HTTP 401 or 403) is replaced by the next configured key for the same
request; the switch holds for the rest of that BUILD/FIX call. It is published as an `engine.stderr` line with
`kind: "key"`, `from_key`, `to_key`, `status` ("the endpoint refused the kimi key (HTTP 403); the agent
engine now uses the nemotron key"), with labels only, never key material.

### Retries

HTTP 429 and 5xx answers, an error object that arrives inside a 200 stream before any output
(NVIDIA's free tier sends "Service temporarily overloaded" that way), and a connection that fails before
any answer (reset, DNS, a dropped TLS stream; not a refused connection or a timeout, which would only double a wait the
caller bounded) are retried by the client: up to 4 requests in total, waiting 0.5, 1 and 2 s (for an
HTTP error with `Retry-After`: what it asks). An error after output was delivered is not retried,
because a retry would repeat that output. When `Retry-After` asks for more than 60 s, nothing is
retried: the error (with that wait) goes up at once, so the engine fails over instead of sending
requests the endpoint already said it would refuse.

On top of that the engine retries a **turn** after a transient failure (the endpoint unreachable, 429,
5xx, an answer ended with `error`/`abort`): up to 6 times in a row, waiting 5, 10, 20, 40, 60, 60 s or
what the endpoint asked (`Retry-After` or wording such as "try again in 250ms"), never longer than 60 s
and never when the wait would end within 10 s of the call's deadline. Each retry is an `engine.stderr`
line with `kind: "retry"`. Settings changed during a retry never mix endpoints: endpoint and keys
change in one step, and a request whose endpoint changed meanwhile is not resent.

### Model chain

`agent_engine.model` comes first, then `agent_engine.fallback_models` in order (default
`z-ai/glm-5.3` -> `nvidia/nemotron-3-ultra-550b-a55b` -> `nvidia/nemotron-3.5-super-vl-120b-a12b` ->
`nvidia/nemotron-3-super-120b-a12b`). Nemotron-3-Ultra answered most turns of the live runs of
2026-10-24 that passed the gate; Nemotron-3.5-Super-VL was chosen from that day's tool-call probe (it
answered a function call) and then answered 31 of 213 agent turns in one live run, among them about
half of the FIX iteration that passed the gate (one run, not a benchmark); Nemotron-3-Super answered 404
("not found for account") for the committed key that day and stays last for accounts that still have it. The next
model is tried when

* the endpoint says the model is gone (HTTP 404 or 410): it is skipped for good while the endpoint, the
  model and the fallback list stay the same (the chain is kept across runs; it starts over when every
  model was found gone, or after a run failed because no model could serve);
* it stays temporarily unavailable after the client's retries (HTTP 500/502/503/504 or overload wording),
  is rate limited (HTTP 429 without credit or billing wording: NVIDIA's free tier limits each model
  separately), or sends nothing within the request's time limit (slow or overloaded; live on 2026-10-23 GLM-5.3 took
  26 s for a 1-token chat): it cools down for 120 s, moves behind the healthy models and returns to its
  place afterwards;
* it refuses function tools (HTTP 400/422 that names tools or functions) before it has answered in this
  run: it is skipped like a gone model, because this engine cannot work without tool calls;
* it keeps writing its tool calls as text after 3 reminders (the endpoint does not parse them): it is
  skipped like a gone model and the next one continues the same conversation.

A refused key (401/403), an unreachable endpoint and a cancel never change the model.

Every switch is published as an `engine.stderr` line with `kind: "model"`, `from_model`, `to_model`,
`status`, `reason` and `temporary` ("agent model X answered HTTP 410 (...); trying Y"), and when the
answering model changes, as `engine.model_resolved {requested, active, fallback_used, reason?}`. The
code-engine tile shows the answering model and "fallback from <requested>"; `run.json` records
`engine.active_model` and `engine.fallback_used`, and `extra.engine_runs` the model of every call.

### Thinking

`agent_engine.thinking` (default **off**) is sent as `chat_template_kwargs.enable_thinking` to
Nemotron-family models (both ways) and, when off, to GLM models (which think by default and read the
same switch). Other families get no thinking field and run with the endpoint's default. The default was
chosen from a live comparison on NVIDIA's free tier on 2026-10-18 (CI, one game, one run each, not a
benchmark): `z-ai/glm-5.3` without thinking built a game that passed the gate 28/28 on its first
iteration (BUILD about 71 minutes); with thinking it had not finished BUILD after 85 minutes;
`nvidia/nemotron-3-super-120b-a12b` built faster (9-16 minutes) but passed fewer checks. `engine.session.effort` is `thinking` when
the switch was sent to the first model of the call, otherwise `default`; the tile shows "code engine ·
thinking" in the first case. The engine has no Claude effort level.

### Context budget

The conversation is measured in characters (message content, echoed reasoning, tool-call arguments).
Before a turn that would exceed `agent_engine.context_chars`, older material is shortened, oldest first,
outside the 8 newest tool results and the turns that asked for them: tool results become
`[output elided to save context: N chars]`, then echoed reasoning of older answers is dropped, then
string arguments longer than 2000 characters of older tool calls (file contents already written) are
replaced by a marker. No message is removed, so each tool call keeps its result. When the endpoint
still answers HTTP 400/413/422 with context-length wording, the budget drops to 60 % of the current size
(never below 20000 characters) and, when that shortens something, the turn is retried, at most 2 times
per call (`engine.stderr` `kind: "context"`).

### Tokens and cost

Token totals are the endpoint's own `usage` numbers. These endpoints report no cost, so `cost_usd` stays
`null` and the tile shows "cost: not reported"; nothing is estimated.

## Failure kinds and failover

A call that fails after every key, retry and fallback is classified in the engine result
(`failure_kind`) so the pipeline can decide whether another engine might do better:

| What happened | `failure_kind` | What the pipeline does |
|---|---|---|
| The endpoint cannot be reached | `unavailable` | next engine in `engines.order` (`engines.failover`) |
| HTTP 401/403 for every configured key | `auth` | next engine |
| HTTP 402, HTTP 429 after retries, or quota/credit wording | `quota` | next engine |
| Every model gone (404/410), refusing tools, or writing its tool calls as text | `model` | next engine |
| 5xx / overload after retries on every model | `unavailable` | next engine |
| Max turns, wall timeout, 3 cut-off answers in a row, an answer cut off while thinking (also after the retries), an answer the endpoint stopped, other errors | none | task failure: no failover, the normal rules apply |
| Cancel | none | the run is cancelled |

With the default order the agent engine is last, so after its own account failure no engine is left and
the run is BLOCKED (resumable) with the reason, unless the engines changed the work tree and `index.html`
exists, in which case TEST and GATE measure what they produced. A block that passes by itself is resumed
automatically (`engines.auto_resume_on_limit`, at most 24 times per run): a reset time named in the error,
or, for a rate limit (HTTP 429 without credit or billing wording) or an outage of the service
(`unavailable`), after 2, 5, 15 and then 30 minutes (by the run's automatic resumes so far); the resumed
run tries that engine again once its wait is over. Refused keys, credits and retired models wait for
*Resume*.
Failover, partial work and automatic resume are described in
[`architecture.md`](architecture.md#code-engines-registry-order-failover-and-automatic-resume).

## Platforms

* **Windows**: the `bash` tool runs Git Bash, found like Claude Code finds it: `CLAUDE_CODE_GIT_BASH_PATH`,
  the `bash.exe` next to `git.exe`, `bash`/`sh` on PATH, `%ProgramW6432%`, `%ProgramFiles%`,
  `%ProgramFiles(x86)%\Git\bin\bash.exe` and `%LOCALAPPDATA%\Programs\Git\bin\bash.exe`; never the WSL
  launcher in `%SystemRoot%\System32` or a WindowsApps alias. `install.ps1` installs Git for Windows,
  which brings Git Bash. **Without any POSIX shell the `bash` tool is not offered at all**: the system
  prompt tells the model, and it works with the file editor and `kyora_check` only. Node for the logic
  tests comes with `install.ps1 -WithNode`.
* **macOS / Linux**: `bash`, else `sh`, else `/bin/sh`.
* **Android (Termux)**: runs on the phone (Python standard library, HTTPS to the endpoint). The shell is
  Termux's `bash`; `nodejs-lts` (installed by `install-termux.sh`) runs the Node logic tests.
* **iOS / iPadOS (iSH)**: runs on the phone too. The shell is whatever `bash` or `sh` exists (Alpine's
  BusyBox `sh` unless you `apk add bash`). There is no Node, so `kyora_check` reports the static checks
  and lists the Node checks as deferred, JavaScript syntax is checked later in the browser, and the Kimi role plans
  browser tests only. Everything runs under iSH's x86 emulation, so it is slow; keep iSH in the
  foreground. The unit tests of this engine run in the CI's 32-bit Alpine (iSH userland) job.
* **On a Kyora Hub**: a hub can run this engine for its clients: it runs a job on its first available
  local engine, Claude Code CLI, then the Messages-API engine, then the Kyora agent engine
  ([`hub-mode.md`](hub-mode.md)).

## Self-check

The item **Kyora agent engine (no Claude needed)**:

* Quick check: availability from Settings only (enabled, endpoint host, model and fallbacks, which key)
  - "configured: ...; not contacted".
* Full check (*Run full check*, `probe_models`): one small non-streamed request (thinking off for a
  Nemotron model, at most 1024 output tokens, 60 s for the whole chain) that asks the model to call a
  test function. **OK** when the model called it with JSON-object arguments; **WARN** when it answered
  without calling it ("pick another agent_engine.model"), when only a fallback model or the other key
  answered (it says which), or for a 429/5xx that may pass; **FAIL** with the provider's explanation for
  refused keys, gone models or no connection.
* When the agent engine is the selected engine, a missing Claude Code CLI or login is **WARN**, not FAIL,
  and says that BUILD/FIX run on the Kyora agent engine, "a non-Claude model; its output passes the same
  measured quality gate". The *Selected code engine* item names the engine, model and endpoint host.

## Settings

`settings.json`, group `agent_engine` (Settings tab: *Kyora agent engine*). Changes apply to the next
BUILD/FIX without a restart.

| Setting | Default | Meaning |
|---|---|---|
| `agent_engine.enabled` | `true` | Offer the engine. Off: it is never selected (self-check SKIP). |
| `agent_engine.model` | `z-ai/glm-5.3` | First model; needs function calling at the endpoint. |
| `agent_engine.fallback_models` | `["nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3-super-120b-a12b"]` | Tried in order (see [Model chain](#model-chain)). |
| `agent_engine.max_turns` | `80` | Model turns per BUILD/FIX call (positive integer). |
| `agent_engine.max_tokens` | `16384` | Output tokens per answer (positive integer). |
| `agent_engine.temperature` | `0.6` | Sampling temperature (0 to 2). |
| `agent_engine.top_p` | `0.95` | Nucleus sampling (validated: 0 to 2). |
| `agent_engine.thinking` | `false` | Thinking switch for models that read it (Nemotron family; GLM when off). See [Thinking](#thinking). |
| `agent_engine.timeout_s` | `5400` | Wall clock per BUILD/FIX call, in seconds (positive integer). |
| `agent_engine.context_chars` | `400000` | Context budget in characters (positive integer). |
| `agent_engine.self_check_tool` | `true` | Offer the `kyora_check` tool. |

`keys.json`, group `agent`:

| Key | Default | Meaning |
|---|---|---|
| `agent.base_url` | `""` | Another OpenAI-compatible `/v1` URL (http or https). Empty = `nvidia.base_url`. |
| `agent.api_key` | `""` | Sent only to the agent endpoint. Empty on the NVIDIA endpoint = the NVIDIA keys. |

Engine selection and failover (`settings.json`, group `engines`): `engines.order` (default
`["hub", "claude_code", "anthropic_api", "openai_agent"]`, names from that list without repeats),
`engines.failover` (`true`) and `engines.auto_resume_on_limit` (`true`).

Fixed in the code (not settings): bash timeout 120 s, tool output 30000 characters, 2 nudges, 3
"continue" answers, 8 newest tool results kept whole, 2 context retries, 600 s per request, 4 requests
per retried call, 120 s cooldown of a busy model, 60 s self-check probe.

## What a run records

* `<run>/engine/agent_turns.jsonl`: one line per turn (answering model, `finish_reason`, usage,
  measured time to first token and latency, characters streamed per kind, the answer text, reasoning
  cut to 20000 characters, the tool calls) and per tool round (results cut to 4000 characters); a failed
  turn lists the models tried with their status.
* `<run>/prompt.txt`, `<run>/system_prompt.md`, `<run>/engine/editor-backups/`.
* Events: `engine.session {backend: "openai_agent", model, requested_model, tools, cwd, permission_mode:
  "kyora-judge", effort, endpoint, key_label}`, `engine.model_resolved`, `engine.text`, `engine.thinking`,
  `engine.tool_use {id, name, tool, summary}` (`name` is the judged name: `Bash`, `Write`, `Edit`, `Read`
  or `KyoraCheck`), `engine.tool_result`, `engine.stderr` (key, model and context notes),
  `engine.error {error, failure_kind?}`, `engine.result {subtype, is_error, num_turns, duration_ms,
  cost_usd: null, result}` and `stage.progress` ("turn N (model)").
* `run.json`: `engine` (backend `openai_agent`, requested and active model, `fallback_used`, turns,
  duration, `cost_usd: null`, `permission_denials`, `tree_changed`, `failure_kind`) and the run's
  `extra.engine_runs` / `extra.engine_failures` / `extra.engine_failovers` entries.

## Honest limits

* A different model: no claim of equal capability, no benchmark number in this repository. The gate
  decides per project; Kyora Bench certifies only from a measured run on your setup.
* The free NVIDIA tier rate-limits and is sometimes overloaded; retries, cooldowns and fallbacks keep a
  run going, but when every model stays unavailable the call fails (`unavailable`) and the run blocks.
* The model needs function calling at the endpoint. A model that answers in text instead of calling
  tools produces nothing; the full self-check tests this with one real request.
* The default chain depends on what NVIDIA serves today; models can disappear (404/410) or be gated.
  NVIDIA's state as measured on 2026-10-18 is in
  [`../README.md#nvidia-status-measured-2026-10-18`](../README.md#nvidia-status-measured-2026-10-18).
* The CI job "live pipeline without Claude (NVIDIA models + Kyora agent engine)" runs one game from prompt
  to gate with `engines.order = ["openai_agent"]` against the live endpoint; its outcome is reported
  there, not here.
