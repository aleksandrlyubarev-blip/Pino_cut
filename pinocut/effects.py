"""Visual effects: LUT color grading, Ken Burns, animated title generators.

Phase 2 implementation of cinematic effects pipeline.
All effects operate on numpy frames for maximum compatibility
with both moviepy and FFmpeg render paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

import numpy as np


# ══════════════════════════════════════════════
#  LUT COLOR GRADING
# ══════════════════════════════════════════════

# Pre-computed 3D LUT curves per style.
# Each LUT is a function (frame: ndarray[H,W,3]) → ndarray[H,W,3]

def _warm_lut(frame: np.ndarray) -> np.ndarray:
    """Warm tones: boost reds/oranges, reduce blues."""
    result = frame.astype(np.float32)
    # Red channel: gentle S-curve lift
    result[:, :, 0] = np.clip(result[:, :, 0] * 1.08 + 5, 0, 255)
    # Green channel: slight warmth
    result[:, :, 1] = np.clip(result[:, :, 1] * 1.02 + 2, 0, 255)
    # Blue channel: reduce
    result[:, :, 2] = np.clip(result[:, :, 2] * 0.88 - 3, 0, 255)
    return result.astype(np.uint8)


def _cold_lut(frame: np.ndarray) -> np.ndarray:
    """Cold/blue tones: boost blues, desaturate reds."""
    result = frame.astype(np.float32)
    result[:, :, 0] = np.clip(result[:, :, 0] * 0.90 - 5, 0, 255)
    result[:, :, 1] = np.clip(result[:, :, 1] * 0.97 + 3, 0, 255)
    result[:, :, 2] = np.clip(result[:, :, 2] * 1.12 + 8, 0, 255)
    return result.astype(np.uint8)


def _vintage_lut(frame: np.ndarray) -> np.ndarray:
    """Vintage film: faded blacks, warm highlights, reduced saturation."""
    result = frame.astype(np.float32)
    # Fade blacks (lift shadows)
    result = result * 0.85 + 20
    # Warm shift
    result[:, :, 0] = np.clip(result[:, :, 0] * 1.05 + 8, 0, 255)
    result[:, :, 1] = np.clip(result[:, :, 1] * 0.98, 0, 255)
    result[:, :, 2] = np.clip(result[:, :, 2] * 0.88 - 5, 0, 255)
    # Desaturate slightly
    gray = np.mean(result, axis=2, keepdims=True)
    result = result * 0.75 + gray * 0.25
    return np.clip(result, 0, 255).astype(np.uint8)


def _cinematic_lut(frame: np.ndarray) -> np.ndarray:
    """Cinematic teal-orange look: orange highlights, teal shadows."""
    result = frame.astype(np.float32)
    luminance = 0.299 * result[:, :, 0] + 0.587 * result[:, :, 1] + 0.114 * result[:, :, 2]

    # Shadow mask (dark areas) and highlight mask (bright areas)
    shadow_mask = np.clip(1.0 - luminance / 128.0, 0, 1)[:, :, np.newaxis]
    highlight_mask = np.clip((luminance - 128.0) / 128.0, 0, 1)[:, :, np.newaxis]

    # Teal in shadows (reduce red, boost blue-green)
    result[:, :, 0] = result[:, :, 0] - shadow_mask[:, :, 0] * 15
    result[:, :, 1] = result[:, :, 1] + shadow_mask[:, :, 0] * 5
    result[:, :, 2] = result[:, :, 2] + shadow_mask[:, :, 0] * 18

    # Orange in highlights (boost red, slight green, reduce blue)
    result[:, :, 0] = result[:, :, 0] + highlight_mask[:, :, 0] * 12
    result[:, :, 1] = result[:, :, 1] + highlight_mask[:, :, 0] * 4
    result[:, :, 2] = result[:, :, 2] - highlight_mask[:, :, 0] * 10

    # Contrast boost
    result = (result - 128) * 1.08 + 128

    return np.clip(result, 0, 255).astype(np.uint8)


LUT_FUNCTIONS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "warm": _warm_lut,
    "cold": _cold_lut,
    "vintage": _vintage_lut,
    "cinematic": _cinematic_lut,
}


def apply_color_grade(frame: np.ndarray, style: str) -> np.ndarray:
    """Apply color grading LUT to a frame.

    Args:
        frame: RGB uint8 frame (H, W, 3)
        style: LUT style name from ColorGradeStyle

    Returns:
        Color-graded frame (H, W, 3)
    """
    fn = LUT_FUNCTIONS.get(style)
    if fn is None:
        return frame
    return fn(frame)


def make_color_grade_filter(style: str) -> Callable[[np.ndarray], np.ndarray] | None:
    """Get a moviepy-compatible frame filter function.

    Usage with moviepy:
        clip = clip.fl_image(make_color_grade_filter("cinematic"))
    """
    fn = LUT_FUNCTIONS.get(style)
    return fn


# ══════════════════════════════════════════════
#  KEN BURNS EFFECT
# ══════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class KenBurnsConfig:
    """Ken Burns slow zoom/pan parameters."""

    zoom_start: float = 1.0  # start zoom factor
    zoom_end: float = 1.15  # end zoom factor
    pan_x: float = 0.0  # horizontal pan (-1 to 1, 0 = center)
    pan_y: float = 0.0  # vertical pan (-1 to 1, 0 = center)


def ken_burns_frame(
    frame: np.ndarray,
    t: float,
    duration: float,
    config: KenBurnsConfig | None = None,
) -> np.ndarray:
    """Apply Ken Burns zoom/pan to a single frame at time t.

    Args:
        frame: Source frame (H, W, 3)
        t: Current time in seconds
        duration: Total clip duration
        config: Zoom and pan parameters

    Returns:
        Cropped/zoomed frame at same resolution
    """
    import cv2

    cfg = config or KenBurnsConfig()
    h, w = frame.shape[:2]

    progress = min(t / max(duration, 0.001), 1.0)
    zoom = cfg.zoom_start + (cfg.zoom_end - cfg.zoom_start) * progress

    # Compute crop region
    crop_w = int(w / zoom)
    crop_h = int(h / zoom)

    # Pan offset
    center_x = w // 2 + int(cfg.pan_x * (w - crop_w) / 2 * progress)
    center_y = h // 2 + int(cfg.pan_y * (h - crop_h) / 2 * progress)

    x1 = max(0, center_x - crop_w // 2)
    y1 = max(0, center_y - crop_h // 2)
    x2 = min(w, x1 + crop_w)
    y2 = min(h, y1 + crop_h)

    cropped = frame[y1:y2, x1:x2]
    return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LANCZOS4)


# ══════════════════════════════════════════════
#  ANIMATED TITLE GENERATORS
# ══════════════════════════════════════════════

class TitleAnimation(str, Enum):
    """Available title animation styles."""

    FADE_IN = "fade_in"
    SLIDE_UP = "slide_up"
    SLIDE_LEFT = "slide_left"
    TYPEWRITER = "typewriter"
    SCALE_IN = "scale_in"


def animated_position(
    t: float,
    duration: float,
    animation: TitleAnimation,
    target_x: int,
    target_y: int,
    clip_h: int,
    clip_w: int,
) -> tuple[int, int]:
    """Compute animated position for a title at time t.

    Args:
        t: Current time in seconds
        duration: Animation duration (typically 0.5-1.0s)
        animation: Animation style
        target_x, target_y: Final resting position
        clip_h, clip_w: Video dimensions

    Returns:
        (x, y) position at time t
    """
    progress = min(t / max(duration, 0.001), 1.0)
    # Ease-out cubic
    ease = 1.0 - (1.0 - progress) ** 3

    if animation == TitleAnimation.SLIDE_UP:
        start_y = clip_h + 20  # start below frame
        return target_x, int(start_y + (target_y - start_y) * ease)

    elif animation == TitleAnimation.SLIDE_LEFT:
        start_x = clip_w + 20  # start right of frame
        return int(start_x + (target_x - start_x) * ease), target_y

    elif animation == TitleAnimation.SCALE_IN:
        # Position stays the same, scale handled by caller
        return target_x, target_y

    else:
        # FADE_IN and TYPEWRITER — position doesn't move
        return target_x, target_y


def animated_opacity(
    t: float,
    total_duration: float,
    fade_in: float = 0.5,
    fade_out: float = 0.5,
) -> float:
    """Compute opacity with fade-in and fade-out.

    Returns:
        Opacity value 0.0 to 1.0
    """
    if t < fade_in:
        return t / fade_in
    elif t > total_duration - fade_out:
        return max(0.0, (total_duration - t) / fade_out)
    return 1.0


# ══════════════════════════════════════════════
#  LOWER THIRD GENERATOR
# ══════════════════════════════════════════════

@dataclass(slots=True)
class LowerThirdStyle:
    """Configuration for lower-third title card."""

    bg_color: tuple[int, int, int] = (20, 20, 30)
    bg_opacity: float = 0.7
    accent_color: tuple[int, int, int] = (233, 69, 96)  # coral red
    text_color: tuple[int, int, int] = (255, 255, 255)
    height: int = 80
    accent_width: int = 4
    padding_left: int = 20
    animation: TitleAnimation = TitleAnimation.SLIDE_UP
    animation_duration: float = 0.6


def render_lower_third(
    frame: np.ndarray,
    text: str,
    t: float,
    total_duration: float,
    style: LowerThirdStyle | None = None,
) -> np.ndarray:
    """Render an animated lower-third title card onto a frame.

    Args:
        frame: Source frame (H, W, 3) uint8
        text: Title text
        t: Current time in seconds
        total_duration: How long the lower third is visible
        style: Visual style config

    Returns:
        Frame with lower third composited
    """
    import cv2

    s = style or LowerThirdStyle()
    h, w = frame.shape[:2]
    result = frame.copy()

    opacity = animated_opacity(t, total_duration, fade_in=s.animation_duration, fade_out=0.4)
    if opacity <= 0.01:
        return result

    # Lower third bar
    bar_top = h - s.height - 60
    bar_bottom = bar_top + s.height

    # Animated slide-up offset
    _, anim_y = animated_position(
        t, s.animation_duration, s.animation,
        0, bar_top, h, w,
    )
    offset = anim_y - bar_top
    bar_top += offset
    bar_bottom += offset

    if bar_top >= h or bar_bottom <= 0:
        return result

    # Clamp to frame bounds
    bar_top = max(0, bar_top)
    bar_bottom = min(h, bar_bottom)

    # Draw semi-transparent background
    overlay = result.copy()
    cv2.rectangle(overlay, (0, bar_top), (w, bar_bottom), s.bg_color, -1)
    # Accent stripe
    cv2.rectangle(overlay, (0, bar_top), (s.accent_width, bar_bottom), s.accent_color, -1)
    result = cv2.addWeighted(overlay, s.bg_opacity * opacity, result, 1.0 - s.bg_opacity * opacity, 0)

    # Draw text
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.8
    thickness = 2
    text_size = cv2.getTextSize(text, font, font_scale, thickness)[0]
    text_x = s.accent_width + s.padding_left
    text_y = bar_top + (s.height + text_size[1]) // 2

    # Text with opacity
    text_layer = result.copy()
    cv2.putText(text_layer, text, (text_x, text_y), font, font_scale, s.text_color, thickness, cv2.LINE_AA)
    result = cv2.addWeighted(text_layer, opacity, result, 1.0 - opacity, 0)

    return result
