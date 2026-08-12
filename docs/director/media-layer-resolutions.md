# LTX-2.5 on L40 — resolution budget for 720p, 1080p and 1440p

Status: sizing plan, 2026-08-11
Companion to `serving-l40.md` (LLM side). Covers the diffusion worker's resolution budget.

---

## 1. Why the VAE decides everything

LTX-2's video VAE compresses **32× spatially and 8× temporally**, into 128 latent channels:

```
pixels  [B, 3, F, H, W]  →  latents  [B, 128, F', H/32, W/32]      F' = 1 + (F-1)/8
```

This is why the model card requires width and height divisible by 32 and
`num_frames % 8 == 1` — the constraints are the downsampling factors, not arbitrary.

The consequence that drives this whole document: **the DiT never sees pixels.** Sampling
happens over latent tokens, and the Stage 2 upsampler is a *latent* upsampler. Only the final
VAE decode touches full-resolution pixels. So raising output resolution is far cheaper than
pixel-count intuition suggests.

## 2. Alignment: the trap before the arithmetic

Neither 720p nor 1080p is natively legal — 720 and 1080 are not divisible by 32.

| Target | Naive | Legal | Fix needed |
|---|---|---|---|
| 720p | 1280×720 | **1280×704** (crop 16px) or 1280×736 (pad 16px) | yes |
| 1080p | 1920×1080 | **1920×1088** (pad 8px) | yes |
| 1440p | 2560×1440 | **2560×1440** — 80×45 | **none** |

1440p is the only one of the three that is natively aligned. Two clean ladders exist:

- **Exact 2× ladder (preserves the 1.818:1 frame of 1280×704):**
  `1280×704 → 2560×1408`. Both aligned, integer scale factor, no reframing.
- **True 16:9 ladder (every rung both 16:9 and 32-aligned):**
  `1024×576 → 2048×1152 → 2560×1440`.

There is no 16:9, 32-aligned rung at 720p. If deliverables must be exactly 16:9, generate at
1024×576 and upscale; if the frame is yours to choose, 1280×704 is the better base.

## 3. Latent token counts at 121 frames (F' = 16)

Latent tokens = `F' × (H/32) × (W/32)`. This is the number the DiT actually attends over:

| Resolution | Grid | Latent tokens | vs 720p |
|---|---|---|---|
| 960×544 (model card example) | 16 × 17 × 30 | 8,160 | 0.58× |
| **1280×704 (720p base)** | 16 × 22 × 40 | **14,080** | 1.0× |
| 1920×1088 (1080p) | 16 × 34 × 60 | 32,640 | 2.3× |
| 2048×1152 (16:9 1080p) | 16 × 36 × 64 | 36,864 | 2.6× |
| 2560×1408 (2× of base) | 16 × 44 × 80 | 56,320 | 4.0× |
| 2560×1440 (1440p 16:9) | 16 × 45 × 80 | 57,600 | 4.1× |

The latent tensor itself is trivial — 128 ch × 14,080 × 2 B = **3.6 MB at 720p**. Memory goes
to attention activations over the sequence, which with FlashAttention scale linearly, not
quadratically, in the token count.

## 4. Final decode — the actual peak of Stage 2

The output pixel tensor at 121 frames, bf16:

| Resolution | Output tensor |
|---|---|
| 1280×704 | 654 MB |
| 1920×1088 | 1.52 GB |
| 2048×1152 | 1.71 GB |
| 2560×1408 | 2.62 GB |
| 2560×1440 | 2.68 GB |

Decoder intermediates run several times that, which is exactly what VAE tiling bounds
("peak memory is bounded by the tile size"). Enable tiling for anything above 1080p.

There is also a decoder choice: the **diffusion decoder** gives better quality at the cost of
longer decode and more VRAM; the **convolutional decoder** is lighter and needs no extra
dependencies. On a 48 GB card the diffusion decoder is affordable at this ladder — reserve
the convolutional one for 4K work.

## 5. Direct generation vs. the ladder — pick by production stage, not by memory

