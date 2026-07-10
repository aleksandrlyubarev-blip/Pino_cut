# PinoCut Project Structure Template

Standard folder tree for every PinoCut project. The Director's `create_project_structure` tool emits exactly this. Consistent structure is what lets Resolve import scripts, the Engine (`pinocut scene build`), and memory references all work without per-project configuration.

```
projects/<yyyy-mm>-<slug>/               # e.g. projects/2026-07-roboqc-heatmap/
├── README.md                            # from template below — the project's single source of truth
├── project.json                         # Director project memory: plan, decisions, rationale
├── 01_docs/
│   ├── production-plan.md               # approved Phase 1 artifact
│   ├── shot-list.json                   # machine-readable shot list (feeds Resolve scripts + Engine)
│   └── retro.json                       # Phase 4 reflection
├── 02_assets/
│   ├── gen/                             # Grok outputs, named <shot_id>_<take>.png (s04_t02.png)
│   │   └── _character_sheets/           # canonical reference views, generated first
│   ├── footage/                         # live footage, camera cards untouched
│   ├── screen/                          # screen recordings
│   ├── audio/
│   │   ├── vo/                          # voiceover takes
│   │   ├── music/
│   │   └── sfx/
│   └── brand/                           # logos, fonts, LUTs
├── 03_resolve/
│   ├── scripts/                         # generated Resolve Python scripts for THIS project
│   ├── exports/                         # .drp / .drt backups (project + timeline)
│   └── stills/                          # grade reference stills, powergrade exports
├── 04_renders/
│   ├── preview/                         # proxies, review cuts (v01, v02, ...)
│   └── final/                           # deliverables, named <slug>_<platform>_v<NN>.mp4
└── 05_scene_ops/                        # PinoCut Engine artifacts: TimelineV1 JSON, SceneOps snapshots
```

Rules:
- **Nothing at top level except README.md and project.json.** Numbered folders keep sort order stable everywhere (Finder, Resolve media pool, scripts).
- **Naming**: shots are `s<NN>`; generated takes `s<NN>_t<NN>`; render versions `v<NN>`. Never "final_final".
- **Camera cards untouched** in `02_assets/footage/` — Resolve links to them; trims live in the timeline, not the filesystem.
- Resolve media-pool bins mirror `02_assets/` one-to-one (the import script enforces this).
- `05_scene_ops/` is where Engine-built scenes land, so Director and Engine share one project root.

## README.md template

```markdown
# <Project Title>

**Status**: planning | assets | edit | grade | delivered
**Platform / Aspect / Duration**: <e.g. LinkedIn / 1:1 / 60 s>
**Deadline**: <date>

## One-liner
<What this video is and what the viewer should do after watching.>

## Current state
- [ ] Production plan approved
- [ ] Assets complete (gen: _/_ shots, footage: _/_, VO: _)
- [ ] Rough cut
- [ ] Fine cut approved
- [ ] Grade + audio mix
- [ ] Delivered

## Key decisions log
| Date | Decision | Why |
|------|----------|-----|

## Deliverables
| Platform | File | Delivered |
|----------|------|-----------|
```
