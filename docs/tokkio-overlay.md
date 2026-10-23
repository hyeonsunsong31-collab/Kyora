# NVIDIA Tokkio and the Kyora overlay

Kyora's fourth system is NVIDIA Tokkio 5.0.0-ga (part of NVIDIA ACE), used as an **avatar presenter**.
This page explains what that means, what you need to run it, how to deploy it with the vendored
NVIDIA scripts and how to connect it to Kyora. The overlay files themselves are described in
[`tokkio-kyora/README.md`](../tokkio-kyora/README.md) (including the complete diff against NVIDIA's
`bot.py`). Kyora's side of the channel is `kyora/models/tokkio.py`; the event contract is in
[`architecture.md`](architecture.md).

## 1. What Tokkio is, and what it is not

Tokkio is a Kubernetes workflow of NVIDIA microservices that streams a talking 3D avatar to a web
page: Riva speech recognition (Parakeet), an LLM, text-to-speech (ElevenLabs in the default config),
Audio2Face-3D facial animation, an animation graph, a renderer (Unreal Engine in the vendored
`llm-rag` profiles), a video/WebRTC service, the Tokkio web UI and the **ACE controller**, a pipecat
pipeline (`vendor/tokkio-5.0.0-ga/src/llm-rag/`) that wires them together.

In Kyora it is the **presenter and custom-view output channel**:

| Kyora event | In Tokkio |
|---|---|
| `tokkio.narration {text}` | the avatar says `text` (verbatim, through the normal TTS/Audio2Face path) |
| `tokkio.view {title, preview_url, screenshot_b64?, stages, buttons}` | a custom view in the Tokkio UI: header, preview link, screenshot, stage table, buttons |
| `tokkio.clear` | the custom view is hidden |
| button `kyora:<action>` pressed in the Tokkio UI | `POST /api/actions {action, run_id, project_id}` to Kyora; the `{"url"}` it answers is added to the view as `<button label>: <url>` |

Links in the view are absolute and name the Kyora host the cluster reaches (`KyoraProcessor.events_url`):
previews and the test harness live on Kyora's separate preview listener (`server.preview_port`, by
default the main port + 1), so `/api/actions` answers `http://<host the overlay used>:<preview_port>/preview/...`
for **Open preview** and `http://<host>:<port>/api/projects/<id>/slides/<n>/download` for **Download ZIP**,
and the overlay replaces a `127.0.0.1` that Kyora reports for itself by the `events_url` host. The Tokkio
UI cannot open a tab on the viewer's device; open the link from a browser on the same network. Like the
rest of Kyora, previews and the ZIP need that browser's Kyora session (the `kyora_token` cookie set by
Kyora's link or QR code; a cookie covers both ports of the host) unless it runs on the Kyora PC itself.
Allow both ports in the PC's firewall.

Tokkio is **not** a UI generator and is not part of the code path. Game UI is produced by Kyora's
pipeline: the Nemotron role (Nemotron-3-Ultra or its fallback) writes the UI spec, tokens and copy; the
code engine (Claude Code by default, or the Anthropic API, a Kyora Hub or the Kyora agent engine)
implements real HTML5/Canvas/WebGL; tests and the measured quality gate (default 94 %,
`quality.min_pass_rate`) decide whether it is complete; the Kimi role (Kimi K3 or its fallback) reviews
failed gates and judges the writes and shell commands the local rules do not settle. When no Tokkio is deployed, the Presenter
panel shows **"Tokkio not deployed"** and every other feature works.

## 2. Prerequisites (NVIDIA's requirements, not Kyora's)

