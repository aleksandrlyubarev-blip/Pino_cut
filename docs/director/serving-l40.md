# Serving the Director on a single L40 — FP8 sizing

Status: sizing plan, 2026-08-11
Goal: the Director model must fit one 48 GB L40 **with headroom**, so no H100 is needed.
FP8 is an accepted quality trade. Model selection rationale lives in
`model-benchmarks-2026-08.md`; this document is the deployment arithmetic.

---

## 1. The lever is context length, not model choice

KV cache scales linearly with `max_model_len`. Before comparing models, size the context to
the actual workload rather than to the model's advertised maximum:

| Content | Tokens |
|---|---|
| Feature screenplay, ~120 pages | ~30K |
| Universe lore + style bible | ~15K |
| Scene logs and edit decisions for a project | ~20K |
| Working headroom for reasoning traces | ~30K |
| **Total** | **~95K** |

**128K is the right `max_model_len`. 256K is paying VRAM rent for space we do not use.**
Halving the advertised context halves the cache, and that single decision matters more than
the choice between any two models below.

## 2. FP8 sizing on 48 GB, at 128K context

Weights at FP8 W8A8 (≈1 byte/param), KV cache at FP8 (`--kv-cache-dtype fp8`), derived from
the bf16 measurements in `model-benchmarks-2026-08.md` § 3:

| Model | FP8 weights | KV @128K (FP8) | Total | Free on 48 GB |
|---|---|---|---|---|
| **Gemma 4 26B A4B** | ~25 GB | ~1.3 GB | **~26 GB** | **~22 GB** |
| **Qwen3.6-27B** | ~27 GB | ~4.3 GB | **~31 GB** | **~17 GB** |
| Muse Glimmer 30B | 32.8 GB | ~1.5 GB | ~34 GB | ~14 GB |
| Gemma 4 31B | ~30.5 GB | ~5.2 GB | ~36 GB | ~12 GB |
| Qwen3.6-35B-A3B | ~35 GB | ~1 GB | ~36 GB | ~12 GB |

"Free" is not waste — vLLM turns it into additional KV pool, which is exactly what buys
concurrency. 17 GB of spare pool at ~34 MB/1K tokens holds roughly 500K additional cached
tokens, i.e. several concurrent long-context sessions.

## 3. Recommendation

**`Qwen/Qwen3.6-27B-FP8` at `--max-model-len 131072`.**

- Best quality in the class (GPQA 87.8, SWE-bench Verified 77.2, MMLU-Pro 86.2, AIME26 94.1)
- ~31 GB resident, ~17 GB spare — comfortable, not marginal
- **Already published by the vendor**, block size 128 — nothing to quantize to get started
- Hybrid Gated DeltaNet keeps the cache flat if context is raised later (only 16 of 64
  layers hold KV)

**Fallback if concurrency matters more than reasoning depth:** `Gemma 4 26B A4B`. It fits at
the *full* 262K context in ~28 GB and leaves ~19 GB, with 4B active params for speed. The
cost is real — Artificial Analysis Intelligence Index 31 vs Qwen's tier at 42 — so use it for
the parallel scene-writer role, not for the Showrunner.

**Not recommended here:** Gemma 4 31B and Qwen3.6-35B-A3B both land near 36 GB, which fits
but is not "with headroom". Muse Glimmer's FP8 build is 32.8 GB and Ada is absent from its
official vLLM recipe's tested hardware.

## 4. Launch configuration

```bash
vllm serve Qwen/Qwen3.6-27B-FP8 \
  --served-model-name director-large \
  --max-model-len 131072 \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 16 \
  --port 8000
```

Notes:
- Pin **vLLM ≥ 0.21.0** in the image regardless of model — see `model-benchmarks-2026-08.md` § 4.
- Prefix caching is on by default in the V1 engine; it is what lets the shared script/lore
  prefix be computed once across all agents. Confirm it is not disabled.
- Flag names drift between releases — check `vllm serve --help` against the pinned version
  before copying this verbatim.
- `--served-model-name` is what agents request through the gateway, so the topology stays out
  of agent code.

## 5. Validation on the spare card

Do not trust the arithmetic in § 2 — vLLM reports the truth at startup.

1. **Read the startup log.** vLLM prints the allocated KV cache size in tokens. That number,
   not this table, is the real capacity. If it is comfortably above 131072 × expected
   concurrent sessions, the sizing holds.
2. **Settle the FP8-vs-AWQ question.** Community reports say FP8 is not yet well optimized on
   L40S and that AWQ is currently faster on Ada — unquantified, and directly relevant. Serve
   both on the spare card and benchmark:
   ```bash
   vllm bench serve --model director-large --host <spare> \
     --dataset-name random --random-input-len 32000 --random-output-len 1000 \
     --max-concurrency 1    # then repeat with 8, then 32
   ```
   Concurrency 1 measures latency for the Showrunner; 8 and 32 measure the scene-writer
   regime. If AWQ wins materially, switch — FP8 was accepted for quality reasons, not speed.
3. **Push context to the wall.** Run one request at the full 131072 to confirm no OOM under
   real allocation, with concurrent sessions already resident.
4. **Quality gate on PinoCut work, not MMLU.** Shot-list generation, Grok prompt packs, and
   `TimelineV1` JSON schema validity. JSON validity rate is the sharpest signal — quantization
   damage shows up as structural drift long before prose quality degrades noticeably.
5. **Only then consider in-house calibration.** If the vendor FP8 passes the gate, ship it.
   If it drifts on structured output, re-quantize with `llm-compressor` using captured
   Director logs as the calibration set (`model-benchmarks-2026-08.md` § 4a).

## 6. What this frees

At ~31 GB resident the Director does **not** share its card with LTX-2.5 — that model needs
~34–40 GB in FP8 on its own (`media-layer` sizing, see the LTX analysis). The two live on
separate cards. The 17 GB of spare pool on the Director's card goes to concurrency, which is
the thing that actually scales the fleet.
