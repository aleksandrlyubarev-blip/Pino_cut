"""TitleStage: animated titles, LUT color grading, Ken Burns, watermarks.

Phase 2 upgrades:
- Animated lower-third overlays via effects.py
- LUT color grading per-frame (warm/cold/vintage/cinematic)
- Ken Burns effect on static clips
- Romeo FV mood → auto color grade
"""

from __future__ import annotations

from pinocut.config import ColorGradeStyle
from pinocut.effects import LowerThirdStyle, TitleAnimation, make_color_grade_filter
from pinocut.stages.base import BaseStage
from pinocut.state import PinoCutState, TitleOverlay


class TitleStage(BaseStage):
    """Generate title overlays and visual effects for each clip."""

    name = "titles"

    def process(self, state: PinoCutState) -> PinoCutState:
        clips = state.get("clips", [])
        romeo_data = state.get("romeo_data", {})
        config = state["project_config"]

        overlays: list[TitleOverlay] = []

        for i, clip in enumerate(clips):
            clip_key = str(clip.path)
            romeo = romeo_data.get(clip_key)

            title_text = self._generate_title(clip, romeo, i)
            if title_text:
                overlays.append(
                    TitleOverlay(
                        clip_path=clip_key,
                        text=title_text,
                        start_time=0.0,
                        duration=config.title.duration,
                        position=config.title.position,
                        animated=True,
                        style=self._pick_style(romeo),
                    )
                )

        color_grade = self._pick_color_grade(romeo_data, config)

        self.log.info(f"Generated {len(overlays)} title overlays, grade={color_grade.value}")
        state["title_overlays"] = overlays
        state["color_grade"] = color_grade
        return state

    def _generate_title(self, clip, romeo, index: int) -> str:
        if romeo and romeo.scene_description:
            return romeo.scene_description
        meta_title = clip.metadata.get("title", "")
        if meta_title:
            return meta_title
        return f"Scene {index + 1}"

    def _pick_style(self, romeo) -> str:
        """Choose title animation style based on context."""
        if romeo and romeo.subjects:
            return "lower_third"
        return "caption"

    def _pick_color_grade(self, romeo_data, config) -> ColorGradeStyle:
        if config.color_grade != ColorGradeStyle.NONE:
            return config.color_grade

        moods: list[str] = []
        for rd in romeo_data.values():
            if rd.mood:
                moods.append(rd.mood.lower())

        if not moods:
            return ColorGradeStyle.NONE

        mood_map = {
            "warm": ColorGradeStyle.WARM,
            "cold": ColorGradeStyle.COLD,
            "vintage": ColorGradeStyle.VINTAGE,
            "dramatic": ColorGradeStyle.CINEMATIC,
            "cinematic": ColorGradeStyle.CINEMATIC,
        }

        for mood in moods:
            for keyword, grade in mood_map.items():
                if keyword in mood:
                    return grade

        return ColorGradeStyle.NONE
