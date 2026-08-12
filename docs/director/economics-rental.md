# Economics: rented L40 at $0.80/hour vs generation APIs

Status: costing model, 2026-08-11
Input assumption: L40-class cards available on demand at **$0.80/GPU-hour**. Now anchored to
a real quote — WOMBO Inference lists **L40 48 GB at $0.79/GPU-hr** (from $0.60 at ≥10K
GPU-hr/mo) and a **24 GB L40G tier at $0.45/hr**. See `provider-wombo-evaluation.md` for the
offer's constraints, which affect the fleet design more than the price does.
Companions: `serving-l40.md` (LLM sizing), `media-layer-resolutions.md` (diffusion sizing).

---

## 1. Why renting changes the earlier conclusion

An earlier costing compared APIs against **buying** a card (~$7–9K) and put break-even at
two to three feature-length projects. Renting removes the capital expense, so there is no
break-even to reach — the comparison becomes pure cost-per-second of output, and the
hardware side wins at every tier.

$0.80/GPU-hour = **$0.000222 per wall-clock second.**

## 2. Cost per second of finished video

The one unknown is wall time per clip. Our estimate is ~2 minutes for a 5-second 1080p clip
with audio on an L40, derived from the RTX 5090 measurement scaled by memory bandwidth —
**not measured**. So the table is parameterised on that unknown rather than asserting it:

| Wall time per 5 s clip | Cost per clip | **Cost per video-second** |
|---|---|---|
| 1 min | $0.013 | $0.0027 |
| **2 min (our estimate)** | **$0.027** | **$0.0053** |
| 4 min | $0.053 | $0.0107 |
| 8 min | $0.107 | $0.0213 |

Against Grok Imagine pricing:

| Route | Cost per video-second |
|---|---|
| Rented L40, 2 min/clip | **$0.0053** |
| Grok Imagine, 480p, third-party | $0.008 |
| Grok Imagine, 1080p, third-party | $0.025 |
| Grok Imagine, xAI API (native audio) | $0.05 |

**The conclusion is robust to our estimate being badly wrong.** Break-even against the
$0.025/sec tier sits at **~9.4 minutes** of wall time per 5-second clip; against xAI's
$0.05/sec it sits at **~18.8 minutes**. As long as an L40 renders a 5-second 1080p clip in
under nine minutes — nearly 5× worse than our estimate — renting still wins.

## 3. Project-level totals

| Project | Generations | Video-seconds | Rented L40 | Grok @ $0.025 | Grok @ $0.05 |
|---|---|---|---|---|---|
| Short (400 gens → ~100 keepers) | 400 × 5 s | 2,000 | **~$11** | $50 | $100 |
| Feature (6,000 gens) | 6,000 × 5 s | 30,000 | **~$159** | $750 | $1,500 |

Wall-clock cross-check on the short: 400 clips × 2 min = 13.3 GPU-hours = $10.67. Consistent.
On one card that is an overnight run; on six rented cards, ~2¼ hours for the same $11 — you
pay for GPU-hours, not for wall time, so parallelism is free.

## 4. What rented cards are actually good for

Renting suits **bursty** workloads, which is most of this pipeline:

- **Burst rendering.** Rent 10 cards for two hours when a project needs them (~$16) instead
  of owning ten cards that idle between projects.
- **LoRA training.** Character and style LoRAs are the strongest argument for self-hosting
  (see § 6), and training is the definition of bursty — hours of load, then nothing.
- **Benchmarking.** The measurement plan in `serving-l40.md` § 5 and
  `media-layer-resolutions.md` needs a card for hours, not permanently.
- **The whole fleet.** The layered architecture — LLM inference servers, CPU-only agent
  containers, diffusion workers — runs identically on rented cards. Nothing in the design
  assumes owned hardware.

Renting suits **steady** load poorly: a card under continuous use costs $7,008/year at
$0.80/hr, which is roughly its purchase price. If the Director's inference server ends up
running 24/7, that one is the candidate to buy; the diffusion workers stay rented.

## 4a. Consumer and AMD alternatives to the L40

