# Claude Code engine, judge hook and judge

How Kyora drives the Claude Code CLI as its code engine, how every tool call is
judged, and what the real-CLI smoke test observed. Code:
`kyora/models/claude_code.py` (`ClaudeCodeEngine`), `kyora/runners/kyora_hook.py`
(PreToolUse hook), `kyora/pipeline/judge.py` (`JudgeService`). Tests:
`tests/test_claude_code_parser.py`, `tests/test_hook.py`, `tests/test_judge.py`,
`tests/test_claude_code_smoke.py` (real CLI, opt-in), fixtures in
`tests/fixtures/stream_json/`.

## 1. Availability

`availability()` (cached 60 s) resolves `cfg.command[0]` on `PATH` (or as a path),
runs `<cmd> --version` and `<cmd> auth status --json`:

| Outcome | `available` | `reason` |
|---|---|---|
| command not found | false | `'claude' not found on PATH (install Claude Code and run 'claude auth login')` |
| `--version` fails | false | `'claude --version' failed (rc=N): <stderr tail>` |
| `loggedIn: false` | false | `Claude Code is not logged in: run 'claude auth login' (subscription)` |
| `loggedIn: true` | true | `""`; `details` has `version`, `auth_method`, `api_provider`, `path` |
| no `auth status` (old CLI) | true | `login state unknown ('claude auth status --json' unsupported)` |

Observed on the smoke host: `version` `2.1.300 (Claude Code)`, `auth_method`
`oauth_token`, `api_provider` `firstParty`.

## 2. Command line

One process per attempt, `cwd` = the job workspace, `stdin` = `DEVNULL` (or a
pipe carrying the prompt, see below), its own process group.

```
<cfg.command...> -p <prompt>
  --model <model> [--fallback-model <m2>,<m3>]
  --effort <job.effort|cfg.effort|max>
  [--max-turns <job.max_turns>]                         (omitted when max_turns is 0)
  --output-format stream-json --verbose
  --permission-mode acceptEdits --permission-prompts none
  --allowedTools Read,Write,Edit,MultiEdit,Bash,Glob,Grep
  --tools Read,Write,Edit,MultiEdit,Bash,Glob,Grep          (the only tools the session gets)
  --strict-mcp-config                                       (no MCP servers from the host)
  [--append-system-prompt-file <run_dir>/system_prompt.md]   (when a system prompt is given)
  --settings <run_dir>/claude_settings.json
  [--resume <session_id>]
  <cfg.extra_args...>
```

* `--bare` is never passed (it skips hooks and never reads OAuth/keychain, i.e. it
  would disable both the judge and the subscription login).
* The prompt is the positional argument right after `-p`. A prompt that starts
  with `-` gets a leading space so it is not parsed as an option. A prompt longer
  than `PROMPT_ARGV_LIMIT` (24 000 characters; Windows command lines are limited
  to 32 K) is written to the child's stdin instead and `-p` is followed directly
  by `--model`.
* `--fallback-model` lists the models after the current one in
  `[job.model] + job.fallback_models` (duplicates and empties dropped).
* Paths in argv and in the settings file use forward slashes.
* Logs and events never contain the prompt: `describe_argv()` replaces the text
  after `-p` and after `--append-system-prompt` with `<text>`.

Exact argv of the smoke run (attempt 2, paths shortened):

```
/opt/node22/bin/claude -p <text> --model claude-opus-5-5 --fallback-model claude-sonnet-5-5
  --effort max --max-turns 8 --output-format stream-json --verbose --permission-mode acceptEdits
  --permission-prompts none --allowedTools Read,Write,Edit,MultiEdit,Bash,Glob,Grep
  --append-system-prompt-file <KYORA_HOME>/projects/p_smoke/runs/r_smoke/system_prompt.md
  --settings <KYORA_HOME>/projects/p_smoke/runs/r_smoke/claude_settings.json
```

