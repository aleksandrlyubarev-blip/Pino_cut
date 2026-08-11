# Director LLM candidates on NVIDIA L40 — benchmark research (August 2026)

Status: research snapshot, 2026-08-11
Scope: which open-weight model should back the PinoCut Director layer on L40-class hardware (48 GB, Ada Lovelace), and how the choice interacts with a multi-agent fleet.

All numbers below are sourced; every claim carries a link in § 8. Benchmark scores are
self-reported by each vendor on their own model card unless marked otherwise, so
cross-vendor comparison is indicative, not controlled — reasoning budgets and harnesses
differ.

---

## 1. The candidate set

| Model | Params | Arch | Context | Released | License |
|---|---|---|---|---|---|
| **Muse Glimmer 30B** (Meta Superintelligence Labs) | 29.6B + 1.8B ViT | dense | 131,072 (→262K) | Aug 2026 | Apache 2.0 |
| **Gemma 4 31B** (Google DeepMind) | 31B | dense | 262,144 | Apr 2, 2026 | Apache 2.0 |
| **Gemma 4 26B A4B** (Google DeepMind) | 26B / ~4B active | MoE (128 experts, 8+1 active) | 262,144 | Apr 2, 2026 | Apache 2.0 |
| **Qwen3.6-27B** (Alibaba) | 27B | dense + hybrid linear attn | 262,144 (→1.01M YaRN) | Apr 22, 2026 | Apache 2.0 |
| **Qwen3.6-35B-A3B** (Alibaba) | 35B / 3B active | MoE + hybrid linear attn | 262,144 (→1.01M YaRN) | Apr 16, 2026 | Apache 2.0 |
| **Nemotron 3 Nano Omni 30B-A3B** (NVIDIA) | 30B / 3B active | MoE Transformer-Mamba | ~260K | 2026 | NVIDIA Open Model |

Naming correction worth recording: there is no single "Qwen 3.6 35B". Qwen3.6 shipped
**two** open checkpoints — a dense **27B** and a sparse **35B-A3B**. They behave very
differently under load, and the dense 27B is the stronger of the two on nearly every
benchmark.

## 2. Benchmark comparison

| Benchmark | Muse Glimmer 30B | Gemma 4 31B | Qwen3.6-27B | Qwen3.6-35B-A3B |
|---|---|---|---|---|
| AIME 2026 | **94.7** | 89.2 | 94.1 | 92.7 |
| GPQA Diamond | 83.5 | 86 | **87.8** | 86.0 |
| SWE-bench Verified | 76.0 | — | **77.2** | 73.4 |
| SWE-bench Pro | 51.2 | — | **53.5** | 49.5 |
| MMLU-Pro | — | — | **86.2** | 85.2 |
| MMMU-Pro (multimodal) | 74 | — | **75.8** | — |
| IFBench (instruction following) | **77.0** | 76 | — | — |
| LiveCodeBench v6 | — | — | 83.9 | — |
| Terminal-Bench 2.0 | — | 36 (TB Hard) | **59.3** | 51.5 |

Agentic-specific (Muse Glimmer only publishes these): MCP Atlas 75.5, DeepSearch QA 74.6,
WildClawBench 47.6, τ3-Banking 23.5. The last one is low enough to matter — long-horizon
tool loops with strict state are still weak across this whole weight class.

**Independent cross-check.** Artificial Analysis puts Gemma 4 31B at Intelligence Index
**39**, *behind* Qwen3.5 27B at **42**, and Gemma 4 26B A4B at only **31**. Two things
follow. First, the "Gemma is the intelligence leader" framing does not survive independent
evaluation — Qwen leads. Second, the 26B MoE is a real capability step down, not a
free speedup; NVIDIA's Nemotron 3 Nano Omni scores **15** on the same index, which places
it firmly as a perception/routing sub-agent rather than a reasoning brain.

Gemma's one clear independent win is **token efficiency**: 39M output tokens to complete
the AA Intelligence Index versus 98M for Qwen3.5 27B — ~2.5× fewer. On a fleet where you
pay in GPU-seconds rather than API dollars, that is a throughput argument, and it partly
cancels Qwen's raw-score lead.

## 3. KV cache — measured, not estimated

This is where the widely circulated summary of these models is most wrong. The claim that
"Google did not apply modern KV compression" is false. Gemma 4 31B applies four separate
techniques:

- 5:1 local-to-global layer ratio — 50 sliding-window layers (1,024-token window) + 10 full-attention layers out of 60
- key/value reuse on global layers (`values = keys`)
- proportional RoPE (p = 0.25) on global layers, cutting global KV by ~37.5%
- KV sharing — the last 6 layers reuse K/V tensors from earlier layers

Measured KV cache at full 262,144-token context, bf16:

| Model | KV cache @ 262K | Why |
|---|---|---|
| Gemma 4 31B | **20.78 GiB** | 10 global layers, 4 KV heads, head dim 512 |
| Qwen3.6-27B | **17.2 GB** | only 16 of 64 layers are Gated Attention; the other 48 are Gated DeltaNet (linear, constant-size state) |
| Gemma 4 26B A4B | **5.20 GiB** | 5 global layers, 2 KV heads |
| Qwen3.6-35B-A3B | very low | 10 of 40 layers attention, 2 KV heads |
| Muse Glimmer 30B | low | 32:2 GQA (16:1), 3:1 local:global, 2,048-token window |

So Gemma 4 31B and Qwen3.6-27B are in the **same ballpark** (20.8 vs 17.2 GiB) — Qwen is
not "several times lighter" as often claimed. The genuinely light options are the two MoE
models and Gemma 4 26B A4B, which is **4× lighter than its own dense sibling**. That is
the single most useful number in this document and it is missing from most comparisons.

KV quantization to 4-bit shrinks the cache ~4×, taking Qwen3.6-27B from 17.2 GB to roughly
4.3 GB. FP8 KV is the safer middle setting on Ada.

## 4. L40-specific deployment constraints

**NVFP4 does not work on L40.** NVFP4 is a Blackwell-only format. L40/L40S are Ada
Lovelace. Every "NVFP4 quant, 25.42 GB, 1.45× faster" recommendation for Muse Glimmer
applies to Blackwell, not to this fleet. Notably, the official vLLM recipe for Muse
Glimmer lists supported hardware as DGX Spark / GB300 / Blackwell and AMD MI300X-MI355X —
**Ada is not on the tested list**.

On L40 the practical choices are:
- **FP8 W8A8** — officially supported on Ada in vLLM, but community reports say it is not
  yet well optimized on L40S; AWQ is currently faster there
- **AWQ / GPTQ INT4** — the pragmatic default for this fleet
- **GGUF** — single-user local only; do not use it for a batched fleet

Muse Glimmer at FP8 is **32.78 GB**, which on a 48 GB L40 leaves only ~15 GB — not the
"~25 GB free" that the 4-bit figure implies. Only the 4-bit build (~17 GB) leaves real room
for a co-resident diffusion worker.

**vLLM version is not optional for Gemma 4.** vLLM v0.20.0 treated all Gemma 4 layers as
global and non-shared, over-allocating catastrophically: ~55K tokens of KV capacity. v0.21.0
recognizes the alternating/shared layout and delivers **~525K tokens** in the same ~50 GB —
a 9.5× difference from a version bump alone. Any capacity planning done on v0.20.0 is garbage.

**DFlash has a coupling cost.** Muse Glimmer's speculative decoder is a task-specific draft
model that "must be paired with the specific target it was distilled against." The advertised
3.1× speedup is measured on RTX 5090 (Blackwell); 1.8× on M5 Max, 1.5× on M4 Max. No Ada
number is published. Treat the L40 speedup as unknown until measured.

## 5. Sizing on a 48 GB L40

Weights at 4-bit, remainder available for KV and activations:

| Model | 4-bit weights | Free for KV | Verdict at long context |
|---|---|---|---|
| Gemma 4 26B A4B | 17.2 GB (AWQ) | ~30 GB | 5.2 GiB KV at full 262K → very large batch fits |
| Muse Glimmer 30B | ~17 GB | ~30 GB | light KV; room for a co-resident diffusion worker |
| Qwen3.6-27B | ~15–16 GB | ~31 GB | 17.2 GB KV at 262K bf16; ~4.3 GB with 4-bit KV |
| Gemma 4 31B | 20.5 GB (AWQ) | ~26 GB | 20.78 GiB KV at 262K — fits, but fills the card |
| Qwen3.6-35B-A3B | ~20 GB | ~27 GB | minimal KV, 3B active → highest tok/s of the dense-quality tier |

## 6. Recommendation for the PinoCut fleet

