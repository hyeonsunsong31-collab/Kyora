# Nemotron-3-Ultra reference (what Kyora uses, and what the uploaded archive is)

## The uploaded archive is the training-recipe repository

`Nemotron-main.zip` is NVIDIA's **Nemotron developer repository** (Apache-2.0): training recipes
(`src/nemotron/recipes/ultra3/`: pretraining and SFT stages), deployment and usage cookbooks, docs
for MOPD/RL and quantization, and a "skill" folder that summarizes the Nemotron 3 Ultra tech report
(`skills/nemotron-ultra/paper/*.md`). It contains **no model weights and no inference server**, and
Kyora does not vendor it: nothing from it is needed at runtime.

Running the weights yourself is far outside a PC or phone. The repository's quantization notes give
the post-trained BF16 weights as about **1.1 TB** ("fits on 1 node (4x B200) [NVFP4] vs 2 nodes
(8x B200) for BF16"); BF16 serving therefore needs on the order of **8 B200-class GPUs, or 16 H100
80 GB GPUs**. The NVFP4 checkpoint (331 GB measured) fits on 4 B200. Kyora instead calls the hosted
model over NVIDIA's API.

## The model Kyora calls

| | |
|---|---|
| Model | NVIDIA Nemotron 3 Ultra, 550B total / 55B active parameters, hybrid Mamba-Attention mixture-of-experts, up to 1M tokens of context |
| Hosted id | `nvidia/nemotron-3-ultra-550b-a55b` |
| Endpoint | `https://integrate.api.nvidia.com/v1/chat/completions` (OpenAI-compatible), key from build.nvidia.com in `config/keys.json` -> `nvidia.nemotron.api_key` |
| Role in Kyora | text/design director: GDD prose, narrative and dialogue, UI/UX spec with design tokens and copy, presenter narration (`kyora/models/nemotron.py`) |

Sampling used by Kyora (`kyora/models/nemotron.py`), with thinking switched through
`chat_template_kwargs.enable_thinking`:

| Mode | When | Request |
|---|---|---|
| thinking on | design document and UI spec (default; `settings.nemotron.thinking_for_design`) | `temperature 1.0`, `top_p 0.95`, `chat_template_kwargs: {"enable_thinking": true}` |
| thinking off, structured | design document and UI spec when the setting is false, and every retry of an unusable answer | `temperature 1.0`, `top_p 0.95` (the model card's defaults), `chat_template_kwargs: {"enable_thinking": false}` |
| thinking off, short | presenter narration (one sentence, at most 120 characters) | `temperature 0.2`, `chat_template_kwargs: {"enable_thinking": false}` |

A design document or UI spec that arrives cut off at the token limit, or without a required part, is
asked for once more without thinking and as plain JSON text (no forced tool call), then once the same
way from the next model of the Nemotron role's chain (`fallback_models`, published as
`model.fallback`). On 2026-10-18, with thinking and a forced tool call, Nemotron-3-Ultra spent whole
budgets (16384 tokens of design, 12288 of UI spec) without a usable call, and a forced-tool retry at
`temperature 0.2` repeated itself until 24576 tokens; long structured answers are therefore never
sampled at the low temperature, and a retry does not force the tool call.

The Tokkio avatar's own real-time LLM (`tokkio-kyora/src/bot.kyora.py`, outside Kyora's process) uses
the thinking-off request as well: `temperature 0.2` and `extra_body={"chat_template_kwargs":
{"enable_thinking": false}}` through pipecat's `InputParams.extra`, so the avatar answers without a
silent reasoning phase.

The design document and the UI spec are requested as structured tool calls (`emit_design`, `emit_ui_spec`). `kyora selfcheck` measures the
model's reachability, time to first token and tokens per second from your device; those measured
numbers are what the UI shows (see [`honesty.md`](honesty.md)).

## Published scores (from the tech report summary in the archive)

Post-trained Nemotron 3 Ultra 550B-A55B, BF16, as reported by NVIDIA ("NVIDIA Nemotron 3 Ultra v3
Tech Report", 2026-06-03, Table 10; copied from `skills/nemotron-ultra/paper/evaluation.md`).
These are NVIDIA's numbers under NVIDIA's harnesses, not measurements by Kyora:

| Area | Benchmark | Score |
|---|---|---|
| Agentic / coding | SWE-Bench Verified | 71.9 |
| | SWE-Bench Multilingual | 67.7 |
| | Terminal Bench 2.1 | 56.4 |
| | TauBench V3 (average) | 70.9 |
| | BrowseComp | 44.4 |
| | GDPVal | 46.7 |
| | PinchBench (held-out gate) | 90.0 |
| | ProfBench (held-out gate) | 56.0 |
| Code / math | LiveCodeBench v6 | 89.0 |
| | IOI 2025 | 570.0 |
| | IMOAnswerBench (no tools / with tools) | 88.6 / 92.3 |
| | Apex-Shortlist (no tools / with tools) | 74.9 / 84.8 |
| Knowledge / reasoning | GPQA (no tools) | 87.0 |
| | MMLU-Pro | 86.8 |
| | HLE (no tools / with tools) | 26.7 / 37.4 |
| | SciCode (subtask) | 44.6 |
| | AA-Omniscience accuracy / non-hallucination | 24.1 / 78.7 |
| Chat / instruction following | IFBench (prompt loose) | 81.7 |
| | Multi-Challenge | 63.8 |
| | Arena-Hard-V2 | 88.1 |
| Long context / multilingual | RULER 1M | 94.7 |
| | AA-LCR | 65.4 |
| | LongBench v2 (<=1M) | 61.9 |
| | MMLU-ProX (10 languages) | 83.0 |
| | WMT24++ (en->xx) | 83.7 |

None of these is 94 or above on the coding benchmarks, and none of them is the "94 %" in Kyora.
Kyora's 94 % is a per-project quality gate and the Kyora Bench threshold measured on your own runs
(see [`honesty.md`](honesty.md) and [`bench.md`](bench.md)).

## Licence and attribution

The Nemotron repository is Apache-2.0. Use of the hosted model goes through NVIDIA's API Catalog under
NVIDIA's terms. Kyora attributes its output to "NVIDIA Nemotron-3-Ultra" (status API `attribution`, UI).
