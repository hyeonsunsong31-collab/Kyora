# Kyora architecture and module contracts

This file is the contract every module is written against. Foundation modules
(`kyora/config.py`, `kyora/server.py`, `kyora/app.py`, `kyora/pipeline/events.py`,
`kyora/models/base.py`, `kyora/util/*`) already exist; read them before coding.
**Standard library only** in `kyora/` (the optional Anthropic engine may import the
`anthropic` package behind an import guard). Python 3.10+.

## What Kyora is

An orchestration application. Four external systems in fixed roles over their own APIs:

| Role | System | Access |
|---|---|---|
| Text/design director: GDD prose, narrative, dialogue, UI/UX spec + design tokens + copy, presenter narration | NVIDIA Nemotron-3-Ultra | `https://integrate.api.nvidia.com/v1`, model `nvidia/nemotron-3-ultra-550b-a55b`, OpenAI-compatible chat completions |
| Planner + judge: intake (vision), plan + task graph + acceptance tests, judgement of the code engine's Write/Edit/Bash calls that local rules do not settle, final review | Moonshot Kimi-K3 | same endpoint, model `moonshotai/kimi-k3` |
| Code engine: implement, debug, test, build | Claude Code CLI at `--effort max` (subscription login) ; optional Messages-API engine ; remote Kyora Hub | subprocess / official `anthropic` SDK / HTTP |
| Presenter + custom-view output channel | NVIDIA Tokkio 5.0.0-ga (vendored, unmodified) + Kyora overlay processor | deployed by the user on an NVIDIA-GPU Kubernetes cluster |

Honesty rules: never fabricate model output, metrics or verdicts. A missing engine =
BLOCKED. Measured numbers only. "Kimi K3" is displayed in the UI (licence).

## Runtime layout (`KYORA_HOME`, default `~/.kyora`)

```
KYORA_HOME/kyora.db                         events + project index (sqlite)
KYORA_HOME/config/{keys.json,settings.json} seeded config when none found elsewhere
KYORA_HOME/bin/kyora_hook.py                extracted PreToolUse hook
KYORA_HOME/logs/
KYORA_HOME/projects/<pid>/project.json
KYORA_HOME/projects/<pid>/repo/             git working tree = engine cwd (the game)
KYORA_HOME/projects/<pid>/runs/<rid>/       run.json, intake.json, plan.json, design.json, ui_spec.json,
                                            prompt.txt, system_prompt.md, claude_settings.json,
                                            engine/{stream.jsonl,stderr.log,judge.jsonl},
                                            iter-<n>/{static,node,browser,gate}.json, iter-<n>/screenshots/*.png,
                                            review-<n>.json
KYORA_HOME/projects/<pid>/cache/slides/v<n>/ materialized checkout for /preview
KYORA_HOME/projects/<pid>/attachments/
KYORA_HOME/hub/jobs/<id>/
KYORA_HOME/bench/<timestamp>/
```

## Foundation APIs (exist)

- `kyora.config.Config`: `.nvidia` (base_url, nemotron{model,api_key}, kimi{model,api_key}), `.anthropic_api_key`,
  `.claude_code` (command, model, fallback_models, effort, max_turns, timeout_s, idle_timeout_s, resume_for_fixes,
  judge_tools, extra_args), `.kimi` (reasoning_effort, judge_reasoning_effort, effort_field), `.nemotron`
  (thinking_for_design), `.quality` (min_pass_rate, max_fix_iterations, browser_wait_s, require_plan_approval,
  auto_run_browser_tests), `.hub` (url, token, expose_ui), `.tokkio` (ui_url, configurator_url), `.server`
  (host, port, workers, preview_port, allowed_hosts); `to_public(reveal)`, `apply_patch(patch)`, `save()`.
- `kyora.pipeline.events.EventBus`: `publish(type, data, run_id=, project_id=, persist=)`, `subscribe(run_id=,
  project_id=, types=[...,'engine.*'])` -> Subscription(.queue, .close()), `replay(since_id, ...)`, `last_id()`,
  `wait_for(type, pred, timeout, run_id=)`. `DeltaCoalescer(bus, run_id=, project_id=, role=, model=)` for token deltas.
  Event type constants are defined there (RUN_STATE, STAGE_PROGRESS, MODEL_METRICS, ENGINE_TOOL_USE, JUDGE_VERDICT, ...).
