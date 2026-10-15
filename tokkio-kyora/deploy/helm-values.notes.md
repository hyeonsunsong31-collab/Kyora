# Helm / UCS values that matter for the Kyora overlay

Short answer: **the overlay needs no Helm values change.** It is applied at runtime to the
ace-controller's hot-reload volume by `upload_overlay.py`. This page lists the values in the vendored
Tokkio 5.0.0-ga profiles (`vendor/tokkio-5.0.0-ga/llm-rag/tokkio-<n>stream-<ui|no-ui>/`) that Kyora
relies on, so you can check them if your deployment was customized. File names are relative to one
profile folder; nothing here is edited by Kyora.

## Where the overlay lands

`tokkio-app-params.yaml` -> `ace-configurator.configurator.config`:

```yaml
services:
  ace-controller-ace-controller-deployment:
    app-storage-volume:
      container_name: ace-controller-container
      refresh_policy:
        type: HotReload
      config_type: Pv
```

`upload_overlay.py` targets exactly this service / volume / container. The ace-controller image
(`src/llm-rag/Dockerfile`) uses `WORKDIR /app`; `entrypoint.sh` runs
`uvicorn ... src.bot:app --reload --reload-dir ./ --reload-include $CONFIG_PATH` with
`CONFIG_PATH=./configs/config.yaml`, so new files under `/app` are picked up by uvicorn's reloader.
The kubectl fallback assumes that layout and refuses to copy if `/app/configs/config.yaml` or
`/app/src/bot.py` is missing.

## The ace-controller config file

`tokkio-app.yaml` -> component `ace-controller` (type `ucf.svc.ace-controller`):

```yaml
files:
  config.yaml: ./config/ace-controller/config.yaml
secrets:
  openai-key-secret: k8sSecret/openai-key-secret/OPENAI_API_KEY
  nvidia-api-key-secret: k8sSecret/nvidia-api-key-secret/NVIDIA_API_KEY
  elevenlabs-api-key-secret: k8sSecret/elevenlabs-api-key-secret/ELEVENLABS_API_KEY
```

`config/ace-controller/config.yaml` is one of the six vendored **symlinks** (to
`../../../../src/llm-rag/configs/config.yaml`); the release ZIP stores them as symlinks, exactly as
NVIDIA's archive does. If you rebuild the app with NVIDIA's "customize with rebuild" flow instead of
uploading at runtime, point that file at `tokkio-kyora/configs/config.kyora.yaml` and copy
`kyora_processor.py` and `bot.kyora.py` (as `bot.py`) into `src/llm-rag/src/` of **your copy** of the
tree before building the image; keep `vendor/` itself unmodified (`scripts/vendor_tokkio.py --verify`).

The secrets are mounted under `/secrets` and exported by `entrypoint.sh` (`NVIDIA_API_KEY`,
`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`). The overlay's `NvidiaLLMService` (model
`nvidia/nemotron-3-ultra-550b-a55b`, base URL `https://integrate.api.nvidia.com/v1`) uses
`NVIDIA_API_KEY`; the one-click scripts still require all three secrets to be non-empty.

## GPUs

`tokkio-app-params.yaml` (`ucfVisibleGpus` / `NVIDIA_VISIBLE_DEVICES` / `nvidiaVisibleDevices`):

| Profile | Riva ASR, Audio2Face-3D, VMS | Animation graph | Unreal renderer | Total |
|---|---|---|---|---|
| 1-stream | GPU 0 | GPU 1 | GPU 1 | 2 GPUs |
| 3-stream, 6-stream | GPU 0 | GPU 3 | GPUs 1, 2, 3 | 4 GPUs |

The overlay adds no GPU work: `KyoraProcessor` is a pass-through pipecat processor with one
background HTTP stream.

## Ports and environment

* Tokkio UI: NodePort **30111**; ACE Configurator: NodePort **30180** (the one-click IaC opens it, e.g.
  `scripts/one-click/aws/iac-ref/main.tf`). Put these into Kyora's Settings (`tokkio.ui_url`,
  `tokkio.configurator_url`).
* The ace-controller params in `tokkio-app-params.yaml` (`DEV`, `replicas`, `OTEL_*`) need no change.
  `KYORA_EVENTS_URL` / `KYORA_HUB_TOKEN` are only fallbacks; the reliable place for them is the
  `KyoraProcessor` section of `config.yaml`, which `upload_overlay.py --events-url ... --token ...`
  fills in. Whether the UCF ace-controller microservice accepts extra environment variables through
  its params is not documented in the vendored tree, so Kyora does not depend on it.
* Outbound: the ace-controller pod must reach Kyora's host and port (default 8765) for the event
  stream; nothing on the cluster needs to accept inbound connections from Kyora.
