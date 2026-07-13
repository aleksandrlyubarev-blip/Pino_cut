"""Export TimelineV1 to a DaVinci Resolve-importable CMX 3600 EDL.

PinoCut assembles the rough cut; Resolve (or any NLE) finishes it. The EDL
carries cut points and dissolves, and ``FROM/TO CLIP NAME`` comments carry
the source filenames Resolve uses to relink media on import
(File → Import → Timeline → Pre-conform / EDL).

Mapping notes:

- ``cut``            → plain C event
- ``crossfade``      → two-line D (dissolve) event per CMX 3600
- ``dip_to_black``   → approximated as a dissolve (Resolve users can swap
  the transition after import; EDL has no native dip primitive)
- Audio tracks and subtitles are not representable in CMX 3600 and are
  intentionally left to the timeline JSON / rendered media.
"""

from __future__ import annotations

from pathlib import Path

from pinocut.state import TimelineSegment, TimelineV1

_DISSOLVE_TYPES = {"crossfade", "dip_to_black", "match_cut"}


def seconds_to_timecode(seconds: float, fps: int) -> str:
    """Convert seconds to non-drop HH:MM:SS:FF at the given fps."""
    total_frames = round(max(0.0, seconds) * fps)
    frames = total_frames % fps
    total_seconds = total_frames // fps
    secs = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


def timeline_to_edl(
    timeline: TimelineV1,
    clip_paths: dict[str, Path] | None = None,
) -> str:
    """Render the timeline as CMX 3600 EDL text."""
    fps = timeline.export_profile.fps
    clip_paths = clip_paths or {}
    lines: list[str] = [
        f"TITLE: {timeline.scene_id}",
        "FCM: NON-DROP FRAME",
        "",
    ]

    for index, segment in enumerate(timeline.video_segments):
        event = index + 1
        transition = segment.transition_in
        dissolve_frames = 0
        if (
            index > 0
            and transition is not None
            and transition.type in _DISSOLVE_TYPES
            and transition.duration_sec > 0
        ):
            dissolve_frames = max(1, round(transition.duration_sec * fps))

        src_in = seconds_to_timecode(segment.source_in_sec, fps)
        src_out = seconds_to_timecode(segment.source_out_sec, fps)
        rec_in = seconds_to_timecode(segment.timeline_in_sec, fps)
        rec_out = seconds_to_timecode(segment.timeline_out_sec, fps)

        if dissolve_frames:
            previous = timeline.video_segments[index - 1]
            prev_out = seconds_to_timecode(previous.source_out_sec, fps)
            # CMX dissolve: a zero-duration statement of the outgoing source,
            # then the incoming source with the D <frames> transition.
            lines.append(
                f"{event:03d}  AX       V     C        "
                f"{prev_out} {prev_out} {rec_in} {rec_in}"
            )
            lines.append(
                f"{event:03d}  AX       V     D {dissolve_frames:03d}    "
                f"{src_in} {src_out} {rec_in} {rec_out}"
            )
            lines.append(f"* FROM CLIP NAME: {_clip_name(previous, clip_paths)}")
            lines.append(f"* TO CLIP NAME: {_clip_name(segment, clip_paths)}")
        else:
            lines.append(
                f"{event:03d}  AX       V     C        "
                f"{src_in} {src_out} {rec_in} {rec_out}"
            )
            lines.append(f"* FROM CLIP NAME: {_clip_name(segment, clip_paths)}")
        lines.append("")

    return "\n".join(lines)


def export_edl(
    timeline: TimelineV1,
    clip_paths: dict[str, Path],
    output_dir: str | Path,
) -> Path:
    """Write the EDL next to the other scene artifacts."""
    output_path = Path(output_dir) / f"{timeline.scene_id}.edl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(timeline_to_edl(timeline, clip_paths), encoding="utf-8")
    return output_path


def _clip_name(segment: TimelineSegment, clip_paths: dict[str, Path]) -> str:
    path = clip_paths.get(segment.clip_id)
    return path.name if path is not None else f"{segment.clip_id}.mp4"
