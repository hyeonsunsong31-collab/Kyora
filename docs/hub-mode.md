# Hub mode

Hub mode lets one Kyora (a PC, or an Android phone that can run Claude Code) lend its
**code engine** to other Kyora instances on the same network. The usual case is an
iPhone or iPad running Kyora inside iSH.

## Why it exists

iSH emulates 32-bit x86 Alpine Linux. It runs Python, so Kyora's server, UI, pipeline,
the Nemotron and Kimi calls, the static checks, and the browser test harness all work
there. It cannot run Node.js, so it cannot run the Claude Code CLI, which means it
cannot run the BUILD and FIX stages itself. Without a hub, a run on iSH stops at
**BLOCKED** with the reason. Kyora never fakes a build.

With a hub, the phone runs every stage locally except the code engine. Only BUILD and FIX
go to the hub:

```
phone (iSH)                                   hub (PC / Android)
intake -> plan -> design ->  BUILD/FIX  ---->  Claude Code CLI (--effort max), judged by the hub's Kimi
                           <- events (SSE) --  engine.* / judge.* / stage.progress
                           <- result zip ----  workspace after the engine finished
test -> gate -> review -> finalize (slides, deck) on the phone
```

The hub runs the judge for every tool call with **its own** NVIDIA keys and Kimi
settings, because the PreToolUse hook can only reach the judge on the same machine.

## Linking a phone to a hub

1. On the PC (or Android), run `kyora hub` (Windows: `kyora.ps1 hub`). This binds
   `0.0.0.0`, creates `hub.token` if none exists, and prints:
   - the hub URL for each LAN address (`http://192.168.1.20:8765`) and the token
   - a pairing link and a QR code for it (ANSI half-blocks in the terminal; the UI's
     *Hub link* card shows the same QR as SVG from `GET /api/qr.svg?text=...`).
2. Start Kyora on the phone (`kyora-ish.sh`) and keep iSH running.
3. Scan the QR code with the camera and open the link. It looks like:

   ```
   http://127.0.0.1:8765/#hub=http%3A%2F%2F192.168.1.20%3A8765&token=<token>
   ```

   The link opens the **phone's own** Kyora UI (`127.0.0.1:8765`). The UI reads the
   `#hub=` and `token=` fragment and saves them with `PUT /api/settings`
   (`keys.hub.url`, `keys.hub.token`). The fragment is never sent to any server. The
   link is built by `kyora.hub.link_payload(base_url, token, ui_url=None)`. Pass `ui_url`
   if the phone's Kyora uses a port other than 8765.
4. You can also enter the hub URL and the token in Settings by hand.

Once `hub.url` is set, engine selection (`select_engine`) tries the hub first. The
`hub` engine counts as available only when the hub answers `GET /api/code-engine/status`
and reports a local engine that is available. If the hub has no engine, the
reason says so (for example *hub has no code engine: install Claude Code and run claude
login on the hub*), and Kyora falls back to a local engine or to BLOCKED.

## Protocol

Every endpoint needs `Authorization: Bearer <hub.token>`. All endpoints exist only on
a Kyora started in hub mode (`kyora hub` or `kyora serve --hub`).

| Method and path | Purpose |
|---|---|
| `GET /api/code-engine/status` | `{ok, version, hub: true, engine: {available, backend, reason, details{version, logged_in, ...}}, node, git, platform, queue: {running, pending}}` |
| `POST /api/code-engine/jobs` | multipart `job` (JSON) + `workspace` (zip, max 256 MB). Returns `201 {job_id, state, run_id, events_since}` |
| `GET /api/code-engine/jobs/{id}` | `{job_id, state, result, error, kind, run_id, queue_position, ...}`. `state` is `queued`, `running`, `done`, `failed` or `cancelled` |
| `GET /api/code-engine/jobs/{id}/events?since=N` | SSE relay of `engine.*`, `judge.*` and `stage.progress` for the job's run, with `hub_job_id` in `data`. Also honours `Last-Event-ID`. The stream closes after a finished job's events have been replayed |
| `GET /api/code-engine/jobs/{id}/result` | zip of the job workspace (without `.git` and `node_modules`) plus `.kyora-hub/result.json`. Returns `409` while the job is queued or running |
| `POST /api/code-engine/jobs/{id}/cancel` | cancels a queued job at once, or signals a running engine to stop. Returns `{job_id, state}` |

