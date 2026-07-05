# PinoCut Agent v1.0

**Modular AI video assembly agent for the Andrew Swarm ecosystem.**

PinoCut takes raw video clips and produces a professional edit with intelligent transitions, animated lower thirds, LUT color grading, segment-aware audio ducking, and cinematic effects.

## Architecture

6-stage modular pipeline orchestrated by LangGraph:

```
Romeo Flex Vision API
        │
        ▼
[Ingest] → [Analyze] → [Transitions] → [Titles] → [Audio Mix] → [Render]
     └──────────────────────────────────────────────────────────────┘
                         LangGraph StateGraph
                         Moltis Runtime (channels, memory, scheduling)
                         E2B Sandbox (FFmpeg, OpenCV)
```

| Stage | What it does |
|-------|-------------|
| **IngestStage** | Load, validate, probe clips with real durations |
| **AnalyzeStage** | Parallel speech detection (Whisper/VAD), keyframe histograms, Romeo FV enrichment |
| **TransitionStage** | Smart transitions: hard cut, crossfade, dip-to-black, match cut |
| **TitleStage** | Animated lower thirds, auto-titles from metadata, LUT color grade selection |
| **AudioMixStage** | Segment-aware ducking with 200ms smooth ramps |
| **RenderStage** | Dual-mode: moviepy (compatible) or FFmpeg subprocess (fast) |

## Andrew Swarm Integration

- **ROMA** — Registered agent with weighted routing (`0.85` for video tasks)
- **LangGraph** — StateGraph with checkpoints and conditional routing
- **Moltis** — Parallel analysis via channels, persistent memory, task scheduling
- **E2B Sandbox** — Isolated FFmpeg/OpenCV with security validation and audit log
- **Romeo Flex Vision** — Semantic metadata: scenes, locations, mood, subjects, quality
- **Romeo PhD** — Pipeline performance metrics and reporting

## Quick Start

### Install

```bash
pip install -e .                # Core
pip install -e ".[whisper]"     # + Whisper speech detection
pip install -e ".[full]"        # + E2B + LiteLLM
pip install -e ".[dev]"         # + pytest + ruff
```

### CLI

```bash
# Basic
pinocut ./raw_footage -o output/final.mp4

# Cinema quality + background music + FFmpeg render
pinocut ./raw_footage -o out.mp4 -m music.mp3 --preset cinema --render-mode ffmpeg

# Color grading + parallel analysis + metrics
pinocut ./raw_footage --color-grade cinematic --metrics --metrics-path metrics.json

# E2B sandbox + persist cache + register in Swarm
pinocut ./raw_footage --sandbox e2b --memory-persist --register-swarm
```

### Python API

```python
from pathlib import Path
from pinocut import PinoCutAgent
from pinocut.config import ProjectConfig, RenderPreset, ColorGradeStyle

agent = PinoCutAgent()
result = agent.run(ProjectConfig(
    input_folder=Path("./footage"),
    output_path=Path("./output/final.mp4"),
    bg_music_path=Path("./music.mp3"),
    render_preset=RenderPreset.CINEMA,
    color_grade=ColorGradeStyle.CINEMATIC,
    render_mode="ffmpeg",
    parallel=True,
    metrics_enabled=True,
))

print(agent.metrics_report())
```

## Visual Effects (Phase 2)

### LUT Color Grading
Applies per-frame color transformation across the entire output:
- **warm** — boosted reds/oranges, reduced blues
- **cold** — boosted blues, desaturated reds
- **vintage** — faded blacks, warm highlights, reduced saturation
- **cinematic** — teal shadows, orange highlights, contrast boost

### Animated Lower Thirds
OpenCV-rendered title cards with:
- Slide-up animation with ease-out cubic
- Semi-transparent background bar with accent stripe
- Configurable fade-in/fade-out opacity
- Auto-generated from Romeo FV scene descriptions

### Ken Burns Effect
Slow zoom/pan on static clips with configurable start/end zoom and pan direction.

## Project Structure

```
pinocut/
├── __init__.py
├── agent.py                 # LangGraph graph + PinoCutAgent
├── state.py                 # Data models: ClipSegment, PinoCutState, ...
├── config.py                # ProjectConfig, presets, constants
├── cli.py                   # CLI entry point
├── effects.py               # LUT grading, Ken Burns, lower thirds
├── stages/
│   ├── base.py              # BaseStage ABC
│   ├── ingest.py            # IngestStage
│   ├── analyze.py           # AnalyzeStage (parallel, Moltis, Romeo FV)
│   ├── transitions.py       # TransitionStage
│   ├── titles.py            # TitleStage
│   ├── audio_mix.py         # AudioMixStage (segment-aware ducking)
│   └── render.py            # RenderStage (moviepy + FFmpeg dual mode)
├── integrations/
│   ├── romeo_vision.py      # Romeo FV API (HTTP, retry, cache)
│   ├── moltis_bridge.py     # Moltis runtime (channels, memory, scheduler)
│   ├── swarm_registry.py    # ROMA agent registration (HTTP, heartbeat)
│   ├── e2b_sandbox.py       # E2B sandbox executor (security, audit)
│   └── romeo_phd.py         # Pipeline metrics collector
├── utils/
│   ├── errors.py            # Result[T, E], StageError
│   └── logging.py           # Structured JSON logger
└── tests/
    ├── test_stages.py        # Stage + effects unit tests
    └── test_integration.py   # Ecosystem integration tests
```