- `kyora.models.base`: `Metrics`, `EngineJob`, `EngineEvent`, `EngineSink`, `BusSink(bus, run_id, project_id)`,
  `EngineResult`, `Availability`, `CodeEngine` (abstract `availability()`, `run(job, sink, cancel)`), `select_engine(app)`.
- `kyora.server`: `Request` (method, path, query, headers, body, client_ip; `.json()`, `.multipart()` -> `[Part]`,
  `.bearer()`, `.cookies()`, `.is_loopback()`), `Response.json/text/html/bytes/file/redirect/sse/error`, `Router.add(method,
  pattern, handler, auth='local'|'any'|'loopback'|'hub')` with `{name}` and `{name:path}` params, `event_stream(bus, ...)`,
  `resource_response(rel)` (`safe_resource_rel` treats `\\` as a separator and rejects `..`, `.`, empty segments, `:` and NUL), `lan_addresses()`.
  `KyoraServer(app, host, port, router, *, preview_port=None, preview_router=None)` runs two listeners on the same
  bind address: the main one (UI + API) and the **preview listener** (`server.preview_port`, `null` = port + 1; an
  ephemeral port when that is busy; `0` = ephemeral), a separate origin for untrusted game code whose router comes
  from `App.register_preview_routes`. Every request first passes the origin guard: `Host` must name this machine
  (loopback names, the address the request arrived on, LAN addresses/hostname/`.local`/FQDN when bound beyond
  loopback, `server.allowed_hosts`) else `403`; a non-GET/HEAD/OPTIONS request with an `Origin` must come from the
  listener's own origin (`null` refused) else `403`; `Request.json()` requires `application/json` (or `*+json`)
  for a non-empty body (`400`). Main-listener HTML gets `X-Frame-Options: SAMEORIGIN`. On Windows the listeners
  claim their ports exclusively (`SO_EXCLUSIVEADDRUSE`); on every platform the CLI holds
  `KYORA_HOME/kyora.lock` (`kyora/util/instance_lock.py`) before `app.start()`, so two Kyoras never share a home.
- `kyora.app.App`: attributes `config, home, bus, platform, store, nemotron, kimi, judge, claude_engine, anthropic_engine,
  hub_client, hub_worker, tokkio, run_manager, selfcheck, bench, browser_results, server_url, preview_url,
  preview_port, hub_mode, missing`; `engine()`; `status()` (includes `preview_url`, `preview_port`);
  `start(server_url, preview_url=None)`; `register_preview_routes(router, main_router)` (game files, harness,
  `/static/runner/*`, both browser-results POSTs, `/favicon.ico` - nothing else). `App.register_routes` calls
  `register_routes(router, app)` from `kyora.projects.routes`, `kyora.pipeline.routes`, `kyora.hub`, `kyora.selfcheck`,
  `kyora.bench.routes`, `kyora.models.tokkio` when present.
  Core routes already provided: `GET /`, `GET /static/{path}`, `GET /api/status`, `GET|PUT /api/settings`,
  `GET /api/events` (SSE: `?run=&project=&types=a,b,engine.*&since=`), `GET /api/events.json`, `GET /api/lan-urls`,
  `GET /api/qr.svg?text=`. `PUT /api/settings` refuses `claude_code.command` / `claude_code.extra_args` (they are
  argv Kyora executes; edited only in `settings.json`). The `kyora_token` cookie is `HttpOnly`.
- `kyora.util`: `fs` (atomic_write_json, read_json, safe_join, rm_tree, which, platform_info, kyora_home),
  `ids` (new_id, now_iso, slug), `sse` (encode_event, parse_sse), `procs` (popen, kill_tree, run).

## Constructors expected by `kyora/app.py`