The ladder is not mandatory, and framing it as the default was wrong. LTX generates natively
at high resolution: its own API offers no 720p tier at all — the rungs are 1080p → 1440p →
4K — and LTX-2.3 is documented as generating 3840×2160 *natively rather than upscaling to
it*. Sampling directly at 1080p is using the model as designed, not stretching it.

LTX's own guidance is to generate at native resolution during iteration and **not** upscale,
because upscaling mid-iteration costs time without changing the decisions being made;
upscaling belongs at the end of the pipeline. So the split is by stage:

| Stage | Resolution | Why |
|---|---|---|
| Take selection — composition, motion, continuity | **720p, no upscale** | fast feedback loop; the reviewer is judging framing and movement, not texture |
| Final render of approved shots | **1080p or 1440p directly** | native sampling, no upsampling artifacts |

The economics favour this sharply. A short runs ~400 generations to yield ~100 keepers.
Rendering all 400 at 1440p pays full price for 300 rejected takes; 400 drafts at 720p plus
100 native finals is roughly a 4× saving on the expensive half. It also maps onto the agent
fleet: the continuity/QC agent reviews cheap 720p takes, and only approved shots reach the
expensive render queue.

## 6. VRAM verdict on a 48 GB L40

Reference point: an RTX 5090 (32 GB) measured **peak 24.2 GB, flat across both 768×512 and
1280×704** — no wall at 720p class, because the DiT sampling cost is not what dominates.

| Phase | Resident | FP8, encoder offloaded |
|---|---|---|
| Stage 1 — sample at 1280×704 | DiT + activations | ~24–27 GB |
| Stage 2 — latent upsample to 1440p | upsampler ~5 GB + latents | ~8–10 GB |
| Final VAE decode @ 2560×1440 | decoder + output tensor, tiled | bounded by tile size |