## Tests

```bash
pytest tests/ -v
```

## Pinnocat Scene Stitcher v1

The repository now includes a scene-level spec for the first Pinnocat editing mode:

- [docs/pinnocat-scene-stitcher-v1.md](docs/pinnocat-scene-stitcher-v1.md)
- [docs/pinnocat-tool-api-v1.md](docs/pinnocat-tool-api-v1.md)
- [docs/pinnocat-timeline-v1.schema.json](docs/pinnocat-timeline-v1.schema.json)
- [docs/pinnocat-timeline-v1.example.json](docs/pinnocat-timeline-v1.example.json)
- [docs/pinnocat-scene-ops-v1.example.json](docs/pinnocat-scene-ops-v1.example.json)

This spec narrows v1 to a Scene Stitcher Agent where Romeo orchestrates scene assembly, Andrew scores technical quality, and editing runs through bounded tool calls.

### Scene CLI skeleton

```bash
pinocut scene build ./raw_footage \
  --goal "arrival at abandoned spaceport" \
  --scene-id scene_03 \
  --template cinematic_montage \
  --duration 35 \
  --style "cinematic dark sci-fi" \
  --output-dir ./output
```

The scene build path renders real media via FFmpeg alongside the JSON artifacts:

```text
output/<scene_id>.mp4              # final scene render (trim + transitions + audio mix)
output/<scene_id>.preview.mp4      # fast low-res proxy for review
output/<scene_id>.timeline.json    # timeline manifest
output/<scene_id>.scene-ops.json   # frontend-ready SceneOps snapshot
```

Transitions from the timeline (hard cuts, crossfade, dip-to-black) are applied with FFmpeg `concat`/`xfade`, music and voiceover tracks are mixed with gain and ducking, and clips are conformed to the export profile (resolution, fps). When FFmpeg or source media is unavailable, the build degrades gracefully to JSON-only export with a warning.

### Bassito regeneration loop

When the assembled scene falls short (clips too brief, technical flaws, duration deficit), the build queues **Bassito jobs** — proposed changes that are not applied until explicitly executed:

```bash
pinocut scene build ./raw_footage --goal "..." --duration 30 --run-bassito
```

- `extend` — pads a too-short take to its timeline target (replaces the source clip)
- `restyle` — stabilizes and re-grades a flawed take (replaces the source clip)
- `bridge_shot` — generates a new atmospheric shot from the anchor clip's closing frame (joins the pool as a new clip)

Jobs run through a pluggable `GenerativeBackend` interface (`pinocut/bassito.py`). The shipped `FfmpegEffectsBackend` executes locally at zero cost; cloud generative providers implement the same interface. After a round, artifacts re-enter the clip pool, the timeline version is bumped, and the whole scene is rebuilt — so every generated clip passes Andrew's quality gate like any other take, and both timeline versions are persisted for review.

### Dispatch SceneOps snapshots to RomeoFlexVision

`PinoCut` can publish a frontend-ready SceneOps snapshot into the
`RomeoFlexVision` GitHub Pages workflow without introducing a separate backend
host.

The repository also keeps a canonical public example snapshot at:

```text
docs/pinnocat-scene-ops-v1.example.json
```

`RomeoFlexVision` can sync from that file directly over GitHub raw URLs, which
keeps the frontend/backend contract alive even before a dedicated SceneOps API
host is deployed.

Required secret in the calling repository:

```bash
ROMEOFLEXVISION_DISPATCH_TOKEN
```

Example local dry run:

```bash
python scripts/dispatch_scene_ops.py \
  --snapshot output/scene_03.scene-ops.json \
  --dry-run
```

Example GitHub Actions step:

```yaml
- name: Dispatch SceneOps snapshot
  env:
    ROMEOFLEXVISION_DISPATCH_TOKEN: ${{ secrets.ROMEOFLEXVISION_DISPATCH_TOKEN }}
  run: |
    python scripts/dispatch_scene_ops.py \
      --snapshot output/scene_03.scene-ops.json
```

Reusable workflow in this repository:

```yaml
jobs:
  publish-scene-ops:
    uses: ./.github/workflows/publish-scene-ops.yml
    secrets:
      romeoflexvision_dispatch_token: ${{ secrets.ROMEOFLEXVISION_DISPATCH_TOKEN }}
    with:
      snapshot_path: output/scene_03.scene-ops.json
```

If the snapshot is already published elsewhere, the same helper can dispatch a
public URL instead:

```bash
python scripts/dispatch_scene_ops.py \
  --source-url https://example.com/scene-ops.json \
  --dry-run
```

## License

MIT
