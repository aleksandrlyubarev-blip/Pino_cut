#!/usr/bin/env python3
"""PinoCut Resolve template 3: render-queue setup with per-platform presets.

Adds the current (or named) timeline to the render queue with exact deliver
settings per platform, then optionally starts rendering.

Presets encode PinoCut's standing deliver targets:
  youtube          H.264/mp4, 16:9, high bitrate, Rec.709
  linkedin         H.264/mp4, native aspect, 1080p class
  reels            H.264/mp4, 9:16 1080x1920
  client_master    DNxHR HQ / QuickTime (edit-friendly master)

Loudness (-14 LUFS etc.) is a Fairlight/mix concern, not a Deliver setting —
the Director's audio plan handles it upstream.

Usage:
    python resolve_render_queue.py --project "2026-07 RoboQC Heatmap" \
        --timeline "v03_fine" --platform linkedin \
        --out /path/to/projects/2026-07-roboqc-heatmap/04_renders/final \
        --filename roboqc-heatmap_linkedin_v01 --start
"""

from __future__ import annotations

import argparse
import sys
import time

from resolve_bootstrap import get_resolve  # same directory

PRESETS: dict[str, dict] = {
    "youtube": {
        "render_settings": {
            "FormatWidth": 1920, "FormatHeight": 1080,
            "VideoQuality": 40_000_000,  # ~40 Mbps: comfortable for 1080p25/30 upload masters
            "AudioCodec": "aac", "AudioBitDepth": 16, "AudioSampleRate": 48000,
            "ExportVideo": True, "ExportAudio": True,
        },
        "format": "mp4", "codec": "H264",
    },
    "linkedin": {
        "render_settings": {
            "VideoQuality": 20_000_000,  # LinkedIn recompresses hard; 20 Mbps is plenty
            "AudioCodec": "aac", "AudioSampleRate": 48000,
            "ExportVideo": True, "ExportAudio": True,
        },
        "format": "mp4", "codec": "H264",
    },
    "reels": {
        "render_settings": {
            "FormatWidth": 1080, "FormatHeight": 1920,
            "VideoQuality": 15_000_000,
            "AudioCodec": "aac", "AudioSampleRate": 48000,
            "ExportVideo": True, "ExportAudio": True,
        },
        "format": "mp4", "codec": "H264",
    },
    "client_master": {
        "render_settings": {
            "VideoQuality": 0,  # 0 = codec default; DNxHR HQ is CBR-class already
            "AudioCodec": "lpcm", "AudioBitDepth": 24, "AudioSampleRate": 48000,
            "ExportVideo": True, "ExportAudio": True,
        },
        "format": "mov", "codec": "DNxHR HQ",
    },
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--timeline", help="Timeline name; defaults to the current one")
    ap.add_argument("--platform", required=True, choices=sorted(PRESETS))
    ap.add_argument("--out", required=True, help="Target directory for the render")
    ap.add_argument("--filename", required=True, help="Output name without extension")
    ap.add_argument("--start", action="store_true", help="Start rendering and wait")
    args = ap.parse_args()

    resolve = get_resolve()
    pm = resolve.GetProjectManager()
    project = pm.LoadProject(args.project)
    if project is None:
        sys.exit(f"Project '{args.project}' not found")

    if args.timeline:
        timeline = next(
            (project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)
             if project.GetTimelineByIndex(i).GetName() == args.timeline),
            None,
        )
        if timeline is None:
            sys.exit(f"Timeline '{args.timeline}' not found")
        project.SetCurrentTimeline(timeline)

    preset = PRESETS[args.platform]
    if not project.SetCurrentRenderFormatAndCodec(preset["format"], preset["codec"]):
        sys.exit(
            f"Resolve rejected format/codec {preset['format']}/{preset['codec']} — "
            "codec names vary by Resolve version/OS; check Deliver page names"
        )

    settings = dict(preset["render_settings"])
    settings["TargetDir"] = args.out
    settings["CustomName"] = args.filename
    if not project.SetRenderSettings(settings):
        sys.exit("SetRenderSettings failed — one of the keys/values is invalid for this codec")

    job_id = project.AddRenderJob()
    if not job_id:
        sys.exit("AddRenderJob failed")
    print(f"Queued render job {job_id}: {args.filename}.{preset['format']} ({args.platform})")

    if args.start:
        project.StartRendering([job_id], isInteractiveMode=False)
        while project.IsRenderingInProgress():
            status = project.GetRenderJobStatus(job_id)
            print(f"  {status.get('JobStatus')} {status.get('CompletionPercentage', 0)}%", flush=True)
            time.sleep(3)
        final = project.GetRenderJobStatus(job_id)
        if final.get("JobStatus") != "Complete":
            sys.exit(f"Render did not complete: {final}")
        print("OK: render complete")


if __name__ == "__main__":
    main()
