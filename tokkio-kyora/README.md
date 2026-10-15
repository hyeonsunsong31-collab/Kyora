# tokkio-kyora: the Kyora overlay for NVIDIA Tokkio 5.0.0-ga

This folder connects a Tokkio deployment that **you** run on an NVIDIA-GPU Kubernetes cluster to
Kyora. It does not contain Tokkio: the unmodified NVIDIA tree is vendored under
[`../vendor/tokkio-5.0.0-ga/`](../vendor/tokkio-5.0.0-ga/) (sha256-verified, see
[`../vendor/VENDOR.md`](../vendor/VENDOR.md)) and nothing in it is edited. The full guide (what
Tokkio is, prerequisites, deployment, troubleshooting) is [`../docs/tokkio-overlay.md`](../docs/tokkio-overlay.md).

## What Tokkio does for Kyora (and what it does not)

Tokkio is a **presenter**: a streamed 3D avatar (speech recognition, an LLM, text-to-speech,
Audio2Face-3D animation, a renderer and a web UI) orchestrated by the ACE controller, a pipecat
pipeline in `src/llm-rag`. With this overlay the avatar

* **speaks** Kyora's `tokkio.narration` events verbatim (short presenter lines that Kyora publishes
  during a run; Kyora's narration role is Nemotron-3-Ultra; the avatar's LLM does not rewrite them),
* **shows** Kyora's `tokkio.view` events as a Tokkio *custom view* (title, preview link, screenshot,
  stage table, action buttons) and hides it on `tokkio.clear`,
* sends presses of `kyora:*` buttons back to Kyora (`POST /api/actions`),
* answers spoken questions with its own LLM, which the overlay config points at
  `nvidia/nemotron-3-ultra-550b-a55b` with a "Kyora presenter" persona.

Tokkio does **not** generate game UI, code or tests. Game UI is produced by Kyora's pipeline
(Nemotron writes the UI spec, Claude Code implements it, Kimi K3 judges it). Without a deployed
Tokkio, Kyora's Presenter panel says "Tokkio not deployed"; nothing is simulated.

## Files

| File | Becomes (inside the ace-controller container, `/app`) | What it is |
|---|---|---|
| `configs/config.kyora.yaml` | `configs/config.yaml` | The vendored `src/llm-rag/configs/config.yaml` with every section kept, plus: `NvidiaLLMService.model: "nvidia/nemotron-3-ultra-550b-a55b"`, a Kyora presenter persona/greeting in `OpenAILLMContext` / `UserPresenceProcesssor`, and a new `KyoraProcessor` section (`events_url`, `token`, `narrate`, `show_views`, `reconnect_s`). `Pipeline.llm_processor` stays `"NvidiaLLMService"`. The vendored pydantic `Config` ignores the extra section. |
| `src/kyora_processor.py` | `src/kyora_processor.py` | `KyoraProcessor`, a pass-through pipecat `FrameProcessor` (pipecat-ai 0.0.68 / nvidia-pipecat 0.2.0 APIs). On `StartFrame` it opens a reconnecting SSE subscription to `<events_url>/api/events?types=tokkio.narration,tokkio.view,tokkio.clear` (`Authorization: Bearer <token>`, resumes with `Last-Event-ID`, skips history on the first connect). Narration is queued at the pipeline head with `PipelineTask.queue_frame(TTSSpeakFrame(text))` so it passes user-presence gating, TTS, transcript sync and Audio2Face; views are pushed downstream as `StartCustomViewFrame` (`HeaderBlock`, `TextBlock`, `ImageBlock`, `TableBlock`, `ButtonListBlock`) / `StopCustomViewFrame`. `UIButtonPressFrame`s with `kyora:` ids are POSTed to `<events_url>/api/actions`. When Kyora is unreachable it logs and retries with backoff (3 s doubling to 60 s); the Tokkio pipeline keeps running. Uses `httpx`, which is already installed in the container (locked in `uv.lock`, imported by nvidia-pipecat). |
| `src/bot.kyora.py` | `src/bot.py` | The vendored `src/llm-rag/src/bot.py` plus lines marked `# KYORA` only (diff below): reads the `KyoraProcessor` section (config first, env `KYORA_EVENTS_URL` / `KYORA_HUB_TOKEN` as fallback), inserts `KyoraProcessor` after `rag_multimodal_response_processor` and before `a2f`, and calls `attach_task(task)`. |
| `.env.kyora.example` | `.env` (optional) | Env fallbacks `KYORA_EVENTS_URL`, `KYORA_HUB_TOKEN`. The vendored `entrypoint.sh` exports `/app/.env` and `bot.py` calls `load_dotenv()`. The upload script never writes it. |
| `deploy/upload_overlay.py` | - | Uploads the three files above (standard library only; dry run by default). |
| `deploy/helm-values.notes.md` | - | Which vendored Helm/UCS values matter for Kyora (GPUs, ports, secrets, the hot-reload volume) and why no values change is needed. |

