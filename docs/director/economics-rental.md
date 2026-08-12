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