* An **NVIDIA-GPU Kubernetes cluster** created by NVIDIA's one-click scripts (they install the
  infrastructure, the NVIDIA Cloud Native Stack and the app). The vendored `llm-rag` profiles assign
  GPUs like this (`ucfVisibleGpus` in `tokkio-app-params.yaml`):

  | Profile (`vendor/tokkio-5.0.0-ga/llm-rag/`) | GPUs |
  |---|---|
  | `tokkio-1stream-with-ui`, `tokkio-1stream-no-ui` | 2 (GPU 0: Riva ASR, Audio2Face-3D, VMS; GPU 1: animation graph and renderer) |
  | `tokkio-3stream-*`, `tokkio-6stream-*` | 4 (GPU 0: Riva ASR, Audio2Face-3D, VMS; GPUs 1-3: renderers, animation graph on GPU 3) |

  Check NVIDIA's Tokkio documentation for the supported GPU models and instance types.
* An **NGC account** and API key: the containers, Helm charts and avatar assets are pulled from
  `nvcr.io` / NGC at deploy time under NVIDIA's licence terms (they are not in this repository).
* Secrets read by the one-click scripts from the environment (see each
  `scripts/one-click/<target>/config-template-examples/*/my-config.env`): `NGC_CLI_API_KEY`,
  `NVIDIA_API_KEY`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, plus the cloud credentials of the target
  (for AWS `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`). The scripts' playbooks check that the
  Kubernetes secrets are **non-empty**, so `OPENAI_API_KEY` must be set even though the overlay keeps
  `llm_processor: "NvidiaLLMService"` and never calls OpenAI. `NVIDIA_API_KEY` can be the same NVIDIA
  API Catalog key Kyora uses (it serves `nvidia/nemotron-3-ultra-550b-a55b` to the avatar's LLM).
  The overlay's `bot.kyora.py` builds that real-time LLM with reasoning **off**
  (`chat_template_kwargs.enable_thinking=false`, sent through the OpenAI SDK's `extra_body`, and
  `temperature 0.2`, NVIDIA's settings for reasoning off): Nemotron-3-Ultra reasons by default, and
  the Tokkio LLM service speaks only `delta.content`, so with reasoning on the avatar would stay silent
  for the whole reasoning trace after the filler phrase was cancelled.
* A Linux workstation for the one-click scripts (they use bash, OpenTofu/Terraform and Ansible;
  `install-pre-requisites.sh` installs what they need).

Tokkio cannot run on the phone or on a typical Windows PC. Kyora itself does not need Tokkio.

## 3. Deploy Tokkio with the vendored one-click scripts

The scripts are unmodified under `vendor/tokkio-5.0.0-ga/scripts/one-click/` for **aws**, **aws-lite**,
**azure**, **gcp** and **baremetal**. Each folder has `config-template.yml`, a worked example in
`config-template-examples/`, `envbuild.sh` and `install-pre-requisites.sh`.

```sh
cd vendor/tokkio-5.0.0-ga/scripts/one-click/aws            # or aws-lite, azure, gcp, baremetal
./install-pre-requisites.sh
cp config-template-examples/tokkio-3streams/config-template.yml my-config.yml   # then fill in every <replace-...>
cp config-template-examples/tokkio-3streams/my-config.env my-config.env         # then fill in the secrets
. ./my-config.env                                           # exports NGC_CLI_API_KEY, NVIDIA_API_KEY, ...
./envbuild.sh install --component all --config-file ./my-config.yml
./envbuild.sh info --config-file ./my-config.yml            # prints the endpoints of the deployment
```

(`envbuild.sh` usage: `install|uninstall -c all|infra|platform|app [-f config] [-d dry-run]`,
`info`, `force-destroy`.) The deployment exposes the Tokkio UI on NodePort **30111** and the ACE
Configurator on NodePort **30180** (cloud targets put them behind the domains configured in
`my-config.yml`). Open the UI and talk to the stock avatar first: that proves the cluster works before
Kyora is involved. Remove everything with `./envbuild.sh uninstall --component all --config-file ./my-config.yml`.

Keep `my-config.env` out of version control; Kyora's repository never contains these secrets.

## 4. Make Kyora reachable from the cluster

The overlay processor connects **from the ace-controller pod to Kyora** (Server-Sent Events), so Kyora
must listen on an address the cluster can reach:

1. On the PC: `kyora hub` (Windows: `launchers\kyora.ps1 hub`). It binds `0.0.0.0`, generates a hub
   token and prints the LAN URL. Keep the token private; it is the bearer credential for
   `/api/events` and `/api/actions`.
2. In Kyora Settings set **`hub.expose_ui` = true**. Without it, non-loopback clients get 403 on
   `/api/events` even with the token.
3. Make port 8765 reachable from the cluster: same LAN, a VPN, or a tunnel you operate. A cloud
   cluster usually cannot reach a home PC without one.
4. In Settings, set `tokkio.ui_url` = `http://<node-ip>:30111` and `tokkio.configurator_url` =
   `http://<node-ip>:30180` (or the HTTPS domains of a cloud deployment). `kyora selfcheck` and
   `GET /api/tokkio/health` then report whether both answer.

## 5. Apply the overlay

```sh
python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180            # dry run
python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180 --apply \
    --events-url http://<kyora-lan-ip>:8765 --token <hub token> [--kubectl-fallback] [--namespace app]
```

The dry run prints the file map (`configs/config.kyora.yaml -> configs/config.yaml`,
`src/kyora_processor.py -> src/kyora_processor.py`, `src/bot.kyora.py -> src/bot.py`) and contacts
nothing. `--apply` discovers the Configurator's upload route from `/openapi.json` and uploads to the
`app-storage-volume` of `ace-controller-ace-controller-deployment` (hot reload), or with
`--kubectl-fallback` copies the files into `/app` of container `ace-controller-container` and compares
sha256 sums inside the pod. The script reports separately what it verified and what it could not
(details: [`tokkio-kyora/README.md`](../tokkio-kyora/README.md)). Then confirm in the pod log:

```sh
kubectl -n app logs deploy/ace-controller-ace-controller-deployment -c ace-controller-container \
    | grep -E 'Kyora overlay|KyoraProcessor'
# Kyora overlay: events_url=http://192.168.1.20:8765 narrate=True show_views=True
# KyoraProcessor: connected to Kyora events (Last-Event-ID=...)
```

Instead of `--events-url/--token` you may provide `KYORA_EVENTS_URL` / `KYORA_HUB_TOKEN` as container
environment or in `/app/.env` (template: `tokkio-kyora/.env.kyora.example`); values in `config.yaml`
win. Files uploaded to the volume may be reset when the app is reinstalled; re-run the script after a
redeploy.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Presenter panel: "Tokkio not deployed" | `tokkio.ui_url` is empty or unreachable from the device running Kyora, or it answers with an HTTP error (the panel names the status, e.g. `HTTP 502` from an ingress whose Tokkio pods are not running, or `HTTP 404` from another service). Only a 2xx/3xx answer shows the Tokkio UI. |
| Pod log: `KyoraProcessor: no events_url configured` | Neither `KyoraProcessor.events_url` nor `KYORA_EVENTS_URL` is set. |
| Pod log: `HTTP 403 ... check hub.token and settings hub.expose_ui=true` | Wrong token, or `hub.expose_ui` is false. |
| Pod log: `Kyora unreachable ... retry in Ns` | The pod cannot reach Kyora's address (firewall, NAT, wrong IP). The avatar keeps working without narration. |
| `upload_overlay.py`: "no PUT/POST operation looks like a per-file upload" | The Configurator's API differs from what the script can identify; use `--kubectl-fallback` or upload the three files with the ACE Configurator VS Code extension. |
| Narration logged but not spoken | No user is present in front of the avatar (user-presence gating), or TTS credentials are wrong (`ELEVENLABS_API_KEY`). |

## 7. Licences

The vendored tree is NVIDIA's (Apache-2.0 `LICENSE` files; several source files carry an NVIDIA
proprietary header, recorded in [`vendor/VENDOR.md`](../vendor/VENDOR.md)). The containers and assets
come from NGC under NVIDIA's AI Product License / AI Enterprise terms. Kyora's overlay code is MIT;
`bot.kyora.py` and `config.kyora.yaml` are modified NVIDIA files with Kyora's lines marked.
