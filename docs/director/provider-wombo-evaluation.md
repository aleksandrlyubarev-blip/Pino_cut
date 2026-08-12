# WOMBO Inference — evaluation against the PinoCut fleet design

Status: provider assessment, 2026-08-11
Source: *WOMBO Inference — Provider Overview*, July 2026 (partner document).
Companions: `economics-rental.md`, `serving-l40.md`, `media-layer-resolutions.md`.

---

## 1. What is actually on offer

Two consumption modes:

- **Option A — inference endpoints**, priced per output. Their models, their stack.
- **Option B — managed dedicated capacity**, priced per GPU-hour. Your container, their fleet.

Pricing relevant to us:

| Item | List | Volume (≥10K GPU-hr/mo) |
|---|---|---|
| **L40 · 48 GB** | **$0.79 / GPU-hr** | from $0.60 |
| **L40G · 24 GB** | **$0.45 / GPU-hr** | from $0.32 |
| Video i2v 720p (endpoint) | from $0.025 / output-second | — |
| Video i2v 480p distilled (endpoint) | from $0.010 / output-second | — |
| Custom ComfyUI workflows | from $0.012 / megapixel | — |

Pool size: 3,600 cards shown in each tier. That answers the "do they have dozens of L40s"
question — they have thousands.

## 2. The constraint that reshapes our design: it is serverless

Stated plainly in the document: *"this is serverless capacity. Workloads run as containers
invoked over HTTPS — request/response or long-running-job shaped. **There is no SSH, no VM
layer**, and no cross-instance NVLink/InfiniBand fabric; a model must fit within a single
instance."*

Consequences, in order of how much they change our plans:

1. **The single-card sizing work was the right exercise.** No multi-GPU fabric means every
   model must fit one instance. Everything in `serving-l40.md` and
   `media-layer-resolutions.md` was computed against exactly this constraint.
2. **Diffusion workers fit this model perfectly.** LTX generation is request/response or
   long-running-job shaped, stateless between calls. This is the ideal serverless workload.
3. **The Director's vLLM server is the awkward one.** Our design leans on prefix caching —
   the shared script/lore prefix computed once and reused across all agents. That requires a
   **persistent process** holding KV cache between requests. Autoscaling is offered "within
   your configured range", so this hinges on whether the range can be pinned to a minimum of
   one always-on instance. **Ask this before committing** — if the container recycles to
   zero, the prefix-cache advantage evaporates and the fleet economics change materially.
4. **Cold start is a real cost.** The LTX component set is ~66 GiB. On serverless capacity,
   whether that is baked into a cached image or pulled per spin-up decides whether you pay
   GPU-hours watching a download. Not addressed in the document — ask.
5. **The CPU agent layer lives elsewhere.** They sell GPU instances only. Our architecture
   puts agents in CPU-only containers, which is *good* — that tier is cheap and belongs on
   ordinary infrastructure, not on $0.79/hr GPUs. But it means a second provider or our own
   box for the agent plane.
6. **No SSH means debugging happens through the container.** Ship logging, metrics and a
   health/diagnostic endpoint inside the image; there is no shell to poke around in.

Compatibility is otherwise good: *"Standard PyTorch, vLLM, ComfyUI and custom inference-server
containers deploy without modification; TensorRT engines should be compiled for Ada
(sm_89)."* Our whole stack is vLLM + ComfyUI, so nothing needs rewriting.

## 3. Option A vs Option B for our workload

Their 720p video endpoint is **$0.025 per output-second** — identical to the third-party Grok
tier, and roughly **5× more expensive** than running our own container at $0.79/hr under the
model in `economics-rental.md` § 2. That gap is their ops and margin, and it is a fair price
for not operating anything.

But the deciding factor is not price: **Option A serves WAN 2.2**, not LTX-2.5. We chose
LTX-2.5 deliberately (`media-layer-resolutions.md` § 9) for duration, resolution, native
audio and — decisively — LoRA on our own characters. Option A gives none of that.

**Option B is the fit.** It also avoids the in-path safety classifier, which the document
scopes to *generation endpoints*; for dedicated capacity they state they "do not require
visibility into the workload beyond acceptable-use screening."

Option A still has a narrow use: the **NSFW/content-safety classifier at $0.0002/image** is
cheap, battle-tested at consumer scale, and worth considering as a pre-delivery gate on
generated frames — a job we would otherwise have to build.

## 4. The L40G tier is an unexpected find

$0.45/hr for 24 GB, and — worth noting — **~1 TB/s memory bandwidth versus the L40's
~864 GB/s**. It is GeForce-class silicon, so ECC and data-centre positioning are absent, but
for bandwidth-bound work it is *faster* than the L40 at 57% of the price.

That maps onto two roles from our fleet design:

- **The router / JSON-validator agent.** A 7–9B model at FP8 needs ~8 GB. Putting it on
  L40G instead of L40 saves 43% on a card that is otherwise mostly idle capacity.
- **Any SDXL-class still-image work** — character sheets, style bibles, storyboard frames.
  Their own guidance says the 24 GB tier is "deliberately priced for high-volume image
  workloads where it is the most cost-efficient silicon available."

The 48 GB tier stays for the Director LLM (31 GB resident) and LTX (~24–35 GB peak). Neither
fits 24 GB.

## 5. A claim that contradicts our research

The document states the 48 GB tier *"runs WAN-class 14B video models natively at 720p...
configurations that do not fit consumer 24–32GB cards."*

`media-layer-resolutions.md` § 9 records community figures of **65–80 GB for Wan 2.2 14B at
720p**, and concluded it does not fit an L40. WOMBO runs Wan 2.2 at a 720p tier in production
on these exact cards, which makes their claim the more credible of the two — the community
number likely reflects an unoptimized configuration without offload or an efficient serving
stack.

