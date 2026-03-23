# Pinnocat Scene Stitcher Agent v1

## Purpose

Pinnocat Scene Stitcher v1 assembles a single scene from a set of short clips.

It is a scene-level orchestration layer, not a full autonomous film editor.

## What v1 does

- accepts clips and a scene goal
- analyzes source material
- scores clip usefulness
- selects the best takes
- trims and orders clips
- adds simple transitions
- applies music and optional voiceover
- renders a rough cut
- exports scene output plus timeline data

## What v1 does not do

- full feature-length autonomous editing
- advanced screenplay-level dramaturgy
- heavy VFX workflows
- advanced color finishing
- deep lip-sync postproduction

## Internal roles

### Romeo PhD

Scene orchestrator.

Responsibilities:
- decide the scene goal
- choose the assembly template
- select clips for the scene
- request transitions or regeneration when needed
- decide when the scene is ready

### Bassito Animator

Visual execution layer.

Responsibilities:
- prepare clips
- create visual variations
- reframe footage
- extend or restyle selected clips
- prepare bridge assets for assembly

### Andrew Analitic

Quality and continuity layer.

Responsibilities:
- evaluate clip quality
- check continuity fit
- review pacing and technical stability
- estimate regeneration cost and reuse value

## User flow

### Input

The user provides:
- scene goal
- style
- duration
- clip set
- editing mode

Example:

> Build a 35-second scene from 8 clips.  
> Tone: dark sci-fi.  
> Pace: medium.  
> Ending should feel tense.

### Output

The system returns:
- scene rough cut
- timeline JSON
- list of used clips
- list of rejected clips
- short Romeo review
- Andrew quality review

## Scene state

```json
{
  "project_id": "rfv_pinnocat_001",
  "scene_id": "scene_03",
  "scene_goal": "arrival at abandoned spaceport",
  "style_profile": "cinematic dark sci-fi",
  "target_duration_sec": 35,
  "editing_mode": "hybrid",
  "available_clips": [],
  "approved_clips": [],
  "rejected_clips": [],
  "clip_scores": {},
  "timeline_version": 1,
  "music_track": null,
  "voiceover_track": null,
  "subtitle_track": null,
  "export_profile": {
    "resolution": "1920x1080",
    "fps": 24,
    "format": "mp4"
  }
}
```

## Action vocabulary

Romeo must operate via a bounded set of actions:

```text
LOAD_SCENE
ANALYZE_CLIPS
SCORE_CLIPS
SELECT_TAKES
TRIM_SEQUENCE
BUILD_ROUGH_CUT
ADD_TRANSITIONS
ADD_AUDIO
ADD_SUBTITLES
REQUEST_BRIDGE_SHOT
REQUEST_REGENERATION
EXPORT_SCENE
SAVE_VERSION
```

## Command envelope

```json
{
  "action": "BUILD_ROUGH_CUT",
  "scene_id": "scene_03",
  "inputs": {
    "clip_ids": ["c01", "c04", "c02", "c07"],
    "target_duration_sec": 35,
    "editing_template": "cinematic_montage",
    "transition_policy": "minimal",
    "audio_policy": "music_only"
  },
  "constraints": {
    "style_profile": "cinematic dark sci-fi",
    "fps": 24,
    "resolution": "1920x1080"
  }
}
```

## Assembly templates

### dialogue_scene

For conversational scenes:
- establishing
- speaker A
- speaker B
- reaction
- insert or pause
- exit beat

### cinematic_montage

For atmosphere and mood:
- wide opener
- medium movement
- detail shot
- emotional beat
- tension rise
- closing image

### trailer_cut

For short-impact teasers:
- strongest opener
- fast alternation
- rhythm acceleration
- climax shot
- end tag

## Andrew rubric

Each clip is scored from 1 to 5 on:
- visual_quality
- continuity_fit
- prompt_match
- motion_stability
- timeline_usefulness

A clip must not enter the rough cut if:
- `visual_quality < 3`, or
- `timeline_usefulness < 3`

## Romeo rubric

Romeo evaluates artistic function:
- cinematic_value
- emotional_relevance
- scene_fit
- pacing_value
- final_image_strength

## Rough cut algorithm

1. Ingest: load clips, read metadata, normalize media.
2. Analyze: Andrew scores technical and editorial fitness.
3. Creative plan: Romeo selects the right scene template.
4. Selection: choose 4 to 8 clips and trim excess.
5. Assembly: order clips and add basic transitions.
6. Audio: add music and optional voiceover.
7. Export: render preview, export final mp4, save timeline.

## MVP success criteria

v1 is successful if it can:
- take 5 to 10 clips
- choose the strongest takes
- assemble a 20 to 60 second scene
- add basic transitions
- apply music
- export a usable rough cut without manual editor work

## Roadmap

### Sprint 1

- import
- metadata
- normalize
- trim
- concat
- export

### Sprint 2

- Andrew scoring
- template assembly
- timeline JSON
- version save

### Sprint 3

- Romeo orchestration
- structured commands
- music layer
- subtitle layer

### Sprint 4

- voice commands
- Bassito bridge shots
- regeneration loop
- scene review dashboard

## Short formula

Pinnocat v1 is a Scene Stitcher Agent where Romeo drives scene assembly, Andrew evaluates technical quality, and editing is executed through bounded video tools.