```
NIMClient(base_url, api_key, *, user_agent)                             kyora/models/nim_client.py
Nemotron(client, model, bus, nemotron_cfg)                              kyora/models/nemotron.py
Kimi(client, model, kimi_cfg, bus)                                      kyora/models/kimi.py
JudgeService(kimi_or_None, bus, config, home)                           kyora/pipeline/judge.py
ClaudeCodeEngine(claude_code_cfg, home, app)  (+ install_hook())        kyora/models/claude_code.py
AnthropicAPIEngine(api_key, claude_code_cfg, judge, home)               kyora/models/anthropic_api.py
HubClientEngine(hub_cfg, bus) ; HubWorker(app)                          kyora/hub.py
Tokkio(tokkio_cfg, bus)                                                 kyora/models/tokkio.py
BrowserResultsCollector(home)                                           kyora/pipeline/quality.py
RunManager(app, workers)  (.start(), .stop(), .status(), .submit(run_id), .cancel, .approve, .resume)   kyora/pipeline/runner.py
SelfCheck(app)  (.run(probe_models=True, probe_claude=False) -> dict)   kyora/selfcheck.py
BenchRunner(app) (.list_tasks(), .estimate(ids), .run(ids, confirm, cancel))  kyora/bench/runner.py
ProjectStore(projects_root, db_path)                                    kyora/projects/store.py
```

## REST routes (by module)

`kyora/projects/routes.py`: `GET|POST /api/projects`, `GET|DELETE /api/projects/{id}`,
`POST /api/projects/{id}/attachments` (multipart `file` parts; `role` field), `GET /api/projects/{id}/attachments/{aid}`,
`GET /api/projects/{id}/slides`, `GET /api/projects/{id}/slides/{n}/download` (zip), `POST /api/projects/{id}/import`
(multipart zip -> new slide "imported"), `GET /api/projects/{id}/preview-check?ref=` (`{ok, ref, error}`, never file
contents), `GET /projects/{id}/deck` (deck HTML), `POST /api/projects/{id}/slides/{n}/browser-results`.
Preview listener only (`register_preview_routes`): `GET /preview/{id}/{ref}/{path:path}` (ref = `v3` or `run-<rid>` or
`work`), `GET /runner/{id}/{ref}` (browser test harness page), `GET /static/runner/*` (from kyora/web/runner) and both
browser-results POSTs. On the main listener `GET /preview/...` and `GET /runner/...` are `302` redirects to
`http://<request host>:<preview_port><same path and query>`. The UI builds the preview origin from
`/api/status.preview_port` and accepts `kyora:test-result` messages only from it; the headless driver opens
`app.preview_url + runner_url`; `tokkio.view.preview_url` is absolute on the preview origin.

`kyora/pipeline/routes.py`: `POST /api/projects/{id}/runs` (json: prompt, kind=generate|modify, parent_slide, attachment_ids,
options{engine_pref, import_zip_attachment}), `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`, `POST /api/runs/{id}/approve`,
`POST /api/runs/{id}/resume`, `POST /api/runs/{id}/browser-results`, `POST /api/judge` (auth=any; the per-run `hook_token` authenticates it, so the hook reaches it even when Kyora binds a LAN IP),
`POST /api/actions` (auth=local; Tokkio button actions), `GET /api/runs` (recent).

`kyora/hub.py`: `GET /api/code-engine/status`, `POST /api/code-engine/jobs` (multipart: `job` json + `workspace` zip),
`GET /api/code-engine/jobs/{id}`, `GET /api/code-engine/jobs/{id}/events?since=` (SSE), `GET /api/code-engine/jobs/{id}/result`
(zip), `POST /api/code-engine/jobs/{id}/cancel` - all `auth='hub'`.

`kyora/selfcheck.py`: `GET /api/selfcheck` (last report), `POST /api/selfcheck/run` (json: probe_models, probe_claude).

`kyora/bench/routes.py`: `GET /api/bench/tasks`, `POST /api/bench/run` (json: task_ids, confirm:true), `GET /api/bench/results`,
`GET /api/bench/badge`, `POST /api/bench/cancel`.

`kyora/models/tokkio.py`: `GET /api/tokkio/health`.

## Event envelope (SSE `data:` JSON)

`{"id": 1042, "ts": 1760450000.12, "type": "stage.progress", "run_id": "r_01", "project_id": "p_8f", "data": {...}}`