The job JSON has these fields: `{kind: "build"|"fix", run_id, project_id, prompt,
system_prompt, max_turns, model, fallback_models, effort, resume_session_id,
judge_context}`. The client's `EngineJob.env` is **never** forwarded, so a LAN client
cannot inject environment variables into the hub's processes. On the hub, `workspace`
and `run_dir` in `judge_context` are replaced with hub-side paths. When a FIX job
resumes a session (`resume_session_id`) that came from an earlier hub job, the new
upload replaces that job's workspace in place. Claude Code then resumes in the same
working directory, which `--resume` needs. If the earlier workspace is gone, the hub
starts a new session and says so in a `stage.progress` message.

The client side (`HubClientEngine`) works like this:

1. It zips the workspace, leaving out `.git` and `node_modules` at any depth and
   `cache/` at the root.
2. It POSTs the job, then follows the SSE relay. On a drop it reconnects with
   `since=<last id>`, using a backoff of 1, 2, 4 ... 15 s. As a backstop it polls the job
   every few seconds.
3. It forwards every relayed event to the local pipeline. `hub_job_id` is removed, and
   `engine.session.backend` becomes `hub:<backend>`.
4. A local cancel becomes `POST .../cancel`. The wall timeout is `KYORA_HUB_TIMEOUT_S`
   in the job env (default 7200 s).
5. When the job is `done`, it downloads the result zip and replaces the local workspace
   with it. It keeps `.git` and the paths it never uploaded (`node_modules/` and
   `cache/` at the root). Then it rebuilds the `EngineResult` with backend
   `hub:<remote backend>`, for example `hub:claude_code`.
6. The local TEST, GATE and REVIEW stages then run on the phone as usual.

## Security

- **Token.** `hub.token` is a random 32-character token (`secrets.token_urlsafe(24)`).
  It is compared with `hmac.compare_digest`. 20 failed attempts from one IP within
  60 s lock that IP out with `429` for 60 s. Treat the token like a password: anyone who
  has it can run the hub's code engine on your Claude subscription.
- **LAN only, no TLS.** The hub speaks plain HTTP, so the token and the workspace cross
  the network unencrypted. Use it only on a network you trust (home Wi-Fi, a phone
  hotspot), or tunnel it through a VPN such as WireGuard or Tailscale. Do not expose the
  hub port to the internet.
- **Host names.** The hub answers only requests addressed to it: by IP address (the
  pairing link uses the LAN IP), `localhost`, or its hostname, `<hostname>.local` and
  FQDN. Any other name (a DNS rebinding attack, but also a VPN or router name such as
  `mypc.lan`) gets `403 host not allowed`; add such names to `server.allowed_hosts` in
  the hub's `settings.json`.
- **Ports and firewall.** `/api/code-engine/*` is on the main port (8765). With
  `hub.expose_ui`, phones that open the hub's UI also load game previews and the test
  harness from the preview port (8766 by default, `server.preview_port`); allow both
  ports for private networks in the PC firewall. A second Kyora started on a port that
  is already in use stops with an error instead of sharing it.
- **Uploads.** Every uploaded zip is validated before anything is written. Absolute
  paths, drive letters, `..`, `:`, symlink entries and anything that resolves outside the
  job directory are rejected, and entries inside `.git` are never written.
- **No loops.** A hub runs jobs only on its own local engine (Claude Code CLI, else the
  optional Messages-API engine). It never forwards a job to another hub.
- **The judge still runs.** Every Write, Edit, MultiEdit, NotebookEdit and Bash call of
  a hub job is judged on the hub, exactly as in a local run.

## Limits

- One job runs at a time. Other jobs wait in a queue, and `queue_position` reports
  their place.
- Uploads are capped at 256 MB. Large assets slow down iSH, because zipping runs under
  emulation.
- Jobs and workspaces are kept under `KYORA_HOME/hub/jobs/<id>/` for 24 h after they
  finish, then removed. Jobs that were running when the hub stopped are marked `failed`
  at the next start.
- The hub must stay reachable for the whole run. If it disappears, the client keeps
  reconnecting until the wall timeout. After that the run fails with the reason, and no
  partial result is unpacked.
- The judge verdict counts for hub jobs come from the hub (`EngineResult.raw.hub.judge`).
  The phone's own judge sees no tool calls for these jobs.
