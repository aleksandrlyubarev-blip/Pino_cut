# PinoCut Director — Architecture v1

Status: draft for iteration
Scope: the LLM-facing "Director" layer of PinoCut — the AI Video Director + Master Editor persona that will ultimately run on ChatGPT 5.6 (or latest) as a Custom GPT / Assistants-API agent, while this repository (`pinocut/`) remains the deterministic execution engine.

---

## 1. Two-layer design: Director + Engine

The single most important architectural decision: **PinoCut is not one program, it is two layers with a strict contract between them.**

```
┌────────────────────────────────────────────────────────────┐
│  PINOCUT DIRECTOR  (LLM runtime — Claude today, GPT later) │
│                                                            │
│  Plan-and-Execute reasoning · Soul/persona · Mentorship    │
│  Story analysis · Shot lists · Grok prompt engineering     │
│  Resolve grade design · Edit plans · Reflection/memory     │
│                                                            │
│  Speaks: natural language + structured JSON artifacts      │
└──────────────────────┬─────────────────────────────────────┘
                       │  Tool calls / artifacts (JSON, .py)
                       ▼
┌────────────────────────────────────────────────────────────┐
│  PINOCUT ENGINE  (this repo — deterministic Python)        │
│                                                            │
│  scene_stitcher.py   TimelineV1 · SceneOps snapshots       │
│  scene_tools.py      trim/concat/transitions/audio/render  │
│  render_service.py   moviepy + FFmpeg dual-mode render     │
│  bassito.py          generative job queue (extend/bridge)  │
│  stages/             ingest→analyze→…→render pipeline      │
│                                                            │
│  Plus: DaVinci Resolve Scripting API scripts the Director  │
│  generates (fusionscript / DaVinciResolveScript module)    │
└────────────────────────────────────────────────────────────┘
```

Why this split wins:

- **Transferability.** The Director layer is a system prompt + tool schemas + memory files. Nothing in it depends on Claude-specific or GPT-specific features, so it ports to ChatGPT 5.6 as-is (Custom GPT instructions + Actions, or Assistants API + function calling).
- **No slop.** Everything that must be exact — timecodes, FFmpeg filters, Resolve node settings, render presets — lives in deterministic code or in machine-checkable JSON artifacts (`TimelineV1` already has a JSON Schema in `docs/pinnocat-timeline-v1.schema.json`). The LLM plans and explains; it does not free-hand arithmetic on frames.
- **Reuse of what already works.** The engine's tool surface (`SceneToolbox`: `import_clips`, `trim_clip`, `add_transition`, `concat_clips`, `add_music`, `mix_voiceover`, `render_preview`, `export_scene`, `request_extend`, `request_restyle`, `request_bridge_shot`) is exactly the "bounded tool API" the Director calls. We add a Resolve script-generation path beside it, not instead of it.

## 2. Agent type and reasoning pattern

**Plan-and-Execute with phase gates**, not free-form ReAct.

1. **INTAKE** — clarify the idea: platform, duration, audience, emotional goal, available assets, deadline. One round of questions maximum; sensible defaults otherwise.
2. **PLAN** — produce the Production Plan artifact (story arc → shot list → asset plan → post plan). The user approves or edits. This is the phase gate: no asset generation before an approved plan.
3. **EXECUTE** — per phase (pre-production → assets → post), call tools / emit artifacts. Each artifact is standalone-usable: a Grok prompt pack, a Resolve Python script, a node-tree spec.
4. **REVIEW** — QC checklist per phase; the Director critiques its own output against the plan before showing it.
5. **REFLECT** — after delivery, write a project retro into memory (what pacing/grade/format worked) to feed the style profile.

The pattern matters for the ChatGPT port: Plan-and-Execute with explicit artifacts degrades gracefully when tools are unavailable (a Custom GPT with no Actions still emits the same artifacts as copy-paste blocks).

## 3. Memory architecture

Three tiers, all plain JSON files so they work in any runtime (repo folder today, Custom GPT knowledge files or Assistants vector store later):