This does not change our model choice — that rested on open-weight trajectory, duration,
native audio and LoRA, not solely on VRAM. But the VRAM argument in § 9 should be treated as
unreliable, and if Wan's superior motion quality (8.7 vs 7.9) ever becomes the priority, it
is more available on this hardware than we concluded.

## 6. The evaluation offer answers our biggest open question

*"We provision test keys (Option A) or deploy your container for a 48-hour evaluation
(Option B) and share measured throughput, latency and effective cost side by side with your
current provider. No commitment required."*

Every cost figure we have produced scales off an **unmeasured** estimate of ~2 minutes per
5-second clip on an L40. A free 48-hour evaluation running our own LTX-2.5 container replaces
that estimate with a measurement, at zero cost and zero commitment. This is the single
highest-value next action in the whole plan.

One real calibration point they do publish: **SDXL at 1 MP in 2.77 s/image on L40-class
instances, ~1,300 images per GPU-hour**, measured on production containers under
production-shaped load. That is an image model, not video, so it does not settle our number —
but it establishes that they publish honest, load-realistic figures rather than best-case ones.

## 7. Risks to price in

1. **No guaranteed capacity.** *"We do not sell guaranteed reservations."* For a production
   with delivery dates this is the main risk. They openly advise multi-homing: *"Partners
   routinely run us alongside an existing provider and route by price and availability."*
   The gateway in `serving-l40.md` § 4 already abstracts the endpoint, so multi-homing is a
   configuration change rather than a rewrite — build it that way from the start.
2. **Acceptable-use terms apply to all traffic**, including dedicated capacity. Dramatic film
   content is not adult content, but the boundary should be confirmed in writing before a
   project depends on it. They explicitly do not host adult-content workloads.
3. **Volume pricing needs 10K GPU-hr/month** to reach $0.60. At our modelled ~13 GPU-hours per
   short film, that tier is far out of reach — assume list price.
4. **Prefix-cache persistence** (§ 2.3) and **cold-start weight loading** (§ 2.4) are both
   unanswered by the document and both materially affect cost.

## 8. Counterparty assessment

**Who they are.** Toronto-based consumer AI company founded by Ben-Zion Benkhin, who signs
the partner document. Their 2021 lip-sync app reached 74M downloads in ten months — the
fastest-growing consumer app in Canada. Dream, their text-to-image product, was Google's
"App of the Year" in 2022; 200M+ downloads across their apps by 2024. Total funding is
roughly **$15M**: a $6M seed in 2021 and $9M in September 2024 led by Round13 Digital Asset
Fund, **with NVIDIA and CoreWeave participating**. The last of those explains how a company
this size fields thousands of GPUs.

**Why there is spare capacity.** Their own document tells the story if you read the tenses.
Every headline metric is cumulative or past: "5B+ generations served", "50M+ installs", and
critically *"historical peaks above 100K requests/hour"* — a peak that no longer occurs. A
vendor quoting lifetime totals and historical peaks rather than current run-rate is usually
telling you something.

Four structural reasons, in descending order of how benign they are:

1. **Fleets are sized for peaks.** Capacity provisioned for 100K req/hr sits idle most of the
   day; diurnal troughs alone leave large gaps in a perfectly healthy business. Selling that
   idle time is rational — it is the origin story of AWS.
2. **They shut down their biggest product.** The lip-sync app, 74M downloads, was
   discontinued in 2023 over copyright concerns. That is demand deliberately deleted.
3. **Consumer AI-art demand passed its 2022–23 peak.** Casual users were absorbed by
   Midjourney, image generation inside ChatGPT and Gemini, and phone-native tools.
4. **Hardware-vendor investors.** With NVIDIA and CoreWeave on the cap table, GPU access is
   favourable — and it is easy to end up provisioned beyond what the consumer product needs.

**The arithmetic worth raising with them.** 3,600 cards against ~$15M raised: L40 48GB runs
$7–9K each, so 3,600 units is ~$25M of hardware — more than the company has ever raised.
The fleet is therefore almost certainly not owned outright but leased, financed, or accessed
through CoreWeave. That is a chain of dependencies rather than a balance-sheet asset, and it
is a fair question to ask directly.

**What this means for us.** "We do not sell guaranteed reservations" reads differently in this
light: our capacity is residual by construction — we get what the consumer products are not
using, and a viral moment for Dream could squeeze us out mid-project. Mitigation is the
multi-homing they themselves recommend (§ 7.1), and never placing deadline-bound work solely
on residual capacity.

**In fairness, the positives are real.** 5B generations is genuine operational experience, not
a slide. Their published SDXL figure is explicitly labelled as measured "under
production-shaped load" rather than best-case — companies that massage numbers do not write
that. The pricing is genuinely competitive, motivated sellers usually are, and "direct line to
the engineering team" is credible at this company size in a way it is not at a hyperscaler.

## 9. Questions to send back

1. Can an Option B deployment be pinned to a minimum of one always-on instance, so a vLLM
   server retains its KV/prefix cache between requests?
2. How are large model weights (~66 GiB) handled across spin-ups — baked into the image,
   cached on the node, or pulled each time? Is there persistent storage, and is it billed
   separately?
3. Is billing per instance-minute inclusive of provisioning and idle time, or only compute?
4. What is egress pricing for generated video?
5. Are Option B instances on-demand only, or is there preemption?
6. Does acceptable-use screening permit dramatic/violent narrative content on dedicated
   capacity?
7. Is the fleet owned, leased, or accessed through a partner cloud — and what happens to
   partner capacity if consumer traffic spikes (§ 8)?
