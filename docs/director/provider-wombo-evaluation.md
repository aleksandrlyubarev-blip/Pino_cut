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

### 2a. Why the missing interconnect costs us nothing

L40/L40S have no NVLink; multi-GPU work falls back to PCIe Gen 4 x16 (~64 GB/s bidirectional
aggregate) against H100's 900 GB/s NVLink. That gap is why L40 clusters are built for
distributed inference rather than foundation-model training — and it is a large part of why
this capacity costs $0.79/hr.

**Our workload is indifferent to it.** The pipeline is embarrassingly parallel throughout:
each diffusion worker renders an independent shot, each LLM replica serves independent agent
requests, and every model was deliberately sized to fit a single card. Inter-GPU bandwidth
matters for exactly two things — training foundation models from scratch, and tensor-parallel
inference of a model too large for one card — and neither is on the roadmap. Even LoRA
training, the main argument for self-hosting at all, fits comfortably on one card.

So the absence of NVLink is the arbitrage, not a limitation: anyone needing a 200B model or
from-scratch training is forced onto H100 at 5–10× the hourly cost.

Two corrections worth recording, since general L40 clustering write-ups state both features
more confidently than the vendor documentation does:

- **PCIe P2P on these cards is a hazard, not a feature.** NVIDIA's data-centre driver release
  notes document that L40, L40S, L20, L4 and RTX PRO 6000 Blackwell Server Edition rely on the
  host platform to preserve ordering of GPU-initiated posted PCIe transactions targeting a
  peer GPU. Platforms based on Intel Sapphire Rapids and later do not guarantee this, and
  GPUDirect P2P there can cause **run-time silent data corruption**. Disable it unless the
  exact platform is known-good.
- **GPUDirect RDMA on L40S works but with friction.** No DMA_BUF registration
  (`CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED` returns false), so it needs the older
  `nvidia-peermem` module — which is broken on Linux ≥ 6.8. It is validated in specific
  configurations, not plug-and-play.

**Forward risk to note.** "A model must fit within a single instance" has no escape hatch. If
a future LTX release outgrows 48 GB, or we later want a 70B Director, splitting across cards
is not available here — the response would be to change model or change provider.

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

**The arithmetic worth raising with them.** Two independent calculations reach the same place.

*Capital.* L40 48GB runs $7–9K each, so 3,600 units is ~$25M of hardware — more than the
company has ever raised (~$15M).

*Physics.* 3,600 cards is ~450 eight-GPU servers. Power, not rack units, is the binding
constraint:

```
8 × 300 W (L40 TDP)                                    = 2,400 W
2 CPUs, RAM, NICs, fans, PSU inefficiency             ≈ 1,300–1,600 W
                                                        ─────────────
per 8-GPU node                                        ≈ 3.7–4 kW
450 nodes                                             ≈ 1.7 MW IT load
with cooling at PUE 1.3–1.5                           ≈ 2.2–2.5 MW facility draw
```

Rack count follows from the power budget per rack: ~180 racks at a conventional 10 kW/rack,
~90 at 20 kW, ~45 at 40 kW (AI-ready with rear-door heat exchangers), and only ~24–38 in a
50–70 kW liquid-cooled hall. Realistic range: **40–90 racks**.

A 2.5 MW hall with dozens of racks needs facilities engineers, network staff, hardware techs
handling RMAs (at 3,600 GPUs, failures are weekly) and a 24/7 NOC — dozens of people on site
alone. **WOMBO has about ten employees, six of them co-founders.** They neither built nor
operate this. The most parsimonious reading is capacity contracted from CoreWeave — investor
and GPU cloud both — sized for the consumer peak and resold when unused.

*Two details consistent with that reading.* The document says "3,600 **shown in pool**", not
"we own 3,600" — reseller phrasing for a view into someone else's inventory. And the identical
figure appears for **both** tiers, L40 48GB and L40G 24GB; an exact coincidence in owned
inventory is implausible, while a pool ceiling or display artefact explains it naturally.

*Note what they did not need to build.* With no cross-instance fabric (§ 2a), the networking
is ordinary datacentre Ethernet — roughly 900 links across ~20–30 leaf switches plus spine.
No InfiniBand, no rail-optimised topology. That is entirely consistent with an inference farm
rather than a training cluster, and it is the cheap part of the estate.

**Consequence:** we would be a sub-tenant of a sub-tenant. Capacity depends not only on
WOMBO's own consumer load but on their contract with the underlying provider remaining in
force. "We do not sell guaranteed reservations" reads literally — you cannot guarantee what
you do not own.