Types and payloads:
- `run.created|run.state{state,iteration}|run.blocked{reason}|run.completed{slide}|run.failed{error}|run.cancelled|run.awaiting_approval`
- `stage.started{stage}`, `stage.progress{stage,pct,message}` (BUILD/FIX start also carries `backend`, `effort`), `stage.finished{stage,ok,ms}`
- `model.request{role,model}`, `model.delta{role,model,content?,reasoning?}` (not persisted), `model.metrics{role,model,ttft_ms,total_ms,prompt_tokens,completion_tokens,tps,tokens_source}`, `model.done{role,model}`, `model.error{role,model,error,status}`
- `engine.session{session_id,model,tools,permission_mode,cwd,backend}`, `engine.model_resolved{requested,active,fallback_used}`, `engine.text{text}`, `engine.thinking{text}`, `engine.tool_use{id,name,summary}`, `engine.tool_result{tool_use_id,is_error,excerpt}`, `engine.result{subtype,is_error,num_turns,duration_ms,cost_usd,result}`, `engine.error{error}`, `engine.compat_retry{reason,argv_change}`, `engine.stderr{line}`
- `judge.request{tool_name,summary}`, `judge.verdict{decision,reason,source,latency_ms,tool_name}`, `judge.unavailable{reason}`
- `test.static{checks}`, `test.node{checks}`, `test.await_browser{runner_url,iteration}`, `test.browser_result{summary}`, `gate.result{threshold,passed,total,pass_rate,ok,required_failures}`, `fix.iteration{n}`, `review.decision{decision,reasons}`
- `slide.created{number,tag,sha}`, `deck.updated{url}`, `tokkio.narration{text}`, `tokkio.view{title,preview_url,screenshot_b64?,stages,buttons}`, `tokkio.clear`, `selfcheck.update`, `bench.progress{done,total,task_id,passed}`, `bench.done{score}`, `settings.changed{changed}`

## Data schemas

`project.json`
```json
{"id":"p_8f2a1c","name":"Sky Runner","created_at":"...","updated_at":"...","engine":"vanilla",
 "repo":{"backend":"git|snapshot","branch":"lineage/main"},
 "attachments":[{"id":"a_01","name":"ref.png","mime":"image/png","size":48213,"path":"attachments/a_01_ref.png","role":"reference"}],
 "slides":[{"number":1,"tag":"slide/v1","sha":"9c1e","parent":null,"run_id":"r_01","title":"Initial generation","created_at":"...",
            "gate":{"pass_rate":0.978,"passed":45,"total":46,"ok":true},
            "engine":{"backend":"claude_code","active_model":"claude-opus-5-5","fallback_used":true},
            "screenshots":["runs/r_01/iter-1/screenshots/start.png"]}],
 "current_slide":1}
```
When a code-engine call of the slide's run failed, the slide's `engine` also carries `"ok":false` and
`"engine_errors":[{"kind","iteration","backend","error","tree_changed"}]` (from `run.extra.engine_failures`);
the gate stays the measured result and the slide card shows a warning chip.

`run.json`
```json
{"id":"r_01","project_id":"p_8f2a1c","kind":"generate|modify","parent_slide":null,"prompt":"...","attachment_ids":["a_01"],
 "options":{"engine_pref":"auto","import_zip_attachment":null},
 "state":"created|intake|plan|awaiting_approval|design|build|test|gate|review|fix|finalize|complete|failed_gate|blocked|failed|cancelled",
 "iteration":1,"stages":{"intake":{"status":"done|running|failed|skipped","started":"...","finished":"...","metrics":{}}},
 "artifacts":{"intake":"intake.json","plan":"plan.json","design":"design.json","ui_spec":"ui_spec.json"},
 "engine":{"backend":"claude_code","requested_model":"claude-fable-5-1","active_model":"...","fallback_used":false,"session_id":"...",
           "num_turns":31,"duration_ms":812000,"cost_usd":null,"permission_denials":0,"effort":"max"},
 "iterations":[{"n":0,"kind":"build","commit":"...","gate":{"passed":41,"total":46,"pass_rate":0.891,"ok":false}}],
 "gate":{"threshold":0.94,"final":null},"judge":{"allowed":57,"denied":3,"unavailable":0,"cached":4,"rule":49,"kimi":7},
 "result_slide":null,"error":null,"blocked_reason":null,"created_at":"...","finished_at":null}
```
`plan.json` (Kimi output, validated by `kimi.plan`)
```json
{"title":"...","engine":"vanilla|phaser|three|zip-only","preview":true,"summary":"...",
 "files":[{"path":"index.html","purpose":"..."}],
 "contract":{"globals":["window.Game"],"api":{"Game.start()":"...","Game.getState()":"{score:number,lives:number,phase:'menu'|'play'|'over'}"}},
 "tasks":[{"id":"t1","title":"...","depends_on":[],"files":["src/game.js"],"done_when":"..."}],
 "acceptance_tests":[{"id":"at-01","name":"...","kind":"browser|node","steps":"...","required":true}],
 "constraints":{"no_external_deps":true,"max_total_mb":25,"touch_controls":true},
 "design_brief_for_nemotron":"..."}
```
`design.json`: `{gdd_markdown, narrative, dialogue:[{speaker,line}], presenter_intro}`;
`ui_spec.json`: `{tokens:{color:{},type:{},spacing:{},radius:{}}, screens:[{id,name,purpose,components:[...]}], copy:{}}`.