`tests/test_tokkio_overlay.py` checks all of this offline: the config keeps every vendored section,
`bot.kyora.py` differs from the vendored file only by added `# KYORA` lines, the dry run makes no
network connection, `scripts/vendor_tokkio.py --verify` passes, and the processor's SSE parser and
payload normalisers behave (with stub pipecat modules).

## Apply the overlay to a running Tokkio

Prerequisites: Tokkio 5.0.0-ga deployed with one of the vendored one-click scripts (see
[`../docs/tokkio-overlay.md`](../docs/tokkio-overlay.md)), the ACE Configurator reachable at
`http://<node-ip>:30180`, and Kyora reachable **from the cluster** (Kyora must listen on the LAN/VPN:
run `kyora hub`, or `kyora serve --host 0.0.0.0`, and set `hub.expose_ui` to true in Settings, otherwise
`/api/events` answers 403 to non-loopback clients).

```sh
# 1. Dry run (default): prints the file map, sizes and sha256 prefixes; contacts nothing.
python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180

# 2. Apply. --events-url/--token are written into config.yaml's KyoraProcessor section
#    (the token is never printed). --kubectl-fallback allows `kubectl cp` into the pod.
python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180 --apply \
    --events-url http://<kyora-lan-ip>:8765 --token <hub token printed by `kyora hub`> \
    [--kubectl-fallback] [--namespace app]

# 3. Check that uvicorn reloaded and the processor connected:
kubectl -n app logs deploy/ace-controller-ace-controller-deployment -c ace-controller-container \
    | grep -E 'Kyora overlay|KyoraProcessor'
```

What `--apply` does, exactly as implemented in `deploy/upload_overlay.py`:

1. `GET <configurator>/openapi.json`. The Configurator's upload route is not documented in the
   vendored tree, so the script scores every PUT/POST/PATCH operation for "file upload addressed by
   service and volume", prints the candidates and uses the best one only if it clearly qualifies
   (otherwise it refuses to guess). Target: service `ace-controller-ace-controller-deployment`,
   volume `app-storage-volume` (from the vendored `tokkio-app-params.yaml`; `refresh_policy: HotReload`).
   A file counts as uploaded only on HTTP 2xx; if the spec has a matching GET, the file is read back
   and its sha256 compared.
2. With `--kubectl-fallback`, when there is no spec, no usable route or a failed upload: finds the
   pod (`-l app=ace-controller`, then a name search), checks that `/app/configs/config.yaml` and
   `/app/src/bot.py` exist in container `ace-controller-container`, `kubectl cp`s each file to
   `/app/<path>` and compares `sha256sum` inside the pod. `bot.py` is uploaded last, so its reload
   re-imports the processor and re-reads `.env`.
3. Prints a summary that separates "uploaded and verified", "uploaded, not read back", "failed" and
   "not attempted", and states that it cannot verify the uvicorn reload itself (step 3 above does).
   Exit status 0 only when every file was uploaded.

## How events reach the cluster

```
Kyora (kyora hub on your PC, LAN/VPN IP)           Tokkio cluster (ace-controller pod)
  pipeline -> EventBus -> GET /api/events  <------  KyoraProcessor SSE client (Bearer hub token)
                          POST /api/actions <-----  kyora:* button presses from the Tokkio UI
```

The connection is opened **from the pod to Kyora**, so Kyora's port (default 8765) must be reachable
from the cluster network (same LAN, a VPN, or a tunnel you operate). Set the address with
`--events-url` (or `KYORA_EVENTS_URL`) and the hub token with `--token` (or `KYORA_HUB_TOKEN`). In
Kyora's Settings, set `tokkio.ui_url` (`http://<node-ip>:30111`) and `tokkio.configurator_url`
(`http://<node-ip>:30180`) so the Presenter panel and `kyora selfcheck` can check them.

## The complete change to the vendored `bot.py`

Output of `diff -u vendor/tokkio-5.0.0-ga/src/llm-rag/src/bot.py tokkio-kyora/src/bot.kyora.py`
(file timestamps omitted; `tests/test_tokkio_overlay.py` regenerates it and fails if this block is stale):

