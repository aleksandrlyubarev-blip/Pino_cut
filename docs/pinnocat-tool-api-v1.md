# Pinnocat Tool API v1

This document defines the minimal tool layer for scene-level assembly.

## Design rules

- Romeo chooses actions, but does not edit media manually.
- Andrew returns scores, notes, risk flags, and recommended actions.
- Bassito handles visual generation or transformation requests.
- Deterministic operations should stay deterministic.
- All tool calls should be serializable and replayable.

## Action to tool mapping

| Action | Primary tools | Output |
|---|---|---|
| `LOAD_SCENE` | `import_clips`, `extract_metadata` | scene state with loaded clip registry |
| `ANALYZE_CLIPS` | `extract_metadata`, Andrew scoring pipeline | clip analysis bundle |
| `SCORE_CLIPS` | Andrew rubric evaluator | scorecard for all candidate clips |
| `SELECT_TAKES` | Romeo selection logic | approved and rejected clip lists |
| `TRIM_SEQUENCE` | `trim_clip`, `reorder_timeline`, `insert_clip` | updated timeline segments |
| `BUILD_ROUGH_CUT` | `concat_clips`, `trim_clip` | rough cut timeline |
| `ADD_TRANSITIONS` | `add_transition` | transition list |
| `ADD_AUDIO` | `add_music`, `mix_voiceover`, `normalize_audio` | audio mix plan |
| `ADD_SUBTITLES` | `generate_subtitles`, `burn_subtitles` | subtitle track metadata |
| `REQUEST_BRIDGE_SHOT` | `request_bridge_shot` | bridge-shot job request |
| `REQUEST_REGENERATION` | `request_extend`, `request_restyle` | regeneration job request |
| `EXPORT_SCENE` | `render_preview`, `export_scene` | preview path and final export path |
| `SAVE_VERSION` | `save_timeline_version` | persisted version marker |

## Tool signatures

### Ingest tools

| Function | Inputs | Returns |
|---|---|---|
| `import_clips(paths)` | `list[str]` | `list[clip_id]` |
| `extract_metadata(clip_id)` | `clip_id` | duration, fps, resolution, audio, codec |
| `normalize_media(clip_id, resolution, fps, audio_rate)` | clip target profile | normalized clip reference |

### Editing tools

| Function | Inputs | Returns |
|---|---|---|
| `trim_clip(clip_id, start_sec, end_sec)` | clip trim window | trimmed clip id |
| `concat_clips(clip_ids, transition_type)` | ordered clips, policy | rough cut id |
| `add_transition(left_clip, right_clip, type, duration_sec)` | two clips and transition config | transition record |
| `reorder_timeline(clip_ids)` | ordered clip ids | updated timeline |
| `insert_clip(position, clip_id)` | position and clip | updated timeline |

### Audio tools

| Function | Inputs | Returns |
|---|---|---|
| `add_music(track_id, gain_db)` | music asset and gain | music layer |
| `mix_voiceover(track_id, duck_music=true)` | voiceover asset and ducking policy | voice mix layer |
| `normalize_audio(target_lufs)` | target loudness | normalized scene audio |

### Subtitle tools

| Function | Inputs | Returns |
|---|---|---|
| `generate_subtitles(language)` | language code | subtitle file reference |
| `burn_subtitles(style_preset)` | style preset | rendered subtitle layer |

### Output tools

| Function | Inputs | Returns |
|---|---|---|
| `render_preview()` | current timeline | preview asset |
| `export_scene(profile)` | export profile | final file path |
| `save_timeline_version(comment)` | version note | timeline version id |

### Optional generation tools

| Function | Inputs | Returns |
|---|---|---|
| `request_extend(clip_id, prompt)` | source clip and extension prompt | async job id |
| `request_restyle(clip_id, prompt)` | source clip and style prompt | async job id |
| `request_bridge_shot(prompt)` | textual shot request | async job id |

## Standard command payload

```json
{
  "action": "ADD_AUDIO",
  "scene_id": "scene_03",
  "inputs": {
    "music_track_id": "music_ambient_01",
    "voiceover_track_id": null,
    "duck_music": false,
    "target_lufs": -14
  },
  "constraints": {
    "style_profile": "cinematic dark sci-fi",
    "audio_policy": "music_only"
  }
}
```

## Expected response envelope

```json
{
  "action": "ADD_AUDIO",
  "scene_id": "scene_03",
  "status": "ok",
  "outputs": {
    "audio_mix_id": "mix_v1",
    "applied_tracks": ["music_ambient_01"]
  },
  "warnings": [],
  "artifacts": []
}
```

## Failure model

Each tool response should be able to return:
- `status`: `ok`, `warning`, `error`
- `warnings`: recoverable issues
- `errors`: blocking issues
- `artifacts`: output references, if any

## Notes for implementation

- Scene operations should be idempotent where possible.
- All timeline-changing tools should return structured state deltas.
- Optional generation tools should remain asynchronous and cost-aware.
- Andrew scoring should be stored before Romeo selects final takes.