Judge request (hook -> `POST /api/judge`): `{"hook_token","run_id","session_id","cwd","hook_event_name":"PreToolUse","tool_name","tool_use_id","tool_input":{...},"deadline_ms":278000}`
Verdict: `{"decision":"allow|deny","reason":"...","risk":"low|medium|high","source":"kimi|rule|cache|timeout","judge_model":"moonshotai/kimi-k3","latency_ms":2210}`
(`deadline_ms` = the hook's wait budget, a little under `KYORA_HOOK_TIMEOUT_S`; a Kimi verdict slower than it is journaled `late`, never governs, and the route answers `source:"timeout"` allow.)

Browser test result (iframe -> harness -> `POST /api/runs/{rid}/browser-results`):
```json
{"type":"kyora:test-result","run_id":"r_01","iteration":1,"ref":"run-r_01",
 "results":[{"id":"at-01","name":"...","status":"pass|fail|skip","ms":18,"error":null}],
 "summary":{"passed":9,"failed":1,"skipped":0,"total":10},
 "syntax_checks":[{"file":"src/game.js","ok":true,"error":null,"method":"new Function|import()"}],
 "screenshots":[{"name":"start","data_url":"data:image/png;base64,..."}],
 "env":{"ua":"...","viewport":[390,760],"touch":true,"started_at":"...","duration_ms":12400}}
```
Game manifest written by the engine at `.kyora/manifest.json`: `{"entry":"index.html","engine":"vanilla|phaser|three","scripts":["src/game.js"],"modules":[],"tests":["tests/browser-tests.js"],"version":1}`

Bench task (`kyora/bench/tasks/<id>.json`): `{id,title,category,kind:"generate|modify",prompt,engine,timeout_min,constraints,contract,
checks:{static:[...],browser_tests_file,min_browser_tests},pass_rule:{min_pass_rate:0.94},fixture_zip?}`

## Claude Code invocation (claude_code.py)

argv (cwd = workspace, stdin DEVNULL):
```
<cfg.command...> -p <prompt> --model <model> [--fallback-model a,b] --effort max --max-turns N
  --output-format stream-json --verbose --permission-mode acceptEdits --permission-prompts none
  --allowedTools "Read,Write,Edit,MultiEdit,Bash,Glob,Grep" --tools "Read,Write,Edit,MultiEdit,Bash,Glob,Grep"
  --strict-mcp-config --append-system-prompt-file <run_dir>/system_prompt.md
  --settings <run_dir>/claude_settings.json [--resume <session_id>] <cfg.extra_args...>
```
Never `--bare`. On stderr `unknown option '--max-turns'|'--append-system-prompt-file'` retry with `--append-system-prompt <text>`
and no `--max-turns` (own turn watchdog). Model problem (`not found|restricted|not available`) before `init` -> retry next model.

settings JSON: `{"hooks":{"PreToolUse":[{"matcher":"Write|Edit|MultiEdit|NotebookEdit|Bash","hooks":[{"type":"command","command":"\"<python>\" \"<KYORA_HOME>/bin/kyora_hook.py\"","timeout":300}]}]},"permissions":{"deny":["WebFetch","WebSearch"]}}`
env: `KYORA_JUDGE_URL=http://<server host>:<port>/api/judge` (`127.0.0.1` for a loopback bind, else the bound LAN host so the hook reaches the judge), `KYORA_RUN_ID`, `KYORA_HOOK_TOKEN`, `KYORA_HOME`.

Hook protocol (`kyora/runners/kyora_hook.py`, standalone stdlib): read JSON from stdin (fields `tool_name`, `tool_input`,
`tool_use_id`, `session_id`, `cwd`, `hook_event_name`); POST to `KYORA_JUDGE_URL` with the token and the `deadline_ms` budget; verdict allow -> exit 0;
deny -> print reason to stderr, exit 2 (blocks the tool and feeds the reason back to Claude); invalid stdin JSON -> stderr + exit 1;
judge unreachable/timeout/5xx -> log to `KYORA_HOME/logs/hook.log`, exit 0 (fail-open; engine emits judge.unavailable); 401/403 (bad token) -> exit 2 (fail-closed).

stream-json (one JSON per line): `{"type":"system","subtype":"init","session_id","model","tools","permissionMode","cwd"}`,
`{"type":"assistant","message":{"model","content":[{"type":"text|tool_use|thinking",...}],"usage"}}`,
`{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id","is_error","content"}]}}`,
`{"type":"result","subtype":"success|error_max_turns|error_during_execution","is_error","num_turns","duration_ms","duration_api_ms","total_cost_usd","session_id","result","permission_denials","modelUsage"}`.

## Pipeline state machine (runner.py / stages.py)

```
created->INTAKE->PLAN->[AWAITING_APPROVAL]->DESIGN->BUILD->TEST->GATE -ok-> FINALIZE->COMPLETE
GATE fail & iteration<max_fix_iterations -> REVIEW(Kimi) -fix-> FIX(engine) -> TEST -> GATE
GATE fail & iteration==max -> FINALIZE(below_threshold) -> FAILED_GATE (slide still committed)
no engine -> BLOCKED (resumable); no browser in browser_wait_s -> BLOCKED; exception -> FAILED; cancel -> CANCELLED (tree reset)
Kyora stopping (App.stop -> RunManager.stop) -> BLOCKED "interrupted: Kyora stopped ..." (no rollback, resumable)
```
Gate: total = counted checks (static non-skipped + browser + node); pass_rate = passed/total; ok iff pass_rate >= min_pass_rate
AND required checks pass (index.html exists, html well-formed, js syntax, manifest valid) AND every plan acceptance id present in results.

Stopping Kyora is not a cancel: `RunManager.stop()` sets its stopping flag before the runs' cancel events, and a
runner that sees both ends the run BLOCKED with its built work and iteration commits kept. A queued run is not
started once the flag is set. Runs that a hard kill (or the stop) leaves in an active or `created` state are
marked BLOCKED with the same reason on the next start.

Lineage branches: a run works on `lineage/main` (first generation), on the head branch of its parent slide
(modify), or on its own `lineage/<rid>`. A branch held by another unfinished run (prepared, not ended, no slide
committed yet) is never shared: the new run (or an import) gets its own `lineage/<rid>` instead. `run.options.tip`
records the commit the run last left on its branch (its base until its first commit). Resume restores exactly
that commit (forced checkout, untracked and ignored leftovers removed); when the branch no longer points there the
run BLOCKS with a reason instead of testing someone else's game. Rollback only resets a branch whose tip is still
the run's own commit.

Cancel: a running run gets its cancel event (it rolls back itself). A queued run that was never prepared left
nothing to roll back. A blocked or queued prepared run is rolled back under the per-project lock; while another
run of the project holds that lock (it is executing in the shared work tree), only the cancelled run's branch ref
moves and the work tree is never touched.

## Hub protocol (hub.py)

`Authorization: Bearer <hub.token>` on `/api/code-engine/*`. Job JSON: `{kind:"build|fix",run_id,prompt,system_prompt,max_turns,model,
fallback_models,effort,resume_session_id,judge_context}`; events relayed over SSE with `hub_job_id`; result = zip of workspace
(no `.git`) + `result.json`. Client unzips with `keep=('.git',)`, then runs local TEST/GATE.

## Tokkio overlay (tokkio-kyora/)

Vendored tree: `vendor/tokkio-5.0.0-ga/` (read it: `src/llm-rag/src/bot.py`, `src/llm-rag/configs/config.yaml`,
`src/llm-rag/src/serializer.py`, `customization-notebooks/09_create_frame_processor.ipynb`, `06_update_llm.ipynb`).
Overlay never modifies the vendored tree.
