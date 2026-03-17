"""TransitionStage: intelligent transition selection between clips."""

from __future__ import annotations

import numpy as np

from pinocut.stages.base import BaseStage
from pinocut.state import (
    EditDecision,
    PinoCutState,
    TransitionType,
)


class TransitionStage(BaseStage):
    """Decide transition type at each clip junction.

    Uses histogram correlation (from AnalyzeStage) and Romeo FV metadata
    to pick the optimal transition.
    """

    name = "transitions"

    def process(self, state: PinoCutState) -> PinoCutState:
        clips = state.get("clips", [])
        analysis = state.get("analysis", {})
        romeo_data = state.get("romeo_data", {})
        config = state["project_config"]
        threshold = config.transitions.histogram_threshold

        decisions: list[EditDecision] = []

        for i in range(1, len(clips)):
            prev_key = str(clips[i - 1].path)
            curr_key = str(clips[i].path)

            prev_analysis = analysis.get(prev_key)
            curr_analysis = analysis.get(curr_key)
            prev_romeo = romeo_data.get(prev_key)
            curr_romeo = romeo_data.get(curr_key)

            decision = self._decide(
                prev_key, curr_key,
                prev_analysis, curr_analysis,
                prev_romeo, curr_romeo,
                threshold, config,
            )
            decisions.append(decision)

        self.log.info(f"Made {len(decisions)} transition decisions")
        state["edit_decisions"] = decisions
        return state

    def _decide(
        self,
        prev_key, curr_key,
        prev_analysis, curr_analysis,
        prev_romeo, curr_romeo,
        threshold, config,
    ) -> EditDecision:
        """Pick transition type for a single junction."""

        # Priority 1: Location change → dip to black
        if prev_romeo and curr_romeo:
            if (
                prev_romeo.location_tag
                and curr_romeo.location_tag
                and prev_romeo.location_tag != curr_romeo.location_tag
            ):
                return EditDecision(
                    from_clip=prev_key,
                    to_clip=curr_key,
                    transition=TransitionType.DIP_TO_BLACK,
                    duration=config.transitions.dip_to_black_duration,
                    reason=f"Location change: {prev_romeo.location_tag} → {curr_romeo.location_tag}",
                )

        # Priority 2: Histogram correlation
        correlation = self._histogram_correlation(prev_analysis, curr_analysis)

        if correlation is not None and correlation < threshold:
            return EditDecision(
                from_clip=prev_key,
                to_clip=curr_key,
                transition=TransitionType.CROSSFADE,
                duration=config.transitions.crossfade_duration,
                reason=f"Scene change (correlation={correlation:.3f})",
            )

        # Default: hard cut (professional style)
        return EditDecision(
            from_clip=prev_key,
            to_clip=curr_key,
            transition=TransitionType.HARD_CUT,
            duration=0.0,
            reason=f"Similar scenes (correlation={correlation:.3f})" if correlation else "Default",
        )

    def _histogram_correlation(self, prev_analysis, curr_analysis) -> float | None:
        """Compute histogram correlation between last frame of prev and first frame of curr."""
        if not prev_analysis or not curr_analysis:
            return None
        if not prev_analysis.last_frame_hist or not curr_analysis.first_frame_hist:
            return None

        h1 = np.array(prev_analysis.last_frame_hist, dtype=np.float32)
        h2 = np.array(curr_analysis.first_frame_hist, dtype=np.float32)

        # Pearson correlation
        if h1.std() == 0 or h2.std() == 0:
            return 0.0

        correlation = float(np.corrcoef(h1, h2)[0, 1])
        return correlation