```diff
--- vendor/tokkio-5.0.0-ga/src/llm-rag/src/bot.py
+++ tokkio-kyora/src/bot.kyora.py
@@ -19,6 +19,8 @@
 from .serializer import TokkioUIWebSocketSerializer
 from .rag_multimodal_response_processor import RAGMultimodalResponseProcessor
 from .otel import tracer_provider, is_otel_collector_up
+from pydantic import BaseModel  # KYORA: config section model, see customization notebook 11
+from .kyora_processor import KyoraProcessor  # KYORA: overlay processor (tokkio-kyora/src/kyora_processor.py)
 from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
 
 # from pipecat.audio.vad.vad_analyzer import VADParams
@@ -83,6 +85,20 @@
 )
 
 AnimationGraphService.pregenerate_animation_databases(config.AnimationGraphService)
+# KYORA ---------------------------------------------------------------------------
+class KyoraProcessorConfig(BaseModel):  # KYORA: mirrors the KyoraProcessor section of configs/config.yaml
+    events_url: str = ""  # KYORA: Kyora base URL reachable from the cluster
+    token: str = ""  # KYORA: Kyora hub token
+    narrate: bool = True  # KYORA
+    show_views: bool = True  # KYORA
+    reconnect_s: float = 3.0  # KYORA
+class KyoraConfig(Config):  # KYORA: extends the vendored Config without editing config.py
+    KyoraProcessor: KyoraProcessorConfig = KyoraProcessorConfig()  # KYORA: section is optional
+kyora_cfg = KyoraConfig(**yaml.safe_load(Path(os.getenv("CONFIG_PATH")).read_text())).KyoraProcessor  # KYORA
+kyora_events_url = kyora_cfg.events_url or os.getenv("KYORA_EVENTS_URL", "")  # KYORA: config first, env fallback
+kyora_token = kyora_cfg.token or os.getenv("KYORA_HUB_TOKEN", "")  # KYORA
+logger.info(f"Kyora overlay: events_url={kyora_events_url or '(not configured)'} narrate={kyora_cfg.narrate} show_views={kyora_cfg.show_views}")  # KYORA
+# KYORA ---------------------------------------------------------------------------
 
 redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
 redis_client = redis.Redis(
@@ -223,6 +239,10 @@
         # nvidia_tts_response_cacher = NvidiaTTSResponseCacher()
 
         rag_multimodal_response_processor = RAGMultimodalResponseProcessor()
+        kyora_processor = KyoraProcessor(  # KYORA
+            kyora_events_url, kyora_token,  # KYORA
+            narrate=kyora_cfg.narrate, show_views=kyora_cfg.show_views, reconnect_s=kyora_cfg.reconnect_s,  # KYORA
+        )  # KYORA
 
         pipeline = Pipeline(
             [
@@ -242,6 +262,7 @@
                 # nvidia_tts_response_cacher,  # For caching TTS response when Speculative Speech Processing is enabled
                 tts_transcript_synchronization,
                 rag_multimodal_response_processor,
+                kyora_processor,  # KYORA: after RAG custom views, before A2F; narration is queued at the pipeline head
                 a2f,
                 # audio_recorder,
                 posture_provider_processor,
@@ -265,6 +286,7 @@
             ),
         )
 
+        kyora_processor.attach_task(task)  # KYORA: task.queue_frame(TTSSpeakFrame) reaches the TTS upstream of us
         return task
 
 app = FastAPI()
```

Every added line carries `# KYORA`; no vendored line is removed or changed.

## Licence

* `src/kyora_processor.py`, `deploy/upload_overlay.py`, `.env.kyora.example` and these docs are Kyora
  code (MIT, see [`../LICENSE`](../LICENSE)).
* `src/bot.kyora.py` and `configs/config.kyora.yaml` are modified copies of NVIDIA files from Tokkio
  5.0.0-ga. `bot.py` carries NVIDIA's proprietary header, which conflicts with the Apache-2.0 `LICENSE`
  in the same NVIDIA directory; Kyora keeps the header unchanged, marks its own lines with `# KYORA`
  and records the discrepancy in [`../vendor/VENDOR.md`](../vendor/VENDOR.md). Using these files with a
  Tokkio deployment is subject to NVIDIA's terms.
* The Tokkio containers, Helm charts and avatar assets are pulled from NGC at deploy time under the
  NVIDIA AI Product License / NVIDIA AI Enterprise terms; Kyora does not ship them.