CLI 2.1.300 accepted every flag: `--max-turns` and `--append-system-prompt-file`
are not listed in `claude --help` but were accepted (no compat retry happened);
`--permission-prompts none` is listed ("anything that would prompt is denied
automatically").

### Files written per run (`run_dir` = `KYORA_HOME/projects/<pid>/runs/<rid>/`, or `judge_context["run_dir"]`)

| File | Content |
|---|---|
| `prompt.txt` | the prompt |
| `system_prompt.md` | the appended system prompt |
| `claude_settings.json` | the settings below |
| `engine/stream.jsonl` | every stdout line of every attempt, each attempt preceded by `{"type":"kyora.attempt","n","ts","argv"}` (argv via `describe_argv`) |
| `engine/stderr.log` | the CLI's stderr |
| `engine/judge.jsonl` | one line per judge verdict (written by `JudgeService`) |

## 3. Settings file (`--settings`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          {"type": "command", "command": "\"<python>\" \"<KYORA_HOME>/bin/kyora_hook.py\"", "timeout": 300}
        ]
      }
    ]
  },
  "permissions": {"deny": ["WebFetch", "WebSearch"]}
}
```

`<python>` is `sys.executable` of the Kyora process, `matcher` is
`"|".join(claude_code.judge_tools)` (default above). `install_hook()` copies the
packaged `kyora/runners/kyora_hook.py` to `KYORA_HOME/bin/kyora_hook.py` when the
content differs (also from `kyora.pyz`) and marks it executable on POSIX.

## 4. Child environment

The parent environment, minus the variables that describe a parent Claude Code
session (`STRIP_ENV`: `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_REMOTE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`,
`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PID`, `CLAUDE_CODE_WORKER_EPOCH`,
`CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`,
`CLAUDE_SESSION_INGRESS_TOKEN_FILE`, `CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2`,
`CLAUDE_CODE_TEE_SDK_STDOUT`, `CLAUDE_CODE_REMOTE_SDK_URL`, `CLAUDE_EFFORT`),
then `job.env` (a `None` value unsets a variable), then:

| Variable | Value |
|---|---|
| `KYORA_JUDGE_URL` | `<app.server_url>/api/judge` (empty when the server URL is unknown) |
| `KYORA_RUN_ID` | `job.run_id` |
| `KYORA_HOOK_TOKEN` | per-run token from `judge.register_context()` (empty without a judge) |
| `KYORA_HOME` | Kyora's home |

Auth and provider variables (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_*`, ...) are passed through. Why the
strip list exists: in the first real run (started from inside a Claude Code
session, only `CLAUDECODE` stripped) the child's `init` carried the *parent*
session's `session_id`.

## 5. Hook protocol (`kyora_hook.py`, standalone, stdlib only, Python 3.8+)

Same protocol as `examples/hooks/bash_command_validator_example.py` of the Claude
Code repository: JSON on stdin; exit 0 = allow; exit 2 = block, stderr is fed
back to Claude; exit 1 = non-blocking error (the tool proceeds).

1. stdin: `{"session_id","cwd","hook_event_name":"PreToolUse","tool_name","tool_input","tool_use_id",...}`.
   Not JSON or not an object -> message on stderr, **exit 1**.