| Card | VRAM | Rental | Availability |
|---|---|---|---|
| **RTX 5090** | **32 GB** | **$0.27–0.60/hr** (spot from ~$0.09) | Vast.ai, ~12 providers |
| L40 / L40S | 48 GB | $0.40 (Vast spot) – $0.79 | Vast, RunPod, WOMBO |
| AMD MI300X | 192 GB | $1.85–3.49/hr | Thunder Compute, CoreWeave, RunPod |
| AMD W7900 | 48 GB | effectively not rented | — |

**The RTX 5090 is faster than the L40, and our own benchmark proves it.** Every LTX timing in
`media-layer-resolutions.md` originates from a 5090 measurement — 97 frames at 1280×704 with
audio in ~50 s, 24.2 GB peak — which we then scaled *down* by memory bandwidth to estimate
L40 performance. The 5090 is Blackwell: ~1792 GB/s against the L40's 864 GB/s, and it supports
**NVFP4**, the format we ruled out twice for Ada (both for LTX and for Unsloth's quants). So it
wins on price per unit of work twice over: cheaper per hour *and* faster per clip.

**But 32 GB is a wall, and it falls in the wrong place:**

| Workload | Requires | RTX 5090 (32 GB) |
|---|---|---|
| LTX 720p, 5 s, encoder offloaded | 24.2 GB (measured) | **fits**, ~7 GB spare |
| LTX 1080p direct | ~29–34 GB | **marginal to no** |
| LTX 20-second clip | 3.8× the latent tokens | **no** |
| Director, Qwen3.6-27B-FP8 | 31 GB + KV | **no** |

§ 5 of `media-layer-resolutions.md` concluded that final shots should be sampled natively at
1080p. That is precisely what the 5090 cannot hold.

**AMD is the wrong economics here.** MI300X costs 2.5–4× the L40 hourly rate to deliver 192 GB
we do not need — everything was deliberately sized under 48 GB. The W7900 (48 GB) is a
workstation purchase, not a rental SKU. Software has improved (ComfyUI Desktop gained official
ROCm support in v0.7.0, January 2026, and AMD publishes text-to-video tutorials for Radeon),
but LTX's own optimisations — `fp8-cast`, NVFP4 — target NVIDIA, and there is no published
data on LTX-2.5 under ROCm at all. Risk without compensating benefit.

### 4b. The NVFP4-on-32GB vs FP8-on-48GB fork is a false one

The obvious counter-argument to the 32 GB wall is NVFP4: on Blackwell, 4-bit weights would
roughly halve the DiT and bring 1080p back into range on a 5090. Three measurements say not
to take that trade.

- **"NVFP4 shrinks a video model 33% — with zero speed gain"** (DGX Spark benchmark). The
  cause is the one established in `media-layer-resolutions.md` § 8: video diffusion is
  compute-bound, so weight-only quantization shrinks the file and leaves throughput alone.
- **"RTX 5090 NVFP4 quantization tested: T2V and I2V quality completely different"** —
  degradation is mode-dependent and unpredictable.
- Head-to-head on FLUX: *FP8 scaled holds quality with almost no degradation, while NVFP4
  degrades*; FLUX Dev under NVFP4 is described as "low quality at the moment".

NVFP4's advertised ~1.9× over FP8 is an LLM result; it does not reproduce on video. **So the
trade is quality paid for VRAM, with nothing returned.** Corollary: even when drafting on a
5090, run **FP8, not NVFP4** — 720p in FP8 is the measured 24.2 GB and fits 32 GB comfortably.
NVFP4 solves a problem we do not have.

### 4c. The card that dissolves the fork

| Card | VRAM | Arch | Rental |
|---|---|---|---|
| **RTX PRO 6000 Blackwell** | **96 GB** | Blackwell | **$0.63–0.67** (Vast) · $1.69–2.09 (RunPod) · median ~$1.73–2.20 |
| L40 | 48 GB | Ada | $0.79 |
| RTX 5090 | 32 GB | Blackwell | $0.27–0.60 |

96 GB of Blackwell, on marketplace tiers **cheaper than the L40** — Vast lists the Server
Edition at $0.63 and Workstation at $0.67, packet.ai at $0.66. This removes the need to choose
between NVFP4-on-32 and FP8-on-48: run **FP8 on 96 GB**, on faster silicon, for less. Twice
the headroom needed even for 20-second clips at 1440p, with no quantization compromise.

Caveats that keep it honest: $0.63–0.67 is marketplace spot pricing carrying the same
eviction risk as the 5090 tier; the reliable providers charge $1.69–2.09, roughly double the
L40. Availability is also thinner and newer — the card only reached clouds from late 2025.

### Recommended split: by tolerance for interruption

| Stage | Card | Format | Rationale |
|---|---|---|---|
| Draft takes, 720p, selection | **RTX 5090 spot** | **FP8** | ~2× faster, ~2× cheaper, 24.2 GB fits 32; an eviction costs one re-run |
| Final shots 1080p/1440p, long clips | **RTX PRO 6000 96 GB** at ~$0.65 | FP8 | dissolves the VRAM question entirely (§ 4c) |
| Same, when reliability is required | L40 48 GB ($0.79), or RTX PRO 6000 on RunPod | FP8 | SLA-backed capacity |
| Director LLM | L40 or RTX PRO 6000 | FP8 | 31 GB resident plus KV, needs a persistent process |

Draft-pass arithmetic: 400 takes at ~1 min on a 5090 is 6.7 GPU-hr × $0.40 = **$2.67**, against
400 at ~2 min on an L40 = 13.3 GPU-hr × $0.79 = **$10.53**. Trivial in absolute terms, but the
**4× ratio holds at any scale**, and drafts are the dominant volume (§ 3).

**The balancing caveat.** RTX 5090 capacity on marketplaces is largely private individuals'
home machines: no ECC, no uptime SLA, spot reclaimed on 15 seconds' notice — the reason those
platforms publish per-host reliability scores. Fine for drafts, where a lost clip costs a
re-run. Not fine for a 20-second `extend` chain on an approved shot, where an eviction loses
the whole sequence. That is why the split above is drawn on interruption tolerance, not on
VRAM alone.

## 4d. Versus a Grok subscription (not the per-second API)

A flat subscription is different economics from the per-second API costed in § 2, and it is
the comparison that actually applies if a SuperGrok seat is already in hand.

**What the subscription provides.** SuperGrok is $30/month, the minimum tier carrying video.
Official quotas are 50 renders/day on Premium, 100 on Premium+, 500 on SuperGrok Heavy.
Reported reality is **10–15 clips/day at 720p** before fair-use throttling, and quotas were
recently cut by up to 80% — "roughly 10 generations per 8 hours instead of dozens per day".
**xAI does not publish limits at all**; every figure here comes from user reports.

Also worth verifying on the account itself: one source puts the $30 tier's ceiling at
**720p** (30 s max), with 1080p appearing in API and 1.5-preview contexts. Confirm before
assuming 1080p is included.

**Per-clip cost is not where the difference lies:**

| Route | Per 5 s clip | Ceiling |
|---|---|---|
| **Grok subscription** (at a full 12/day) | ~$0.083 | **10–15 per day** |
| RTX 5090 spot, $0.40/hr | ~$0.007 | none |
| RTX PRO 6000, $0.65/hr | ~$0.011 | none |
| L40S, $0.79/hr | ~$0.026 | none |

Self-hosting is 3–12× cheaper per clip, but both are pocket change in absolute terms.

**The difference is throughput, and it is ~30×.** A short film is ~400 generations (§ 3):

| | Time for 400 generations |
|---|---|
| **Grok subscription** (12/day) | **33 days** |
| One rented card | **one overnight run** |
| Six cards | **~2 hours** |

**The compounding risk is that the quota is undisclosed and retroactively cut.** A production
schedule cannot be planned against an undocumented limit the vendor reduced by 80% without
notice. That is an inconvenience for a hobbyist and disqualifying for deadline work.

**Net position.** Self-hosting loses on exactly two things: setup and operations time (days of
real work — container, benchmarking, debugging without SSH, multi-homing), and ownership of
failures. It gains ~30× throughput, predictability, LoRA-based character consistency, version
stability, no content filters, IP containment, and 20-second clips.

**Recommendation: keep the subscription and build the engine anyway.** $30/month is cheap for
what Grok is genuinely good at — fast ideation, checking a composition, showing a client, and
reference-to-video with up to seven reference images. Self-hosting handles volume: hundreds of
takes per pass, final renders, and anything needing LoRA or continuity. The boundary is
neither quality nor price but **quantity — under ~10 clips/day, subscription; above it, own
capacity.**

### 4e. If the seat is SuperGrok Heavy (a $100/month bill)

**$100/month is the promotional rate.** SuperGrok Heavy lists at **$300/month**, with ~$99 for
the first three months. Confirm the promo end date in billing — the bill triples on schedule.

**At Heavy's quota the throughput argument in § 4d largely evaporates.** Reported allowance is
**500 renders/day** against 50 on Premium and 100 on Premium+:

| | 400 generations |
|---|---|
| Heavy at 500/day | **under one day** |
| $30 tier at a real 10–15/day | 33 days |

Caveat: xAI's stated quotas and observed throughput diverge — the $30 tier advertises 50/day
against user reports of 10–15 before throttling. Whether Heavy has the same gap is
undocumented. **The account holder can measure this directly**, and that observation beats any
published estimate.

**The decisive number is now the price cliff, not throughput.** $300/month is $3,600/year:

| | For $3,600/year |
|---|---|
| RTX PRO 6000 (96 GB) at $0.65/hr | **~5,500 GPU-hours** |
| At ~1 min/clip | **~330,000 clips/year** |
| SuperGrok Heavy at its theoretical 500/day | 182,500 clips/year |

Own capacity delivers roughly **double the output for the same spend** — measured against the
subscription's *theoretical* ceiling, before any throttling, and without the 720p/1080p
uncertainty, with LoRA available and no content filter in path.

**Plan for the promo window.** Run volume through the subscription while it is cheap; in
parallel, stand up the container and take real measurements (WOMBO's free 48-hour evaluation
plus RunPod for comparison costs nothing and closes the outstanding unknown — the ~2 min/clip
estimate in § 2 is still unverified). By the time the rate goes to $300, the decision rests on
two numbers that do not exist today: Heavy's actual sustained daily throughput, and this
pipeline's real wall time per clip.

**Verify on the account now:** that the tier is Heavy and $99 is promotional (with its end
date); whether 1080p is actually included or the ceiling is 720p; and how many clips genuinely
complete before throttling begins.

## 5. Teaching video generation on rented capacity

The economics are unusually favourable, because student workloads are bursty and small.

Per student, per course, assuming ~100 generated clips:

```
100 clips × 2 min = 3.3 GPU-hours × $0.80 = $2.67 per student
```

At any realistic course price that is a rounding error — under 2% of revenue at $200/seat.
Practical shape:

- **Labs, not dedicated cards.** A 3-hour lab with 10 students does not need 10 cards.
  Students are prompting, reviewing and discussing most of the time; a queue over 3–4 cards
  absorbs the peaks. Rent for the lab window, release afterwards.
- **One Director card serves the whole class.** `Qwen/Qwen3.6-27B-FP8` at 128K with
  continuous batching handles a class's worth of concurrent agents on a single card — that
  is exactly what the ~17 GB spare KV pool in `serving-l40.md` § 2 is for.
- **Class size scales by rental, not procurement.** Twenty students next cohort is a
  parameter change, not a purchase.

It also monetises the knowledge already accumulating in these documents, and aligns with the
Director persona's mentorship role (`architecture.md` § 6 — "teaches while correcting").

## 6. What renting does *not* buy back

The reasons to run your own weights survive the cost analysis, because none of them is about
price per second:

1. **Version stability.** An API vendor updating its model mid-production breaks continuity
   between shots filmed months apart. Frozen local weights reproduce the same frame from the
   same seed indefinitely. For long-form work this is the decisive argument.
2. **LoRA.** LTX ships a LoRA trainer. Training on your own characters and style is
   structurally stronger than reference images for cross-film consistency.
3. **Content policy.** APIs refuse material that legitimate dramatic work requires.
4. **IP.** Unreleased footage does not leave your infrastructure.

## 7. Verify before committing

$0.80/hour is only the headline. What routinely breaks rental economics:

1. **Cold start.** The LTX component set is ~66 GiB. If weights download on every spin-up,
   you pay GPU-hours to watch a progress bar. Confirm persistent volumes, and price them —
   storage is usually billed separately.
2. **Spot vs on-demand.** $0.80 may be preemptible pricing. An interruption mid-render on a
   20-second extend chain loses the whole clip. Check the interruption policy.
3. **Egress.** Video is large. A short's worth of 1080p output is tens of GB leaving the
   provider. Egress is frequently the line item that dwarfs GPU cost.
4. **Idle billing.** Whether you pay while the container is provisioned but not computing
   determines whether "rent for the lab window" actually works.
5. **The wall-time estimate.** Everything in § 2 scales off the unmeasured ~2 min/clip
   figure. One benchmark run on a rented card replaces the whole table with real numbers —
   do that before signing anything.

## 8. Sources

- [Vast.ai RTX PRO 6000 S](https://vast.ai/pricing/gpu/RTX-PRO-6000-S) · [RTX PRO 6000 WS](https://vast.ai/pricing/gpu/RTX-PRO-6000-WS) · [RTX PRO 6000 across 30+ providers](https://getdeploying.com/gpus/nvidia-rtx-pro-6000) · [Thunder Compute RTX PRO 6000 pricing, Aug 2026](https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing)
- [NVFP4 shrinks a video model 33% with zero speed gain](https://ai-muninn.com/en/blog/dgx-spark-sulphur-nvfp4-video) · [RTX 5090 NVFP4 tested: T2V vs I2V quality](https://zenn.dev/toki_mwc/articles/rtx5090-nvfp4-quantization-reality?locale=en) · [BF16 vs GGUF vs FP8 scaled vs NVFP4 quality](https://github.com/FurkanGozukara/Stable-Diffusion/discussions/357)
- [Vast.ai RTX 5090 pricing](https://vast.ai/pricing/gpu/RTX-5090) · [RTX 5090 across 12+ providers](https://getdeploying.com/gpus/nvidia-rtx-5090) · [GPU Finder: RTX 5090](https://gpufinder.dev/gpu/rtx-5090)
- [Vast.ai L40S pricing](https://vast.ai/pricing/gpu/L40S) · [RunPod vs Lambda vs CoreWeave](https://www.buildmvpfast.com/blog/gpu-cloud-cost-comparison-runpod-lambda-labs-coreweave-2026)
- [MI300X cloud pricing](https://gpufinder.dev/gpu/mi300x) · [AMD MI300X/MI355X pricing 2026](https://www.spheron.network/blog/amd-mi300x-mi355x-pricing-2026/) · [Radeon PRO W7900 pricing](https://www.notebookcheck.net/AMD-Radeon-Pro-W7900-Dual-Slot-gets-500-price-cut-up-to-52-better-perf-per-dollar-compared-to-RTX-6000-Ada.843500.0.html)
- [ComfyUI on AMD ROCm](https://rocm.blogs.amd.com/artificial-intelligence/comfyui/README.html) · [Text-to-video with ComfyUI on Radeon](https://rocm.docs.amd.com/projects/ai-developer-hub/en/latest/notebooks/inference/t2v_comfyui_radeon.html) · [AMD GPUs for AI inference in 2026](https://idfs.ai/blog/amd-gpus-for-ai-inference-2026)
- [RTX 5090 LTX-2.3 benchmark report](https://huggingface.co/datasets/witcheer/rtx-5090-benchmarks/blob/main/reports/ltx-2.3.md)
- [Grok pricing 2026: all tiers](https://aitoolanalysis.com/supergrok-subscription-price-2026/) · [Grok pricing: Free, SuperGrok, Heavy & API](https://www.ai-toolbox.co/grok-models/grok-pricing-plans-api-2026) · [Grok pricing plans and weekly limits](https://felloai.com/grok-pricing/)
- [SuperGrok video/image generation pricing math](https://www.buildfastwithai.com/blogs/supergrok-video-image-generation-2026-speed-pricing-math-comparison) · [Grok Imagine daily limits by tier](https://www.arsturn.com/blog/grok-imagines-daily-generation-limits-what-you-need-to-know) · [New Grok Imagine limits spark user fury (Ctech)](https://www.calcalistech.com/ctechnews/article/rkbynj99bx) · [Is SuperGrok still worth $30/mo](https://aiveed.io/blog/supergrok-30-month-still-worth-it-2026)