**What this means for us.** "We do not sell guaranteed reservations" reads differently in this
light: our capacity is residual by construction — we get what the consumer products are not
using, and a viral moment for Dream could squeeze us out mid-project. Mitigation is the
multi-homing they themselves recommend (§ 7.1), and never placing deadline-bound work solely
on residual capacity.

**Current state of the team (checked 2026-08).**

- **Headcount ~10.** The CEO described the funding as going partly toward growing a
  "10-person workforce". Six of those are co-founders: Ben-Zion Benkhin, Angad Arneja,
  Akshat Jagga, Paul Pavel, Vivek Bhakta, Parshant Loungani.
- **Profitable**, per Benkhin: "the company is profitable, this new investment round took
  place, and new and exciting things [are] on the horizon."
- **One funding round, not two.** The $9M USD and $12.2M CAD figures are the same September
  2024 Series A reported in different currencies.
- **"Cockroach mode" since 2022** — the CEO's own term for the survival pivot after a
  collapsed financing, alongside the app shutdown and a proposed privacy class-action they
  subsequently dodged.

**The group's centre of gravity is w.ai, not WOMBO.** Benkhin's own LinkedIn headline reads
"janitor @ w.ai | creator @ WOMBO". **w.ai is a token project** — a "decentralized AI
supercomputer" paying W COIN and "w points" to people who contribute idle compute from
laptops, phones and consoles. This reframes the Series A: it was led by Round13's **Digital
Asset Fund**, and contemporaneous coverage described it as raising to "build a decentralized
supercomputer". They also partnered with io.net, a decentralized GPU network, in 2024.

So the inference offering is not merely liquidation of a declining consumer app's spare
capacity — it is plausibly a revenue and validation layer beneath a broader compute play.
That is *more* strategic than incidental, but it means partner inference competes for
attention with a token network inside a ten-person company. Note also that the partner
document explicitly excludes "crypto/compute-network workloads" — they are keeping the
enterprise offering clean of the category their sibling project occupies.

**Practical consequence:** the recommendation is unchanged — multi-home from day one, no
deadline-bound work on residual capacity — but now with better grounds. Add to § 9: who owns
the partner business internally, and what the escalation path is. The highest-signal public
check available is their job listings: whether they are hiring platform/infrastructure roles
distinguishes a real bet on inference from an experiment on idle silicon.

**In fairness, the positives are real.** 5B generations is genuine operational experience, not
a slide. Their published SDXL figure is explicitly labelled as measured "under
production-shaped load" rather than best-case — companies that massage numbers do not write
that. The pricing is genuinely competitive, motivated sellers usually are, and "direct line to
the engineering team" is credible at this company size in a way it is not at a hyperscaler.

## 9. Market position, capacity origin, and failover options

### 9.1 The price is market rate, not a discount

| Provider | Card | Price |
|---|---|---|
| **Vast.ai** | L40S 48 GB | **$0.40/hr** (spot marketplace) |
| **RunPod** | L40S 48 GB | **$0.79/hr** |
| **WOMBO** | L40 48 GB | **$0.79/hr** |
| Spheron | L40S 48 GB | ~$0.96/hr |

RunPod charges the same for the **L40S** — same 48 GB, but higher clocks, 350 W against 300 W,
and better FP8 throughput. Identical money for strictly faster silicon, self-serve, no
partner conversation required. Vast.ai is half the price, with the marketplace caveat that
spot instances are reclaimed on 15 seconds' notice with no uptime SLA.

**WOMBO has no pricing advantage.** They may win on service — direct engineering contact,
deployment help — but not on cost, which materially weakens the case for accepting sub-tenancy
risk (§ 8).

### 9.2 When the capacity was likely contracted, and why it matters

Reconstruction: L40 shipped late 2022, L40S in August 2023. WOMBO's consumer peak was
2022–23. They partnered with io.net (a decentralized GPU network) in April 2024, then raised
in September 2024 **with NVIDIA and CoreWeave participating**.

That round is textbook **circular financing** — suppliers investing in a customer who then
spends the proceeds on their chips and cloud. The same period saw NVIDIA invest $2B in
CoreWeave to become its second-largest shareholder, CoreWeave revenue go from $15,800 (2022)
to $1.9B (2024), and NVIDIA underwrite unused-capacity guarantees through 2032.

GPU contracts signed in 2023–24 ran two to three years at scarcity pricing, typically
take-or-pay. A 2024 signing expires around 2027. **The entire partner offering reads as
monetisation of an under-used commitment** — the bill arrives regardless, so recovering some
of it is rational.