2. `KYORA_JUDGE_URL` unset/empty -> line in `KYORA_HOME/logs/hook.log`, **exit 0**.
3. `POST KYORA_JUDGE_URL` (system proxies bypassed) with
   `{"hook_token","run_id","session_id","cwd","hook_event_name","tool_name","tool_use_id","tool_input"}`;
   timeout `KYORA_HOOK_TIMEOUT_S` (default 280 s, below the settings' 300 s).
4. Verdict `{"decision":"deny","reason","source"}` -> stderr
   `Kyora judge (<source>) denied <tool>: <reason>`, **exit 2**.
   Any other decision -> **exit 0**.
5. Fail-open, each logged to `hook.log` and **exit 0**: connection refused /
   timeout / DNS errors, any non-200 status (500, 403 bad token, ...), a body
   that is not a JSON object.

Fail-open is visible, never silent: after each `tool_result` of a judged tool the
engine asks `judge.has_verdict(run_id, tool_use_id)`; without a journaled verdict
it emits `judge.unavailable{reason,tool_name,tool_use_id}` **and** counts the call
on the run via `judge.record_unavailable(run_id)`, so `run.judge.unavailable`
matches the number of unjudged calls (the Messages-API engine does the same when
`judge.decide` raises). It also emits `judge.unavailable` once at start when there
is no judge or no server URL.

What Claude sees for a block (CLI 2.1.300, real): an error `tool_result`
`PreToolUse:Write hook error: ["<python>" "<KYORA_HOME>/bin/kyora_hook.py"]: Kyora judge (rule) denied Write: <reason>`,
and the blocked call is listed in the result line's `permission_denials`.

## 6. Judge (`JudgeService`)

`POST /api/judge` (auth `loopback`) calls `handle_request(payload)`:

* payload not an object -> `400 {"error","decision":"deny"}`;
* unknown run, missing or wrong `hook_token` (constant-time compare of UTF-8 bytes) -> `403`;
* missing `tool_name` -> `400`;
* otherwise `200` + verdict `{"decision","reason","risk","source","judge_model","latency_ms"[,"rule"|"cached_from"]}`.

`register_context(run_id, ctx)` returns the token; `forget(run_id)` drops
context, token and cache (stats and verdict ids are kept). The context keys the
rules read: `workspace`, `run_dir`, `project_id`, `files` / `plan.files`,
`constraints` / `plan.constraints`.

Decision order in `decide(run_id, call)`:

1. **Rules** (`source: "rule"`, no model):
   * read-only tools (`Read`, `Glob`, `Grep`, `LS`, `NotebookRead`, `TodoWrite`, `Task`, ...) -> allow;
   * `WebFetch`, `WebSearch` -> deny;
   * file tools (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`): no path -> deny; path
     escaping the workspace (absolute elsewhere, `..`, another Windows drive, `~`,
     `$VAR`, a symlink or dangling symlink leading outside) -> deny; resolved path
     under `.git/` or `.claude/`, or the workspace root -> deny; a planned file ->
     allow; `index.html`, `README*`, `src/`, `assets/`, `tests/`, `.kyora/` -> allow;
     any other in-workspace path -> defer;
   * `Bash`: empty -> deny; `sudo`/`doas`, `git push`, `git remote add|set-url`,
     git ref deletion/rewrites, `chmod 777`, fork bomb, `mkfs`, `dd` to devices,
     power commands, `killall`/`kill -9 -1` -> deny; when
     `constraints.no_external_deps`: `curl`/`wget`, pip/npm/npx/yarn/pnpm/bun
     installs, `git clone|fetch|pull`, system package managers -> deny;
     recursive `rm` of `/`, `~`, `.`, `..`, `*`, `$VAR`, anything outside the
     workspace (tracking `cd`), the workspace or `.git` -> deny; a chain of
     read-only commands (`ls`, `cat`, `grep`, `find` without `-exec/-delete`,
     `node --check`, `git status|diff|log|show|...`, ...) without redirection or
     command substitution -> allow; anything else -> defer.
2. **Cache** (`source: "cache"`, `cached_from: "kimi"`): per run, keyed by
   `sha256(tool_name, normalized input)`; only Kimi verdicts are cached (LRU 512).
3. **Kimi-K3** (`source: "kimi"`): `kimi.judge(ctx, call, run_id)` must return
   `{"decision":"allow|deny","reason","risk"}`.
4. **Fail-open** (`source: "unavailable"`, decision allow, risk `unknown`) when
   Kimi is not configured, raises, or returns no usable verdict; each case
   publishes `judge.unavailable{reason,tool_name,tool_use_id}`.

Every verdict: `judge.request` + `judge.verdict` events, one line appended to
`<run_dir>/engine/judge.jsonl` (or `KYORA_HOME/logs/judge.jsonl`), the
`tool_use_id` recorded for `has_verdict`, and the counters of
`stats(run_id)` = `{allowed, denied, unavailable, cached, rule, kimi}`.

## 7. stream-json -> engine events

| Line | Events |
|---|---|
| `system/init` | `engine.session{session_id,model,tools,permission_mode,cwd,backend}` + `engine.model_resolved` |
| `assistant` | `engine.model_resolved` when `message.model` changes (not for `<synthetic>`); per block `engine.text`, `engine.thinking`, `engine.tool_use{id,name,summary}` + `stage.progress{stage,pct,message}` (pct = tool uses / max_turns, capped at 95) |
| `user` tool_result | `engine.tool_result{tool_use_id,name,is_error,excerpt}` |
| `rate_limit_event` | no event; `status`, `resetsAt`, `errorCode`, `overageDisabledReason`, `rateLimitType` kept in `raw.rate_limits` |
| `result` | `engine.result{subtype,is_error,num_turns,duration_ms,cost_usd,result}` |
| other `system` subtypes (`thinking_tokens`, `hook_response`), `stream_event`, unknown | ignored |
| not JSON / not an object | counted in `raw.malformed_lines`, ignored |

`engine.model_resolved{requested,active,fallback_used}` is emitted once per model
change; `fallback_used` uses `same_model()`, where aliases (`opus`), `[1m]`
suffixes and `-YYYYMMDD` snapshot ids count as the requested model. stderr lines
become `engine.stderr` (at most 10 per 5 s, the rest counted).

## 8. Retries

At most `len(models) + 3` attempts; a cancel between attempts stops the loop.
Each retry emits `engine.compat_retry{reason,argv_change,model}` and is listed
in `raw.retries`.

1. **Compat** (`compat_retry_decision`, pure): stderr `unknown option '--max-turns'`
   -> drop `--max-turns` (the watchdog bounds the run);
   `'--append-system-prompt-file'` -> `--append-system-prompt <text>`;
   `'--permission-prompts'` -> drop it; `'--tools'` -> drop it (`--strict-mcp-config`
   still keeps MCP tools out); `'--strict-mcp-config'` -> drop it. One flag per retry,
   never twice.
2. **Model before init** (`model_retry_decision`, pure): no `init` line and the
   output mentions a model together with `not found|restricted|not available|invalid model|...`
   -> next model of `[model] + fallbacks`. An unrelated pre-init error (`file not found`) is not retried.
3. **Model refused after init** (`model_unavailable_after_init`, pure): the session
   ended with an `is_error` result, no tool was used, no message came from a real
   model, and the error says the model cannot be used (`switch to another model`,
   `credits_required`, `requires_usage_credits`, `out of usage credits`,
   `usage limit`, `model_not_found`, `not_found_error`, `permission_error`, or
   "model ... not found/not available/restricted/..."), or HTTP 403/404 mentioning a
   model -> next model. HTTP 401 never switches models. The CLI's own
   `--fallback-model` does not cover this case (observed, see 11).

`probe_model(model, fallbacks)` (selfcheck) runs `claude -p "Reply with exactly OK"
--max-turns 1 --output-format json ...` along the same chain and reports
`{ok, requested, active, fallback_used, refused:[{model,reason}], error, duration_ms, ...}`.

## 9. Timeouts and cancel

The engine's watchdog polls every 0.25 s:

* `cancel` set -> kill the process tree; `error: "cancelled"`;
* wall time since `run()` started (all attempts) > `timeout_s` -> kill;
  `error: "wall timeout after N s (claude_code.timeout_s=T)"`;
* no stdout line for `idle_timeout_s` -> kill;
  `error: "idle timeout: no output for N s (claude_code.idle_timeout_s=T)"`.
  Keep it above the hook timeout (300 s): the CLI prints nothing while a hook waits for Kimi.

Kill = `SIGTERM` to the process group (POSIX) / `taskkill /T /F` (Windows),
`SIGKILL` after 10 s. Timeouts also emit `engine.error`. `0` disables a limit.

## 10. EngineResult

* `ok` = result `subtype == "success"` and not `is_error`, not cancelled, no timeout.
* `error` (first match): `cancelled`; timeout text; spawn error; for an `is_error`
  result with text `<subtype or terminal_reason> (HTTP <status>): <text>` (the CLI
  reports API errors as subtype `success` + `is_error`); `<subtype or exit code N>:
  <stderr tail>`; `<subtype>`; `claude exited with code N without a result line`.
* `active_model`: the last model seen in `init`/assistant lines **only if a real
  model answered** (a message or a tool call); otherwise the matching key of the
  result's `modelUsage`; a model named in `init` that was refused is not active.
  `fallback_used` = `active_model` is not the requested model.
* `duration_ms` from the result line, else measured wall time; `cost_usd` =
  `total_cost_usd` as reported by the CLI; `permission_denials` from the result.
* `raw`: `rc, attempts, retries, compat, run_dir, stream, stderr, tool_uses,
  malformed_lines, model_usage, duration_api_ms, stderr_tail, api_error, rate_limits`.

## 11. Real smoke test: what was observed

`tests/test_claude_code_smoke.py` (skipped unless `KYORA_REAL_CLAUDE=1` and
`claude` on `PATH`): real `JudgeService(kimi=None)` with one extra test rule
(deny any write of `forbidden.txt`) behind `POST /api/judge` on a real
`KyoraServer` (127.0.0.1, ephemeral port); `ClaudeCodeEngine` with
`claude-fable-5-1` -> `claude-opus-5-5`, `claude-sonnet-5-5`, `--effort max`,
`--max-turns 8`, timeout 600 s, idle 300 s; temp git workspace; prompt "Create a
file named allowed.txt containing the word OK. Then create a file named
forbidden.txt containing the word NO. If a tool call is blocked, do not retry it;
just stop and report." `KYORA_SMOKE_KEEP=<dir>` keeps the artefacts.

Host: Claude Code CLI 2.1.300, OAuth subscription login (`apiKeySource: "none"`).
Real runs (UTC, 2026):

| When | Run | Observed |
|---|---|---|
| 10-14 22:15 | engine author's scratch script (before the test file; stub Kimi denied `forbidden.txt`) | `claude-fable-5-1` served (no fallback); 3 turns, `duration_ms` 14 773; `allowed.txt` written, `forbidden.txt` blocked by exit 2, listed in `permission_denials`; CLI-reported `total_cost_usd` 1.00287625 (`costBasis: list`). The child's `session_id` was the parent Claude Code session's id (CLAUDE_* env inherited; led to `STRIP_ENV`). `probe_model` the same day: fable answered "OK" in 6.2 s. |
| 10-15 02:53 | smoke test, run 1 | `init` with `claude-fable-5-1`; `rate_limit_event` `status: rejected`, `errorCode: credits_required`, `overageDisabledReason: out_of_credits`; a `<synthetic>` assistant message "You're out of usage credits. Switch to another model to continue."; result `subtype: success`, `is_error: true`, `terminal_reason: api_error`, `api_error_status: 429`, `api_error_code: credits_required`, `api_error: model_requires_usage_credits`, `modelUsage: {}`, 529 ms. The CLI did **not** use `--fallback-model`. The engine (then without post-init model switching) reported the error; test failed. Captured as `real_credits_refusal.jsonl`. |
| 10-15 02:57 | smoke test, run 2 (with the post-init model switch) | Attempt 1: same refusal for `claude-fable-5-1` (408 ms). `engine.compat_retry{argv_change: "--model claude-opus-5-5"}`. Attempt 2 served by **`claude-opus-5-5`** (`engine.model_resolved{active: claude-opus-5-5, fallback_used: true}`): Write `allowed.txt` -> rule `planned_file` allow; Write `forbidden.txt` -> smoke rule deny -> hook exit 2 -> error tool_result with the reason; Claude did not retry and reported the block. Result `success`, 3 turns, `duration_ms` 9 253, `duration_api_ms` 8 665, wall 12.0 s for both attempts, CLI-reported cost 0.0730166 (list basis), `permission_denials` = the blocked Write, judge stats `{allowed: 1, denied: 1, rule: 2, unavailable: 0}`, no `judge.unavailable`. All flags accepted (no compat retry). All assertions of the test held. Captured as `real_session.jsonl` and `real_{init,assistant_tool_use,user_tool_result,result}.jsonl`. |

Other facts from the captures: the `init` line listed 25 tools (environment
dependent, e.g. `Task`, `Skill`, `Monitor`, `Workflow`) and no `MultiEdit`;
`thinking` blocks arrived with empty text plus a signature, so `engine.thinking`
carried `""`; `system/thinking_tokens` lines and two `rate_limit_event` lines
(`status: allowed`, `rateLimitType: five_hour`) appeared in the served session.
The fixtures are sanitized (workspace -> `/WORKSPACE`, Kyora home ->
`/KYORA_HOME`, local lists and socket/memory paths removed, signatures redacted);
`tests/test_claude_code_parser.py` replays them through `run()` with a fake CLI
and reproduces the outcome above.

## 12. Known limits

* `--allowedTools` only pre-approves tools; it does not hide the others. A live run
  on CLI 2.1.300 showed the session receiving every MCP server and agent tool configured
  on the host (for example a desktop-commander `write_file`), none of which the judge
  hook matches. Kyora therefore also passes `--tools <the 7 built-ins>` and
  `--strict-mcp-config`, verified on that CLI to leave exactly
  `Bash, Edit, Glob, Grep, Read, Write` (it has no `MultiEdit`) and no MCP servers.
  On an older CLI that rejects one of these flags the compat retry drops it and says so
  in `engine.compat_retry`; anything left that would need a permission is still denied
  by `--permission-prompts none`.
* The judge rules are heuristics in front of Kimi, not a sandbox; when Kimi is not
  configured, deferred calls run unjudged (visible as `judge.unavailable`).
* A model refused *after* it already worked (usage limit mid-run) ends the run
  with the error; Kyora does not switch models mid-session.
