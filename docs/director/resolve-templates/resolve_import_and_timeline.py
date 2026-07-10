#!/usr/bin/env python3
"""PinoCut Resolve template 2: media import + timeline population from a shot list.

Reads the project's machine-readable shot list (01_docs/shot-list.json), imports
media from the PinoCut folder tree into mirrored bins, then builds a timeline
with one clip per shot, trimmed to the planned duration, in shot-list order.

shot-list.json format (produced by the Director's create_shot_list tool):
{
  "fps": 25,
  "shots": [
    {"id": "s01", "source": "GEN", "file": "02_assets/gen/s01_t02.png",
     "duration_sec": 3.0, "description": "hook: defect heatmap reveal"},
    {"id": "s02", "source": "SCREEN", "file": "02_assets/screen/dashboard.mov",
     "duration_sec": 6.5, "in_sec": 12.0, "description": "live dashboard"}
  ]
}

Known API limits (handled honestly below):
  * AppendToTimeline can set in/out via clipInfo record frames; stills get a
    duration instead.
  * Grades/Fusion comps cannot be authored from Python — the Director delivers
    those as node recipes for manual application.

Usage:
    python resolve_import_and_timeline.py --project "2026-07 RoboQC Heatmap" \
        --root /path/to/projects/2026-07-roboqc-heatmap --timeline "v01_assembly"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from resolve_bootstrap import get_resolve  # same directory


def find_bin(media_pool, path: str):
    """Resolve a slash-separated bin path under the media pool root."""
    folder = media_pool.GetRootFolder()
    for segment in path.split("/"):
        match = next((f for f in folder.GetSubFolderList() if f.GetName() == segment), None)
        if match is None:
            return None
        folder = match
    return folder


def import_shot_media(media_pool, root: Path, shots: list[dict]) -> dict[str, object]:
    """Import each shot's file into the bin mirroring its folder. Returns file->MediaPoolItem."""
    imported: dict[str, object] = {}
    for shot in shots:
        rel = shot["file"]
        abs_path = root / rel
        if not abs_path.exists():
            sys.exit(f"Missing media for shot {shot['id']}: {abs_path}")

        bin_path = str(Path(rel).parent)
        folder = find_bin(media_pool, bin_path)
        if folder is None:
            sys.exit(f"Bin '{bin_path}' not found — run resolve_bootstrap.py first")
        media_pool.SetCurrentFolder(folder)

        if rel not in imported:
            items = media_pool.ImportMedia([str(abs_path)])
            if not items:
                sys.exit(f"Resolve failed to import {abs_path}")
            item = items[0]
            # Metadata makes the media pool searchable and feeds smart bins.
            item.SetMetadata("Shot", shot["id"])
            item.SetMetadata("Description", shot.get("description", ""))
            imported[rel] = item
    return imported


def build_timeline(project, media_pool, name: str, shots: list[dict], items: dict, fps: float):
    timeline = media_pool.CreateEmptyTimeline(name)
    if timeline is None:
        sys.exit(f"Could not create timeline '{name}' (name already in use?)")
    project.SetCurrentTimeline(timeline)

    for shot in shots:
        item = items[shot["file"]]
        duration_frames = round(shot["duration_sec"] * fps)
        start_frame = round(shot.get("in_sec", 0.0) * fps)
        clip_info = {
            "mediaPoolItem": item,
            "startFrame": start_frame,
            "endFrame": start_frame + duration_frames - 1,
        }
        if not media_pool.AppendToTimeline([clip_info]):
            sys.exit(f"AppendToTimeline failed at shot {shot['id']}")

    return timeline


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--root", required=True, help="PinoCut project root folder")
    ap.add_argument("--timeline", default="v01_assembly")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    shot_list_path = root / "01_docs" / "shot-list.json"
    if not shot_list_path.exists():
        sys.exit(f"No shot list at {shot_list_path}")
    data = json.loads(shot_list_path.read_text(encoding="utf-8"))
    shots, fps = data["shots"], float(data.get("fps", 25))

    resolve = get_resolve()
    pm = resolve.GetProjectManager()
    project = pm.LoadProject(args.project)
    if project is None:
        sys.exit(f"Project '{args.project}' not found — run resolve_bootstrap.py first")

    media_pool = project.GetMediaPool()
    items = import_shot_media(media_pool, root, shots)
    timeline = build_timeline(project, media_pool, args.timeline, shots, items, fps)

    total = sum(s["duration_sec"] for s in shots)
    pm.SaveProject()
    print(
        f"OK: timeline '{timeline.GetName()}' — {len(shots)} shots, "
        f"planned {total:.1f}s @ {fps}fps. Grade/Fusion recipes apply manually."
    )


if __name__ == "__main__":
    main()