**Verdict: 720p → 1080p/1440p sits comfortably on one L40 with roughly 20 GB to spare.** The
wall for this card is 4K, not 1440p. Offloading the Gemma text encoder after the text pass
(CPU, or Lightricks' API encoding node) is what buys the margin — it frees ~12 GB before
sampling starts.

## 7. Time, and why upscaling is cheap

On an RTX 5090, the distilled two-stage pipeline produces a 97-frame clip with synchronized
audio in ~40 s at 768×512 and ~50 s at 1280×704. That is **2.29× the pixels for 1.25× the
time** — strongly sublinear, because wall time is dominated by VAE decode and mp4/AAC
encoding rather than by DiT sampling.

Note what this does *not* settle. Since decode cost is set by output resolution, direct 1080p
and 720p→1080p pay the same decode; they differ only in how many latent tokens the 8-step
Stage 1 sampling runs over (32,640 vs 14,080). The ladder therefore does less work in the
expensive phase — but if decode genuinely dominates, the saving may be small. Benchmark both
routes at equal output resolution before standardising on either (§ 5 covers which to use
when).

**Do not read the 5090 timings as L40 timings.** L40 is Ada, the 5090 is Blackwell with
faster memory and NVFP4 paths that Ada lacks. Expect meaningfully slower and measure on the
spare card — no L40 figures are published.

## 8. Decision: ship on FP8 now, do not wait for 4-bit DiT quantization

Recorded because it will be re-asked. Four reasons, strongest first:

1. **We are not VRAM-bound.** Peak on this ladder is ~24–27 GB of 48 GB. Going 4-bit would
   take the DiT from ~22 GB to ~11 GB — saving memory that is already idle. It optimizes a
   resource we have in surplus.
2. **Diffusion is compute-bound, so weight-only 4-bit buys no speed.** This inverts the LLM
   intuition. LLM decode at batch 1 is memory-bandwidth-bound — every weight crosses the bus
   per token, so halving weight bytes nearly halves latency. Diffusion sampling processes
   thousands of latent tokens per step, weights are reused across all of them, and arithmetic
   intensity is high. The literature is explicit that diffusion models remain compute-bound
   even at small batch sizes, which is why serious 4-bit DiT work (DiRotQ, ConvRot,
   SVDQuant) all quantizes activations too — W4A4 or W4A8 — and activations are where quality
   is lost.
3. **Low-bit paths often never touch the low-bit tensor cores.** Production INT8 for diffusion
   transformers is documented to quantize, immediately dequantize back to bf16, and run a
   bf16 matmul — the INT8 cores go unused and "INT8" lands slower than FP8. Real speedups
   require fused custom kernels (DiRotQ ships Triton), not a checkpoint download.
4. **Quality risk is asymmetric in video.** Aggressive 4-bit PTQ is reported to cause severe
   degradation, and in video it surfaces as temporal instability — flicker, drift, texture
   crawl between frames — which is precisely what makes generated footage read as cheap.

Also note the published 4-bit DiT speedups (2.1–2.3×) are measured on FLUX.1-dev, an *image*
model, with bespoke kernels. Transfer to a video DiT with a temporal axis is not automatic.
Meanwhile LTX already ships `int8-convrot`, which is the ConvRot (ICLR 2026) rotation method
— the mature end of this research line — and it targets Ampere/Turing cards that lack FP8.
The L40 has FP8.

**Revisit this if** the target moves to 4K, or two LTX instances must share one card, or the
fleet moves to 24 GB cards. None applies today.

## 9. Caveats

1. Every VRAM and timing figure here is measured on **LTX-2.3**, mostly on RTX 5090. Nothing
   equivalent has been published for LTX-2.5 (released 2026-08-10). The VAE geometry and the
   two-stage structure carry over; the absolute numbers should be re-measured.
2. The latent-token arithmetic in § 3 is derived from the documented VAE shape, not measured.
   It is exact as geometry; it does not by itself predict activation memory.
3. `fp8-cast` on Ada may or may not hit hardware FP8 matmul — open question from
   `model-benchmarks-2026-08.md` § 4. Affects speed, not whether it fits.

## 10. Sources

- [Lightricks/LTX-2 (GitHub)](https://github.com/Lightricks/LTX-2) — VAE shape, `fp8-cast`, `--offload`, decoder choice
- [LTX-2: Efficient Joint Audio-Visual Foundation Model (arXiv 2601.03233)](https://arxiv.org/html/2601.03233v1)
- [LTX-Video: Realtime Video Latent Diffusion (arXiv 2501.00103)](https://arxiv.org/html/2501.00103v1)
- [Lightricks/LTX-2.5 model card](https://huggingface.co/Lightricks/LTX-2.5) — divisibility constraints, distilled schedule
- [RTX 5090 LTX-2.3 benchmark report](https://huggingface.co/datasets/witcheer/rtx-5090-benchmarks/blob/main/reports/ltx-2.3.md) — 24.2 GB flat peak, 40 s / 50 s timings
- [LTX 2.3 VRAM requirements tested](https://ltxworkflow.com/resources/community/ltx-23-vram-requirements-12gb-16gb-24gb)
- [LTX 2.3 ComfyUI: two-stage pipeline and Gemma encoder offload](https://wavespeed.ai/blog/posts/ltx-2-3-comfyui-setup-two-stage-pipeline/)
- [LTX 2.3 spatial and temporal upscaler](https://crepal.ai/blog/aivideo/ltx-2-3-spatial-temporal-upscaler/)
- [DiRotQ: rotation-aware 4-bit diffusion transformers (arXiv 2605.16732)](https://arxiv.org/abs/2605.16732) — compute-bound argument, W4A4/W4A8, Triton kernels
- [ConvRot: rotation-based 4-bit quantization for DiTs (ICLR 2026)](https://openreview.net/forum?id=SCC11m676G) — the method behind LTX's `int8-convrot`
- [ViDiT-Q (arXiv 2406.02540)](https://arxiv.org/pdf/2406.02540) · [DVD-Quant (arXiv 2505.18663)](https://arxiv.org/pdf/2505.18663) — video DiT quantization quality
- [Native INT8 compute for diffusion transformers (arXiv 2606.14598)](https://arxiv.org/html/2606.14598v1) — the dequantize-to-bf16 trap
