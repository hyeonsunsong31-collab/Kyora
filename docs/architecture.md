# Kyora architecture and module contracts

This file is the contract every module is written against. Foundation modules
(`kyora/config.py`, `kyora/server.py`, `kyora/app.py`, `kyora/pipeline/events.py`,
`kyora/models/base.py`, `kyora/util/*`) already exist; read them before coding.
**Standard library only** in `kyora/` (the optional Anthropic engine may import the
`anthropic` package behind an import guard). Python 3.10+.

## What Kyora is

An orchestration application. External systems in fixed roles over their own APIs:

| Role | System | Access |
|---|---|---|
| Text/design director: GDD prose, narrative, dialogue, UI/UX spec + design tokens + copy, presenter narration | NVIDIA Nemotron-3-Ultra (role `nemotron`) | `https://integrate.api.nvidia.com/v1` (or `nvidia.nemotron.base_url`), model `nvidia/nemotron-3-ultra-550b-a55b`, fallbacks `nvidia.nemotron.fallback_models`, OpenAI-compatible chat completions |
| Planner + judge: intake (vision), plan + task graph + acceptance tests, judgement of the code engine's Write/Edit/Bash calls that local rules do not settle, final review | Moonshot Kimi-K3 (role `kimi`) | same endpoint (or `nvidia.kimi.base_url`), model `moonshotai/kimi-k3`, fallbacks `nvidia.kimi.fallback_models` (on NVIDIA's free endpoint Kimi K3 answers 410 since 2026-10-15, so the fallbacks answer there) |
| Code engine: implement, debug, test, build | Claude Code CLI at `--effort max` (subscription login) ; optional Messages-API engine (same Claude models, API key) ; Kyora agent engine (non-Claude model, OpenAI-compatible endpoint) ; remote Kyora Hub | subprocess / official `anthropic` SDK / `/chat/completions` with Kyora's tool loop / HTTP |
| Presenter + custom-view output channel | NVIDIA Tokkio 5.0.0-ga (vendored, unmodified) + Kyora overlay processor | deployed by the user on an NVIDIA-GPU Kubernetes cluster |