| Tier | File | Contents | Written when |
|---|---|---|---|
| **User style profile** | `memory/style_profile.json` | pacing taste (avg cut length by content type), color preferences (LUTs, grade recipes that were approved), aspect-ratio defaults per platform, title/caption styling, language preference (EN/RU), recurring visual anchors | updated in REFLECT |
| **Project memory** | `projects/<slug>/project.json` | approved plan, shot list state, asset inventory + consistency anchors, edit decisions with rationale, final export settings | continuously during a project |
| **Craft library** | `memory/craft_library.json` | reusable recipes: proven Grok consistency prompt skeletons, Resolve node trees that landed, transition policies per genre | promoted from project memory when something works twice |

Rule: memory stores **decisions + rationale**, never raw media. Media lives in the project folder structure (§ project-structure.md); memory references it by relative path.

No vector DB in v1 — the profile is small and structured; retrieval is "load the JSON". Add embeddings only if the craft library exceeds what fits in context (revisit at ~50 projects).

## 4. Tool categories

Full schemas in `tools.json`. Four categories:

1. **Reasoning tools** (LLM-internal, produce structured artifacts): `analyze_story`, `create_shot_list`, `suggest_edit_plan`, `design_resolve_grade`, `plan_fairlight_audio`.
2. **Prompt-engineering tools**: `generate_grok_image_prompts` (character sheets, key frames, environments, style bible), `refine_prompt_for_consistency`.
3. **Artifact-generation tools**: `generate_resolve_python_script`, `create_project_structure`, `generate_export_preset`.
4. **Engine tools** (map 1:1 to this repo, callable when the Engine is reachable): `scene_build` (CLI `pinocut scene build`), `render_preview`, `export_scene`, `request_bridge_shot` / `request_extend` / `request_restyle` (Bassito jobs).

In the ChatGPT runtime, categories 1–3 are "always available" (the model produces the artifact directly; the tool schema exists to force structured output). Category 4 is served by the Engine HTTP API (`pinocut/director_api.py`, run with `pinocut serve`) — its `/openapi.json` is the Actions schema, with Bearer auth via `PINOCUT_API_TOKEN`. See README.md in this directory for the endpoint↔tool mapping.

## 5. Runtime recommendation for ChatGPT 5.6

Ranked:

1. **Custom GPT + Actions (recommended start).** Master system prompt goes in Instructions; `tools.json` categories 1–3 as behavioral contract inside the prompt; category 4 as Actions against a self-hosted Engine API. Knowledge files: `style_profile.json`, `craft_library.json`, the Grok consistency guide. Cheapest to stand up, easy to iterate on soul.
2. **Assistants API + function calling** when you want programmatic memory writes (the REFLECT phase updating JSON automatically) and integration with the Andrew Swarm — the assistant becomes a swarm-registered service like the Engine already is (`integrations/swarm_registry.py`).
3. Lightweight wrapper (own loop over the chat API) only if you need custom routing between PinoCut and other swarm agents in one conversation.

The master prompt is written to work in all three unchanged.

## 6. How it feels to use daily

- You drop a voice-note-grade idea: *"60-sec LinkedIn video about the new RoboQC defect-heatmap feature, slightly cinematic, I have screen recordings."*
- PinoCut asks at most 3 sharp questions, then delivers the Production Plan: arc, 9-shot list with camera language and per-shot duration, which shots are screen capture vs. Grok-generated, the exact Grok prompt pack with consistency anchors, and the post plan (edit rhythm, node tree, Fairlight chain, deliver preset).
- You approve; it emits the Resolve Python script that builds the project, bins, and timeline skeleton; you run it and start cutting into a prepared structure.
- It reviews your rough cut description against the plan ("shot 4 runs 2s long — the J-cut into shot 5 loses its breath"), teaches while correcting.
- After delivery it writes the retro; next project it already knows your LinkedIn videos land at ~2.1 s average cut length and you prefer the teal-amber split-tone at low saturation.

One precise cut at a time — but with the boring parts scripted away.
