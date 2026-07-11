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

Director tool category `engine` in `tools.json` maps 1:1 onto `pinocut.scene_tools.SceneToolbox` and the `pinocut scene build` CLI, and is served over HTTP by `pinocut/director_api.py`:

```bash
pip install -e ".[api]"
export PINOCUT_API_TOKEN=<secret>       # optional; unset = open (local dev only)
pinocut serve --host 127.0.0.1 --port 8642
```

| Endpoint | Director tool |
|---|---|
| `POST /scenes` | `scene_build` (async: returns 202, poll `GET /scenes/{id}`) |
| `GET /scenes/{id}` | build/render status + artifact paths |
| `POST /scenes/{id}/jobs` | `request_bridge_shot` / `request_extend` / `request_restyle` |
| `POST /scenes/{id}/jobs/run` | execute the Bassito queue, rebuild the scene |
| `POST /scenes/{id}/preview` | `render_preview` |
| `POST /scenes/{id}/export` | `export_scene` |
| `GET /scenes/{id}/video` | download the rendered mp4 |

To wire it into a Custom GPT: import `http://<host>:8642/openapi.json` as the Actions schema and set Bearer auth with the `PINOCUT_API_TOKEN` value. Heavy operations never block a request — every mutating call returns `202` immediately and the LLM polls, which stays inside Action timeout limits.