Honesty rules: never fabricate model output, metrics or verdicts. A missing engine =
BLOCKED. Measured numbers only. Every substitution (fallback model, shared key, engine failover,
automatic resume) is published as an event. "Kimi K3" is displayed in the UI (licence).

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
                                            engine/{stream.jsonl,stderr.log,judge.jsonl} (Claude Code),
                                            engine/{agent_turns.jsonl,api_turns.jsonl,editor-backups/}
                                            (Kyora agent engine / Messages-API engine),
                                            iter-<n>/{static,node,browser,gate}.json, iter-<n>/screenshots/*.png,
                                            review-<n>.json
KYORA_HOME/projects/<pid>/cache/slides/v<n>/ materialized checkout for /preview
KYORA_HOME/projects/<pid>/attachments/
KYORA_HOME/hub/jobs/<id>/
KYORA_HOME/bench/<timestamp>/
```

## Foundation APIs (exist)

- `kyora.config.Config`: `.nvidia` (base_url, share_keys, `alt_keys(role)`, nemotron/kimi {model, api_key, base_url
  (resolved: the role's own or `nvidia.base_url`), fallback_models}), `.anthropic_api_key`, `.agent_engine`
  (`AgentEngineCfg`: the `agent_engine` settings plus the resolved endpoint base_url, api_key, key_label, alt_keys),
  `.engines` (order, failover, auto_resume_on_limit),
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
  `EngineResult`, `Availability`, `CodeEngine` (abstract `availability()`, `run(job, sink, cancel)`), `select_engine(app)`
  (first available engine in `settings.engines.order`), `engine_order(app)`, `engine_candidates(app, exclude)`,
  `classify_failure(result)` -> `auth|quota|unavailable|model|None`, `reset_time(result)`, `parse_reset_text(text)`,
  `ENGINE_APP_ATTRS`, `FAILURE_KINDS` (see [Code engines](#code-engines-registry-order-failover-and-automatic-resume)).
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
- `kyora.app.App`: attributes `config, home, bus, platform, store, nim_nemotron, nim_kimi, nemotron, kimi, judge,
  claude_engine, anthropic_engine, agent_engine, hub_client, hub_worker, tokkio, run_manager, selfcheck, bench,
  browser_results, server_url, preview_url, preview_port, hub_mode, missing`; `engine()`; `apply_model_settings()`
  (called after `PUT /api/settings` changed `keys.nvidia.*`: endpoints, keys, models and fallbacks apply without a
  restart); `status()` (includes `preview_url`, `preview_port`, `engines` with `openai_agent`, `selected_engine`,
  `runs.auto_resume`);
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
NIMClient(base_url, api_key, *, user_agent, key_label, alt_keys)        kyora/models/nim_client.py
Nemotron(client, model, bus, nemotron_cfg, fallback_models=())          kyora/models/nemotron.py
Kimi(client, model, kimi_cfg, bus, fallback_models=())                  kyora/models/kimi.py
JudgeService(kimi_or_None, bus, config, home)                           kyora/pipeline/judge.py
ClaudeCodeEngine(claude_code_cfg, home, app)  (+ install_hook())        kyora/models/claude_code.py
AnthropicAPIEngine(api_key_or_callable, claude_code_cfg, judge, home)   kyora/models/anthropic_api.py
OpenAIAgentEngine(agent_cfg_or_callable, judge, home, *, client_factory=None)   kyora/models/openai_agent.py
ToolLoopEngine(judge, home)  (base of the two above: judged bash/editor tools)  kyora/models/agent_tools.py
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
options{engine_pref: auto|hub|claude_code|anthropic_api|openai_agent, import_zip_attachment}), `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`, `POST /api/runs/{id}/approve`,
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
- `run.created|run.state{state,iteration}|run.blocked{reason,stage,auto_resume_at?}|run.completed{slide}|run.failed{error}|run.cancelled|run.awaiting_approval`
  (`auto_resume_at`: ISO time of the usage-limit reset when the run will resume by itself)
- `run.auto_resume{at,in_s,reset_at,reason}`: a run blocked by a usage limit resumes by itself at `at` (the reset + 60 s, ISO)
- `stage.started{stage}`, `stage.progress{stage,pct,message}` (BUILD/FIX start also carries `backend`, `effort`; `effort` is `null` for the Kyora agent engine), `stage.finished{stage,ok,ms}`
- `model.request{role,model}` (Kimi adds `effort_sent`, and `effort_best_effort`/`effort_note` in the `extra_body` mode, also on metrics/done/error; when a fallback model answers for the role, request/metrics/done/error also carry `requested_model` and `fallback: true`; `model` is always the model that was called), `model.delta{role,model,content?,reasoning?}` (not persisted), `model.metrics{role,model,ttft_ms,total_ms,prompt_tokens,completion_tokens,tps,tokens_source,finish_reason}`, `model.done{role,model,finish_reason,tool_calls,content_chars,reasoning_chars}`, `model.error{role,model,error,status,hint}` (`hint`: what the HTTP status means and what to do)
- `model.fallback`: `{kind:"model",role,requested,from_model,to_model,status,reason,temporary,cooldown_s?,purpose?}` (the next model of the role's chain answers: `temporary:false` for a gone model, 404/410; `true` for one that stayed overloaded, cooled down for `cooldown_s`, and for a Nemotron model that gave no usable design/UI-spec answer twice, `status:null`) or `{kind:"key",role,model,from_key,to_key,status,reason,purpose?}` (a refused key, 401/403, replaced by the other role's key on the same endpoint; labels only)
- `engine.session{session_id,model,tools,permission_mode,cwd,backend}` (Kyora agent engine: also `requested_model`, `effort` = `thinking|default`, `endpoint` (host only), `key_label`, and `resume` when a resume was asked for), `engine.model_resolved{requested,active,fallback_used,reason?}`, `engine.text{text}`, `engine.thinking{text}`, `engine.tool_use{id,name,summary}` (agent engine: also `tool`), `engine.tool_result{tool_use_id,is_error,excerpt}`, `engine.result{subtype,is_error,num_turns,duration_ms,cost_usd,result}`, `engine.error{error,failure_kind?}`, `engine.compat_retry{reason,argv_change}`, `engine.stderr{line}` (agent engine: `kind` = `key|model|context` with `from_key/to_key/status` or `from_model/to_model/status/reason/temporary`)
- `engine.failover{from,to,reason,failure_kind,kind,iteration,pinned,tree_changed}`: the same BUILD/FIX (`kind`) continues on engine `to` after `from` stopped for an account or service reason; `pinned` = the run had picked `from`; `tree_changed` = `from` left partial work, which `to` is told to continue
- `judge.request{tool_name,summary}`, `judge.verdict{decision,reason,source,latency_ms,tool_name}`, `judge.unavailable{reason,tool_name?,tool_use_id?,ran_unjudged?}` (`ran_unjudged: true` = an engine-detected unjudged call, counted allowed + unavailable, no `judge.verdict` follows)
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
Keys the code keeps in `run.extra` are written as top-level keys of `run.json` (`Run.to_dict`). The code-engine ones:

| Key | Content |
|---|---|
| `engine_runs` | one entry per engine attempt of every BUILD/FIX: `{kind, iteration, engine, ok, backend, active_model, fallback_used, num_turns, duration_ms, error, tree_changed, failure_kind, session_id}`; slides, the deck and Bench certification read the models from here |
| `engine_failures` | every failed attempt: `{kind, iteration, backend, error, tree_changed, failure_kind, failover_to?}` |
| `engine_failovers` | every failover: the `engine.failover` payload plus `at` (ISO) |
| `auto_resume_at` | epoch seconds of the usage-limit reset after which the run manager resumes this blocked run (removed when the run is resumed, blocks for another reason, is interrupted by Kyora stopping or reaches 24 automatic resumes; a cancel stops its timer) |
| `auto_resumes` | ISO times of the automatic resumes done so far (at most 24) |
| `partial_work_kept` | `{kind, iteration, at}`: the work an engine stopped by a usage limit left was committed before the run blocked |
| `blocked_stage` | the stage at which the run blocked |

`run.engine` (the last attempt) also carries `backend_name`, `effort` (`null` for the Kyora agent engine),
`tree_changed`, `failure_kind` and, for a continued Claude Code session, `resumed_session_id`.
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
(`deadline_ms` = the hook's wait budget, a little under `KYORA_HOOK_TIMEOUT_S`; a Kimi verdict slower than it is journaled `late`, never governs, and the route answers `source:"timeout"` allow. `judge_model` is the model that answered the call: Kimi K3 or the Kimi role's fallback.)
Rule verdicts carry `rule`: `kyora_secrets` (a path or command that reaches Kyora's config directory, key files or Hub token: deny), `read_only_tool`, read-only command chains whose every path argument lies inside the workspace (`env`/`printenv`, variable assignments and paths elsewhere defer), and the escape/destructive-command denials; the full list is in [`claude-code-engine.md`](claude-code-engine.md#6-judge-judgeservice).

Review (`review-<n>.json`, `review.decision{iteration,decision,reasons,source,model,images_sent,screenshots}`): `source:"model"` with the answering `model` and how many of the `screenshots` reached it (`images_sent`: 0 for a model that takes no images, which gets a note and a system prompt without them), or `source:"rules"` with `model:null` when no review model could answer: a `fix` decision whose instructions are the gate's failing checks (required first). `run.extra.role_models` records the model that answered intake, plan, design, ui_spec and each review.

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
model call failed after keys, retries and fallbacks -> BLOCKED (resumable)
BUILD/FIX engine account failure -> same BUILD/FIX on the next engine (engine.failover) ... none left:
    usage limit with a known reset -> BLOCKED + auto_resume_at -> resumed at reset + 60 s (run.auto_resume)
    otherwise -> BLOCKED (resumable), or TEST/GATE measure the files when the engines changed the tree
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

## Code engines: registry, order, failover and automatic resume

**Registry.** `settings.engines.order` names engines; `models.base.ENGINE_APP_ATTRS` maps each name to its
`App` attribute:

| Name | `App` attribute | Engine | Available when |
|---|---|---|---|
| `hub` | `hub_client` | `HubClientEngine` (`kyora/hub.py`) | `hub.url` is set and the hub reports an available local engine |
| `claude_code` | `claude_engine` | `ClaudeCodeEngine` | the CLI is found and logged in |
| `anthropic_api` | `anthropic_engine` | `AnthropicAPIEngine` | an Anthropic API key is set and the `anthropic` package imports |
| `openai_agent` | `agent_engine` | `OpenAIAgentEngine` ([`agent-engine.md`](agent-engine.md)) | `agent_engine.enabled`, an endpoint and a key for it (no network check) |

**Selection.** `select_engine(app)` returns the first available engine in `engines.order` (default
`hub, claude_code, anthropic_api, openai_agent`); `stages.select_engine(ctx)` first honours the run's
`options.engine_pref` when that engine is available. No engine -> `StageBlocked(NO_ENGINE_REASON)`.

**Failure kinds.** `classify_failure(result)` decides whether another engine might do better: the
engine's own `raw["failure_kind"]` wins (Claude Code and the agent engine set it), otherwise Claude Code's
`api_error` fields and `rate_limit_event` (`status: rejected` -> `quota`), then the error text and the
HTTP statuses in it: `auth` (not logged in, invalid or revoked key, 401, 403 without a model word),
`quota` (usage/session/weekly limit, credits, subscription, plan, rate limit, 402, 429), `model` (model
not found, retired, end of life, 410, 404 naming a model), `unavailable` (cannot reach, overload,
connection, DNS/TLS, 5xx, lost contact, CLI not found). Task failures (max turns, job/idle timeouts,
refusals, malformed output, judge denials, context limits) and cancels are `None`: no failover.

**Failover** (`stages.run_engine`, `engines.failover`): after an attempt with a failure kind, the same
job (same kind and iteration) runs on the first engine of `engine_candidates(app, exclude=<tried>)`. A
run's pinned engine (`engine_pref`) fails over too (`pinned: true`). Each switch appends to
`engine_failovers`, sets `failover_to` on the failure, publishes `engine.failover` and a
`stage.progress` line. When the failed attempt changed the work tree, the next prompt starts with
`PARTIAL_WORK_PREFIX` ("A previous code engine (<name>) stopped part-way (<reason>). The work tree already
contains its partial work: inspect it and complete the task below without starting over."). TEST and
GATE measure the result as always.

**After the last attempt failed** (`_after_failed_attempts`): if the last failure is an account failure,
one of the call's failures was `quota`, `engines.auto_resume_on_limit` is on and `reset_time()` finds a
reset for a quota failure (one already in the past counts as now, so the run resumes after the grace) (`raw["resets_at"]`, Claude Code's `rate_limits[].resetsAt`, or the
error text: "resets 1:20pm (UTC)", "resets Oct 20, 10am (Europe/Berlin)", "resets at <ISO>",
"try again in 3h 20m", "Retry-After: 120"), the partial work is committed when the tree changed
(`partial_work_kept`), `auto_resume_at` is stored and the run blocks with `StageBlocked(..., resume_at=)`;
the reason names the reset in UTC. A failure that passes by itself (`stages.passes_by_itself`: failure kind
`unavailable`, or `quota` whose text is a rate limit - HTTP 429, "Too Many Requests" - without credit or
billing wording) and has no known reset gets `retry_at` = its time + `TRANSIENT_RETRY_S` (120, 300, 900,
1800 s by the run's automatic resumes so far) in its `engine_runs` entry; the block resumes at the soonest
reset or `retry_at`, and `_still_failing` treats that engine as usable again once `retry_at` passed.
Otherwise the run blocks (no `index.html`, or the engines changed no file relative to the iteration base)
or TEST/GATE measure what the engines produced.

**Automatic resume** (`RunManager`): a blocked run with `auto_resume_at` is resumed at
`auto_resume_at + AUTO_RESUME_GRACE_S` (60 s). The timer wakes at least every `AUTO_RESUME_STEP_S`
(600 s) to re-check the wall clock (sleep/suspend), is re-armed for every blocked run with
`auto_resume_at` when Kyora starts (one whose reset has passed resumes at once), publishes
`run.auto_resume` when armed, appends to `auto_resumes` when it fires, and is dropped by a manual
resume, a cancel or a block for another reason. After `AUTO_RESUME_MAX` (24) automatic resumes the run
waits for the user ("automatic resume stopped after 24 automatic resumes: press Resume"). A run that is
active when Kyora stops loses `auto_resume_at` and waits for Resume. `RunManager.status()["auto_resume"]`
maps run ids to their due time.

**Claude Code session continuation.** When the engine for a BUILD/FIX is Claude Code and its last attempt
of the same kind and iteration failed with `quota` and has a `session_id`, the attempt runs with
`--resume <session_id>` and the prompt prefixed with `CONTINUE_PREFIX` ("Continue the task from where you
stopped. "). When the CLI says the session is gone (`SESSION_GONE_RE`), a fresh session
starts with `PARTIAL_WORK_PREFIX` (when the resumed attempt failed only because of that, changed nothing
and is no account failure). The Kyora agent engine has no sessions; every call starts fresh.

## Hub protocol (hub.py)

A hub runs each job on its first available local engine: Claude Code CLI, then the Messages-API engine,
then the Kyora agent engine (`select_local_engine`; never another hub). It does not fail over between its
own engines within a job; the client classifies the hub's error like any other engine result and its own
`engines.order` decides what runs next.

`Authorization: Bearer <hub.token>` on `/api/code-engine/*`. Job JSON: `{kind:"build|fix",run_id,prompt,system_prompt,max_turns,model,
fallback_models,effort,resume_session_id,judge_context}`; events relayed over SSE with `hub_job_id`; result = zip of workspace
(no `.git`) + `result.json`. Client unzips with `keep=('.git',)`, then runs local TEST/GATE.

## Tokkio overlay (tokkio-kyora/)

Vendored tree: `vendor/tokkio-5.0.0-ga/` (read it: `src/llm-rag/src/bot.py`, `src/llm-rag/configs/config.yaml`,
`src/llm-rag/src/serializer.py`, `customization-notebooks/09_create_frame_processor.ipynb`, `06_update_llm.ipynb`).
Overlay never modifies the vendored tree.
