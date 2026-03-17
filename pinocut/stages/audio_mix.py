"""AudioMixStage: segment-aware ducking with smooth volume ramps."""

from __future__ import annotations

from pinocut.stages.base import BaseStage
from pinocut.state import AudioMixConfig, AudioTrack, PinoCutState, TransitionType
from pinocut.utils.errors import ErrorSeverity, StageError


class AudioMixStage(BaseStage):
    """Build audio mix configuration with segment-aware ducking.

    Key improvements over v0.3:
    - Uses real speech_segments timestamps instead of boolean has_speech
    - Computes cumulative offsets accounting for crossfade overlap
    - Generates smooth volume ramp regions instead of hard switches
    """

    name = "audio_mix"

    def process(self, state: PinoCutState) -> PinoCutState:
        clips = state.get("clips", [])
        edit_decisions = state.get("edit_decisions", [])
        config = state["project_config"]
        errors: list[StageError] = list(state.get("errors", []))

        mix = AudioMixConfig()

        # ── Build timeline with cumulative offsets ──
        timeline = self._build_timeline(clips, edit_decisions)

        # ── Background music track ──
        if config.bg_music_path and config.bg_music_path.exists():
            total_duration = timeline[-1]["end"] if timeline else 0.0
            mix.tracks.append(
                AudioTrack(
                    path=str(config.bg_music_path),
                    role="music",
                    volume=config.audio_ducking.no_speech_volume,
                    loop=True,
                )
            )

            # ── Ducking regions from speech segments ──
            ducking = config.audio_ducking
            for entry in timeline:
                clip = entry["clip"]
                offset = entry["start"]

                for seg_start, seg_end in clip.speech_segments:
                    abs_start = offset + seg_start
                    abs_end = offset + seg_end

                    # Ramp down before speech
                    mix.ducking_regions.append({
                        "start": max(0, abs_start - ducking.ramp_duration),
                        "end": abs_start,
                        "volume_from": ducking.no_speech_volume,
                        "volume_to": ducking.speech_volume,
                        "type": "ramp_down",
                    })
                    # Hold during speech
                    mix.ducking_regions.append({
                        "start": abs_start,
                        "end": abs_end,
                        "volume_from": ducking.speech_volume,
                        "volume_to": ducking.speech_volume,
                        "type": "hold",
                    })
                    # Ramp up after speech
                    mix.ducking_regions.append({
                        "start": abs_end,
                        "end": abs_end + ducking.ramp_duration,
                        "volume_from": ducking.speech_volume,
                        "volume_to": ducking.no_speech_volume,
                        "type": "ramp_up",
                    })

            speech_count = sum(len(e["clip"].speech_segments) for e in timeline)
            self.log.info(
                f"Audio mix: {len(timeline)} clips, "
                f"{speech_count} speech segments, "
                f"{len(mix.ducking_regions)} ducking regions"
            )
        else:
            if config.bg_music_path:
                errors.append(
                    StageError(
                        stage=self.name,
                        message=f"Background music not found: {config.bg_music_path}",
                        severity=ErrorSeverity.WARN,
                    )
                )

        state["audio_mix"] = mix
        state["errors"] = errors
        return state

    def _build_timeline(self, clips, edit_decisions) -> list[dict]:
        """Build timeline with cumulative offsets, accounting for transition overlaps."""
        timeline: list[dict] = []
        current_offset = 0.0

        for i, clip in enumerate(clips):
            timeline.append({
                "clip": clip,
                "start": current_offset,
                "end": current_offset + clip.duration,
            })

            # Advance offset, subtracting overlap from crossfade/dip transitions
            overlap = 0.0
            if i < len(edit_decisions):
                decision = edit_decisions[i]
                if decision.transition in (
                    TransitionType.CROSSFADE,
                    TransitionType.DIP_TO_BLACK,
                ):
                    overlap = decision.duration

            current_offset += clip.duration - overlap

        return timeline