**Consequence: this pricing has an expiry date we cannot see.** Question 10 in § 10 (term of
their commitment to the underlying provider) is the single most valuable answer to obtain.
A further signal, August 2026: credit investors demanded covenants after CoreWeave's spread
widened 125 basis points. If the capacity sits on CoreWeave, that is another link in the chain.

### 9.3 How far a startup scales on this pricing

One card yields ~150 seconds of generated video per hour; at a 4:1 take ratio that is
**~37 seconds of keeper footage per GPU-hour**, or ~$0.021 per delivered second.

| Monthly output | GPU-hours | Cost |
|---|---|---|
| 10 minutes finished | ~16 | **$13** |
| 1 hour | ~97 | **$77** |
| 10 hours | ~970 | **$766** |
| 103 hours (reaches the $0.60 volume tier) | 10,000 | $7,900 |

**Cost stops being the constraint long before anything else does** — roughly a 100× growth
runway from pilot scale.

The real ceiling is **concurrency**, not money. Delivering 10 finished hours in a month needs
~970 GPU-hours against 720 hours in the month: impossible on one card, requiring 2–3
continuously or bursts of twenty. That is precisely where "no guaranteed reservations" bites —
not on price, but on obtaining twenty cards on the day they are needed.

### 9.4 Failover options

Plentiful, which makes the multi-homing recommendation practical rather than theoretical:

- **Direct substitutes (per-GPU-hour):** RunPod, Vast.ai, Spheron, Hyperstack, DataCrunch,
  TensorDock, Massed Compute, Crusoe
- **Serverless containers, closest to WOMBO's model:** Modal, Replicate, Baseten, fal.ai
  (the last specialising in image and video)
- **Enterprise:** CoreWeave, Lambda, Together

Because our workload is containerised and serverless-shaped, migration is largely a
configuration change — RunPod, Modal and Replicate all accept containers on the same terms.
The gateway in `serving-l40.md` § 4 already abstracts the endpoint.

## 10. Questions to send back

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
8. Which host platform are the L40 instances on, and is GPUDirect P2P disabled? (§ 2a — on
   Sapphire Rapids and later it risks silent data corruption.)
9. Which datacentres and regions hosts the capacity?
10. Is the hardware owned, leased, or contracted from a provider — and what is the term of
    that commitment?
11. Is 3,600 owned inventory or a pool ceiling? Why is the figure identical for both the
    48 GB and 24 GB tiers?

## 11. Sources

- *WOMBO Inference — Provider Overview*, July 2026 (partner document, supplied by the user)
- [Vast.ai L40S pricing](https://vast.ai/pricing/gpu/L40S) · [RunPod vs Lambda vs CoreWeave pricing](https://www.buildmvpfast.com/blog/gpu-cloud-cost-comparison-runpod-lambda-labs-coreweave-2026) · [Cloud GPU rental guide 2026](https://www.promptquorum.com/power-local-llm/cloud-gpu-rental-guide-2026)
- [BetaKit — Round13/NVIDIA-backed Wombo raises $12.2M CAD](https://betakit.com/round13-nvidia-backed-wombo-announces-12-2-million-cad-to-launch-more-generative-ai-apps/) · [CO/AI — $9M for a decentralized supercomputer](https://getcoai.com/news/ai-startup-wombo-secures-9m-to-build-decentralized-supercomputer/)
- [w.ai](https://w.ai/) · [io.net × WOMBO partnership](https://cryptodaily.co.uk/2024/04/ionet-partners-with-ai-startup-wombo-to-enhance-computing-power-for-its-machine-learning-models)
- [Circular financing in AI infrastructure](https://io-fund.com/ai-stocks/nvidia-coreweave-nebius-circular-financing-gpu-boom) · [CoreWeave S-1 breakdown](https://www.mostlymetrics.com/p/coreweave-ipo-s1-breakdown) · [CoreWeave credit spread widening, Aug 2026](https://www.techtimes.com/articles/322772/20260803/ai-loan-investors-demand-covenants-after-coreweave-spread-blows-out-125-points.htm)
- [NVIDIA Data Center GPU Driver release notes — PCIe P2P ordering](https://docs.nvidia.com/datacenter/tesla/tesla-release-notes-570-124-06/index.html) · [Does L40S support GPUDirect? (NVIDIA forums)](https://forums.developer.nvidia.com/t/does-l40s-support-gpudirect/288105)