Mapped onto the layered architecture (agents are CPU containers; only inference servers and
diffusion workers hold GPUs):

- **Showrunner / story reasoning** — **Qwen3.6-27B**. Highest independent-ish scores in the
  class (GPQA 87.8, SWE-bench Verified 77.2, MMLU-Pro 86.2), hybrid linear attention keeps
  long-script KV tractable, Apache 2.0, and mature vLLM support. This is the brain.
- **Scene writers (N parallel replicas)** — **Gemma 4 26B A4B**. 4 GB active compute per
  token and a 5.2 GiB KV ceiling means the highest concurrency per card of anything here.
  Its lower Intelligence Index (31) is acceptable because scene agents work from a compressed
  brief, not the full lore.
- **Router / JSON validator / ComfyUI call-fixer** — **Nemotron 3 Nano Omni 30B-A3B**
  (~323 tok/s, fastest measured) or Gemma 4 E4B. Index 15 is fine for structured dispatch.
- **Continuity / storyboard QC (vision)** — **Muse Glimmer 30B** for its 1.8B ViT-G/14
  perception encoder (4,096 visual tokens/image, ScreenSpot Pro 75.4, Charxiv 78.8), or
  Nemotron Omni if audio and video input matter more than reasoning depth.
- **Gemma 4 31B** — hold. It is beaten by Qwen3.6-27B on most published benchmarks while
  using more VRAM for KV. Its token efficiency is real but does not outweigh that here.

## 7. What to re-verify before committing hardware

1. Benchmark scores in § 2 are vendor self-reported. Re-run the two finalists on a PinoCut-shaped
   eval (shot-list generation, Grok prompt packs, TimelineV1 JSON validity) before choosing.
2. Measure AWQ vs FP8 throughput on the actual L40 — the community claim that AWQ beats FP8
   on Ada is recent and unquantified.
3. Pin vLLM ≥ 0.21.0 in the serving image and record the version in the AgentCard.
4. Measure DFlash speedup on Ada if Muse Glimmer is selected; no published number exists.

## 8. Sources

- [Muse-Glimmer-30B model card](https://huggingface.co/meta-models/Muse-Glimmer-30B) — architecture, benchmarks, VRAM tiers
- [vLLM recipe: Muse-Glimmer-30B](https://recipes.vllm.ai/meta-models/Muse-Glimmer-30B) — supported GPUs, FP8/NVFP4 sizes, DFlash config
- [Unsloth: Muse Glimmer](https://unsloth.ai/docs/models/muse-glimmer) — quant sizes, sampling defaults
- [NVIDIA: local agentic workflows with Muse Glimmer](https://developer.nvidia.com/blog/run-local-agentic-ai-workflows-with-metas-muse-glimmer/)
- [Google: Gemma 4 announcement](https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/) — sizes, context, Arena placement
- [Gemma 4 Technical Report (arXiv 2607.02770)](https://arxiv.org/pdf/2607.02770)
- [Artificial Analysis: Gemma 4](https://artificialanalysis.ai/articles/gemma-4-everything-you-need-to-know) — Intelligence Index, token efficiency
- [The Kaitchup: Gemma 4 31B and 26B A4B architecture and memory](https://kaitchup.substack.com/p/gemma-4-31b-and-26b-a4b-architecture) — measured KV cache
- [vLLM issue #43308: Gemma 4 KV capacity in v0.21.0](https://github.com/vllm-project/vllm/issues/43308)
- [Qwen3.6-27B model card](https://huggingface.co/Qwen/Qwen3.6-27B)
- [Qwen3.6-35B-A3B model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [The Kaitchup: Qwen3.6-27B KV cache quantization](https://kaitchup.substack.com/p/qwen36-27b-kv-cache-quantization)
- [vLLM quantization docs (FP8 W8A8, Ada/Hopper support)](https://docs.vllm.ai/en/latest/features/quantization/)
- [NVIDIA: introducing NVFP4](https://developer.nvidia.com/blog/introducing-nvfp4-for-efficient-and-accurate-low-precision-inference/) — Blackwell-only
- [Artificial Analysis: Nemotron 3 Nano Omni 30B-A3B](https://artificialanalysis.ai/models/nemotron-3-nano-omni-30b-a3b)
- [GMI Cloud: open-weight benchmarks, August 2026](https://www.gmicloud.ai/en/blog/ai-model-benchmarks-august-2026-open-weight-models-catch-the-frontier)
