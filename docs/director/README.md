# PinoCut Director

The LLM-facing layer of PinoCut: the AI Video Director + Master Editor persona ("the soul") that plans productions, engineers Grok image prompts, and designs/automates DaVinci Resolve post — while this repository's Python engine (`pinocut/`) does the deterministic assembly and rendering.

Designed to be runtime-portable: developed with Claude, deployable to ChatGPT 5.6 (Custom GPT + Actions or Assistants API) unchanged.

## Contents

| File | What it is |
|---|---|
| [`architecture.md`](architecture.md) | Two-layer Director/Engine architecture, reasoning pattern, memory design, runtime recommendation |
| [`master-system-prompt.md`](master-system-prompt.md) | The complete system prompt — copy-paste ready for Custom GPT / Assistants API |
| [`tools.json`](tools.json) | 14 portable tool schemas (reasoning, prompting, artifact, engine categories) |
| [`project-structure.md`](project-structure.md) | Standard per-project folder tree + README template |
| [`grok-consistency-guide.md`](grok-consistency-guide.md) | Prompt-engineering guide for multi-shot character/style consistency in Grok |
| [`resolve-templates/`](resolve-templates/) | Runnable DaVinci Resolve Scripting API templates: project bootstrap, media import + timeline population, render queue |
| [`examples/roboqc-linkedin-60s.md`](examples/roboqc-linkedin-60s.md) | Full end-to-end example conversation (idea → plan → prompts → Resolve → retro) |

## Relationship to the Engine

Director tool category `engine` in `tools.json` maps 1:1 onto `pinocut.scene_tools.SceneToolbox` and the `pinocut scene build` CLI. To wire it up as Custom GPT Actions, a thin HTTP wrapper (FastAPI over `SceneToolbox`) is needed — tracked in the roadmap (`docs/goal-plan-pinocut-bassito-v1.md`).
